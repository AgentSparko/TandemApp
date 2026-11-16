"""Decision providers (SRV-851): who answers the conductor's questions in the controller.

The conductor asks typed questions (classify, packages, deliverable, memory_intent, memory_gate,
judge; proxy/providerstore.py `QUESTIONS`) of its decider stage. A provider answers in that
stage's place, for the questions it maps:

    builtin          the conductor's own decider (the `decider` role's endpoint, `tester` for the
                     judge) at a model per question: two models of one decider side by side
    systemone-http   the Jev contract over HTTP (classify.JevHttp): `POST {endpoint}/v1/systemone`,
                     `GET /readyz` and `/v1/models`, with an API key as the bearer; an endpoint,
                     a key, a model and a timeout per question, the provider's own for the rest
    hook             a `decision.ask` hook of the hooks' registry (proxy/hooks.py `Runner.ask`):
                     any other API shape behind an adapter the owner writes as a webhook
    plugin           a running plugin's provider (SRV-859, proxy/pluginhost.py): `decision/ask`
                     over its stdio, the question's text at the plugin's granted prompts: level.
                     A running plugin's providers join the registry's in shadow (`contrib`, id
                     `<plugin>/<provider>`); a registry provider of kind plugin naming the same
                     `target` takes its place, with its own mode

WHERE IT RUNS. Only the controller's conductor (proxy/app.py `conductor_of`, run by the harness
around relayed requests, its judge and the controller's memory writer) asks through providers:
the harness sets `router.providers` and `judge.providers` (proxy/harness.py). The engine's own
conductor (tandemapp/engine_ext.py, in the engine's process) leaves them None and asks its
in-process decision models as before; docs/decision-providers.md says what routing those would
take.

MODES per provider:
    on       the provider answers its questions; on a failure the built-in answer is used
    shadow   the conductor decides as without it; the provider is asked the same question off
             the request's path and its answer, its time and whether it agrees with the
             conductor's decision are journaled; nothing it says is used
    ab       a sticky share (`ab_percent`) of sessions (or users, or requests: `sticky`) get the
             provider as in `on` (arm b), the rest the built-in (arm a); both arms journaled

FALLBACK. A provider that times out, fails, answers an id that is not an option, has its breaker
open, or whose last health probe said down is not used: the stage asks the built-in decider (the
role's adapter) instead, or answers what it answered without a provider (`decider_role_off`,
`decider_no_endpoint`), and the stage's row names the provider and why (`provider.fallback`).
The breaker is the hooks' (5 failures in a row, or a p95 at or over the timeout across 20 calls
in 60 s, opens it for 30 s, then one probe), one per provider and endpoint.

THE JOURNAL. Every provider call and every arm-a decision is one row of the conductor's journal
(`kind: provider`, task = the question id): the provider, mode, arm, which answer was used, the
provider's answer (label, confidence, probabilities, model, time, error), the built-in decision,
agreement, the fallback's reason and a hash of the question's text (`key`, the replay's index).
Never the text. `report()` reads them back per provider and question.

REPLAY. `Recorded` answers from journaled rows (tools/decision_replay.py) stand in for every
provider: the same text, question and options get the same answer, so a run replayed with them
decides the same every time (tests/test_decision_providers.py).
"""

from __future__ import annotations

import collections
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from proxy import hooks as hooks_mod
from proxy.providerstore import (DEFAULT_SCOPE, QUESTIONS, ProviderError, ProviderStore, no_key,
                                 origin)
from tandemapp.conductor import classify

HEALTH_TTL_S = 10.0
RELOAD_CHECK_S = 2.0
RING = 5000
CLIENTS_MAX = 64
FAILING_WINDOW_S = 60.0
PROBE_TIMEOUT_MS = 1500
REPORT_MAX_ROWS = 200000

#: the test action's question per id (made-up text; the conductor's own wording where it has one)
SAMPLES = {
    "classify": ("What kind of work does this request ask for?",
                 {"code_edit": "change code in a project", "debug": "find and fix a failure",
                  "chat": "a question or a conversation", "research": "find and read sources"},
                 "Rename the variable cnt to count in utils.py and run the tests."),
    "packages": ("Which of these skills fits the work this request asks for best?",
                 {"python-testing": "writing and running Python tests",
                  "web-design": "pages, layout and styles", "none": "none of them"},
                 "Add a unit test for the parser and run it."),
    "deliverable": ("What does this request ask to deliver?",
                    {"code": "a change to the code", "answer": "an answer in words",
                     "document": "a document or a report"},
                    "Write a short summary of the release notes."),
    "judge": ("Does this tool output show a failure?",
              {"failure": "a traceback, a failed test, an error line or a crash",
               "clean": "it ran and shows no failure",
               "unclear": "it cannot be told from this output"},
              "3 passed, 1 failed in 0.42s"),
}


def _samples() -> dict:
    out = dict(SAMPLES)
    try:
        from tandemapp.conductor import memory, memwriter
        out["memory_intent"] = (memory.INTENT_QUESTION, dict(memory.INTENT_OPTIONS),
                                "Remember that I want short answers from now on.")
        out["memory_gate"] = (memwriter.GATE_QUESTION, dict(memwriter.GATE_OPTIONS),
                              "From now on use tabs, not spaces, in this project.")
    except Exception:                                              # noqa: BLE001
        pass
    return out


def state_key(qid: str, state: str, question: str, options: dict) -> str:
    """The replay's index: the question id, its text, its wording and its options."""
    raw = json.dumps([qid, state, question, sorted(options.items())], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _rank(ds: list, p: float):
    return round(hooks_mod.rank(sorted(ds), p), 1) if ds else None


class Reply:
    """A provider's answer to one question, or why there is none."""
    __slots__ = ("label", "confidence", "probs", "model", "ms", "error", "endpoint", "limits")

    def __init__(self, label=None, confidence=0.0, probs=None, model=None, ms=0.0, error=None,
                 endpoint=None, limits=None):
        self.label, self.confidence = label, float(confidence or 0.0)
        self.probs, self.model, self.ms = dict(probs or {}), model, round(float(ms), 2)
        self.error, self.endpoint, self.limits = error, endpoint, limits

    def view(self) -> dict:
        return {"label": self.label, "confidence": round(self.confidence, 4),
                "probs": {k: round(float(v), 4) for k, v in self.probs.items()},
                "model": self.model, "ms": self.ms, "error": self.error}


class Recorded:
    """Recorded answers in place of every provider: state_key -> {label, confidence, probs,
    model, limits}; (provider, question) -> the limits the recorded calls read with."""

    def __init__(self, answers: dict | None = None, limits: dict | None = None):
        self.answers = dict(answers or {})
        self.limits = dict(limits or {})

    @classmethod
    def from_records(cls, recs) -> "Recorded":
        out = cls()
        for r in recs:
            a, key = r.get("answer"), r.get("key")
            if not key or not isinstance(a, dict) or a.get("error") or a.get("label") is None:
                continue
            out.answers[key] = {k: a.get(k) for k in ("label", "confidence", "probs", "model")}
            if r.get("limits"):
                out.limits[f"{r.get('provider')}/{r.get('question')}"] = dict(r["limits"])
        return out

    @classmethod
    def load(cls, path: str) -> "Recorded":
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return cls(doc.get("answers"), doc.get("limits"))

    def dump(self) -> dict:
        return {"kind": "decision-replay", "version": 1, "answers": self.answers,
                "limits": self.limits}

    def reply(self, qid, state, question, options) -> Reply:
        a = self.answers.get(state_key(qid, state, question, options))
        if a is None:
            return Reply(error="replay_miss")
        if a.get("label") not in options:
            return Reply(error="unknown_label")
        return Reply(a["label"], a.get("confidence") or 0.0, a.get("probs"), a.get("model"))


class Served(classify.Decider):
    """The decider stage of one question as a provider serves it (`on`, or arm b of `ab`): the
    router's cascade and the judge ask it as any decider."""

    name = "provider"

    def __init__(self, ps: "Providers", p: dict, qid: str, builtin, why: str | None,
                 timeout_ms: int, arm: str | None):
        self.ps, self.p, self.qid, self.builtin = ps, p, qid, builtin
        self.why, self.timeout_ms, self.arm = why or "no_endpoint", int(timeout_ms), arm

    def limits(self) -> dict:
        got = self.ps.limits(self.p, self.qid, self.timeout_ms)
        if got is not None:
            return got
        if self.builtin is not None and hasattr(self.builtin, "limits"):
            return self.builtin.limits()
        return dict(classify.DEFAULT_LIMITS)

    def choice(self, state: str, question: str, options: dict) -> classify.Answer:
        return self.ps.serve(self, state, question, options)


class Providers:
    """The controller's decision providers: the registry, the calls, health, the journal."""

    def __init__(self, cfg, runner=None, log=None, clock=time.monotonic):
        self.runner = runner
        self.store = ProviderStore(cfg, hook=(lambda hid: runner.store.get(hid))
                                   if runner is not None else None)
        self.log = log or (lambda m: None)
        self.clock = clock
        self.journal = None                 # the conductor's journal (`attach`)
        self.roles = lambda: {}             # the conductor's roles document (`attach`)
        self.replay: Recorded | None = None
        self.pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="provider")
        self.bg = hooks_mod.Background(workers=2, size=1000)
        self.breakers: dict = {}
        self.ring: collections.deque = collections.deque(maxlen=RING)
        self._health: dict = {}
        self._clients: collections.OrderedDict = collections.OrderedDict()
        self._rows: list[dict] = []
        self._gen = -1
        self._dv = None
        self._checked = 0.0
        self._lock = threading.Lock()
        # SRV-859: the plugin host's providers fn() -> (generation, rows), its ask and liveness
        self.contrib = None
        self.plugin_ask = None
        self.plugin_up = lambda pid: False
        self._cgen = None

    def attach(self, conductor) -> None:
        """The controller's conductor: its journal for the rows, its roles for `builtin`."""
        self.journal = getattr(conductor, "journal", None) or None
        store = getattr(conductor, "store", None)
        self.roles = (lambda: store.peek("roles")[1]) if store is not None else (lambda: {})

    def stop(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def load_replay(self, path: str) -> int:
        self.replay = Recorded.load(path)
        return len(self.replay.answers)

    # ------------------------------------------------------------------ the registry, cached
    def rows(self) -> list[dict]:
        """The enabled providers; reloaded when the registry changed here, or (checked every
        2 s) in another process."""
        now = time.monotonic()
        dv = None
        if now - self._checked >= RELOAD_CHECK_S:
            self._checked = now
            dv = self.store.data_version()
        cgen, crows = (None, [])
        if self.contrib is not None:
            try:
                cgen, crows = self.contrib()
            except Exception:                                      # noqa: BLE001
                cgen, crows = None, []
        if self.store.gen == self._gen and (dv is None or dv == self._dv) and \
                cgen == self._cgen:
            return self._rows
        with self._lock:
            try:
                rows = self.store.list() if self.store.ready() else []
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[providers] the registry could not be read: {type(exc).__name__}")
                rows = []
            if crows:
                named = {p.get("target") for p in rows if p["kind"] == "plugin"}
                rows = rows + [p for p in crows if p["target"] not in named]
            self._rows = [p for p in rows if p["enabled"]]
            self._gen = self.store.gen
            self._cgen = cgen
            if dv is not None:
                self._dv = dv
        return self._rows

    def row(self, pid: str) -> dict:
        """A provider of the registry, or a running plugin's contributed one."""
        p = self.store.get(pid)
        if p is not None:
            return p
        for r in self.rows():
            if r["id"] == pid:
                return r
        return self.store.need(pid)

    def idle(self) -> bool:
        return not self.rows()

    def mapped(self, qid: str) -> tuple[dict | None, list[dict]]:
        """(the provider that serves `qid` on or ab, the ones that watch it in shadow)."""
        primary, shadows = None, []
        for p in self.rows():
            if qid not in p["questions"]:
                continue
            if p["mode"] == "shadow":
                shadows.append(p)
            elif primary is None:
                primary = p
        return primary, shadows

    @staticmethod
    def arm(p: dict, ctx: dict | None = None) -> str:
        """`b` (the provider) for a sticky `ab_percent` share, else `a` (the built-in). The key
        is the context's session, user or request (`sticky`), the next one when it is missing;
        with none at all the built-in."""
        ctx = ctx if ctx is not None else (classify.CONTEXT.get() or {})
        share = int(p.get("ab_percent") or 0)
        v = ctx.get(p.get("sticky") or "session") or ctx.get("session") or ctx.get("request")
        if not v or share <= 0:
            return "a"
        if share >= 100:
            return "b"
        h = int(hashlib.sha256(f"{p['id']}\0{v}".encode()).hexdigest()[:8], 16) % 100
        return "b" if h < share else "a"

    # ------------------------------------------------------------------ the conductor's seams
    def stage(self, qid: str, builtin, why: str | None, timeout_ms: int):
        """The decider stage of `qid` as a provider serves it, or None: no provider serves it on
        or ab, or this session is in arm a (the built-in `builtin` as it is). Asks nothing."""
        try:
            if self.idle():
                return None
            p, _ = self.mapped(qid)
            if p is None:
                return None
            arm = None
            if p["mode"] == "ab":
                arm = self.arm(p)
                if arm == "a":
                    return None
            return Served(self, p, qid, builtin, why, timeout_ms, arm)
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[providers] {qid}: the stage stays built-in ({type(exc).__name__})")
            return None

    def cache_tag(self, qid: str):
        """What the router's answer cache adds to its key for `qid`: the provider serving it and
        this session's arm, so an answer is never reused across them; None with no provider."""
        try:
            if self.idle():
                return None
            p, _ = self.mapped(qid)
            if p is None:
                return None
            return ("provider", p["id"], p["version"], self.arm(p) if p["mode"] == "ab" else "on")
        except Exception:                                          # noqa: BLE001
            return None

    def after(self, qid: str, text: str, options: dict, question: str, line, chars: int, got,
              state: str | None = None) -> None:
        """After a question was decided: arm a's row, and each shadow provider asked the same
        question off this thread. Never raises; asks nothing on this thread."""
        try:
            if self.idle():
                return
            p, shadows = self.mapped(qid)
            if (p is None or p["mode"] != "ab") and not shadows:
                return
            accepted, stages = got
            dec = next((s for s in stages or () if isinstance(s, dict)
                        and s.get("stage") == "decider"), None)
            ctx = dict(classify.CONTEXT.get() or {})
            base = {"label": getattr(accepted, "label", None),
                    "stage": getattr(accepted, "stage", None),
                    "decider": None if dec is None else
                    {"label": dec.get("label"), "ms": dec.get("ms"), "error": dec.get("error")}}
            if p is not None and p["mode"] == "ab" and self.arm(p, ctx) == "a":
                self.record(p, qid, "a", "builtin", base["label"], None, base, None, None, ctx)
            if not shadows:
                return
            if state is None:
                from tandemapp.conductor import signals
                state = signals.digest(text or "", int(chars), line)
            opts = dict(options)
            for sp in shadows:
                if not self.bg.submit(lambda sp=sp: self._shadow(sp, qid, state, question, opts,
                                                                 base, ctx)):
                    self.log(f"[providers] {sp['id']}: the shadow queue is full, {qid} dropped")
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[providers] {qid}: after failed ({type(exc).__name__})")

    def _shadow(self, p, qid, state, question, options, base, ctx) -> None:
        tok = classify.CONTEXT.set(ctx)
        try:
            rep = self.ask(p, qid, state, question, options)
            agree = (rep.label == base["label"]) if rep.error is None and \
                base["label"] is not None else None
            self.record(p, qid, None, "builtin", base["label"], rep, base, agree, None, ctx,
                        key=state_key(qid, state, question, options))
        finally:
            classify.CONTEXT.reset(tok)

    def serve(self, s: Served, state: str, question: str, options: dict) -> classify.Answer:
        """A Served stage's answer: the provider's, else the built-in's with the reason."""
        p = s.p
        rep = self.ask(p, s.qid, state, question, options, s.timeout_ms)
        tag = {"id": p["id"], "mode": p["mode"]}
        if s.arm is not None:
            tag["arm"] = s.arm
        if rep.error is None:
            ans = classify.Answer("decider", rep.label, rep.confidence, rep.probs, rep.ms,
                                  model=rep.model)
            used, base = "provider", None
        else:
            if s.builtin is not None:
                ans = s.builtin.choice(state, question, options)
            else:
                ans = classify.Answer("decider", error=f"decider_{s.why}")
            tag["fallback"] = rep.error
            used = "builtin"
            base = {"label": getattr(ans, "label", None), "stage": "decider",
                    "decider": {"label": getattr(ans, "label", None),
                                "ms": getattr(ans, "ms", None),
                                "error": getattr(ans, "error", None)}}
        try:
            ans.provider = tag
        except AttributeError:
            pass
        self.record(p, s.qid, s.arm, used, getattr(ans, "label", None), rep, base, None,
                    rep.error, classify.CONTEXT.get() or {},
                    key=state_key(s.qid, state, question, options))
        return ans

    # ------------------------------------------------------------------ one call
    def _spec(self, p: dict, qid: str, timeout_ms) -> tuple[dict, str, int]:
        spec = p["questions"].get(qid) or {}
        model = spec.get("model") or p["model"] or "auto"
        t = int(spec.get("timeout_ms") or p["timeout_ms"])
        if timeout_ms:
            t = min(t, int(timeout_ms))
        return spec, model, max(1, t)

    def _endpoint(self, p: dict, spec: dict) -> str | None:
        if p["kind"] == "systemone-http":
            return spec.get("endpoint") or p["endpoint"]
        if p["kind"] == "hook":
            return f"hook:{p['hook_id']}"
        if p["kind"] == "plugin":
            return f"plugin:{p['target']}"
        return "builtin"

    def _key(self, p: dict, qid: str, spec: dict) -> str | None:
        """The question's own key; else the provider's, never sent to another origin than the
        provider's endpoint."""
        k = self.store.key_for(p["id"], qid)
        if k:
            return k
        if spec.get("endpoint") and origin(spec["endpoint"]) != origin(p["endpoint"]):
            return None
        return self.store.key_for(p["id"], DEFAULT_SCOPE)

    def _client(self, p: dict, qid: str, spec: dict, model: str, tmo: int):
        """The decider a call goes through: a JevHttp with its key (cached per provider version,
        so a changed key is the next call's), or the role's decider for `builtin`."""
        if p["kind"] == "builtin":
            role = (self.roles() or {}).get("tester" if qid == "judge" else "decider") or {}
            ep = str(role.get("endpoint") or "")
            if role.get("mode", "off") == "off" or not ep:
                return None
            ck = (p["id"], p["version"], "builtin", ep, model, tmo)
        else:
            ep = self._endpoint(p, spec)
            ck = (p["id"], p["version"], qid if spec.get("endpoint") else None, ep, model, tmo)
        with self._lock:
            c = self._clients.get(ck)
            if c is not None:
                self._clients.move_to_end(ck)
                return c
        if p["kind"] == "builtin":
            c = classify.decider_for(ep, model, tmo)
            if c is None:
                return None
        else:
            c = classify.JevHttp(ep, model, tmo, api_key=self._key(p, qid, spec))
        with self._lock:
            self._clients = collections.OrderedDict(
                (k, v) for k, v in self._clients.items()
                if not (k[0] == p["id"] and k[1] != p["version"]))
            self._clients[ck] = c
            while len(self._clients) > CLIENTS_MAX:
                self._clients.popitem(last=False)
        return c

    def limits(self, p: dict, qid: str, timeout_ms) -> dict | None:
        """The limits the provider reads with (its /v1/models, cached by the client), None when
        it has none of its own (a hook: the built-in's)."""
        if self.replay is not None:
            got = self.replay.limits.get(f"{p['id']}/{qid}")
            return dict(got) if got else None
        if p["kind"] in ("hook", "plugin"):
            return None
        try:
            spec, model, tmo = self._spec(p, qid, timeout_ms)
            ep = self._endpoint(p, spec)
            h = self._health.get((p["id"], p["version"], ep))
            if self._breaker((p["id"], ep)).view()["state"] == "open" or (
                    h is not None and time.monotonic() - h[0] < HEALTH_TTL_S and
                    not h[1].get("up")):
                return None                   # skipped: the built-in reads with its own
            c = self._client(p, qid, spec, model, tmo)
            return c.limits() if c is not None else None
        except Exception:                                          # noqa: BLE001
            return None

    def _breaker(self, key) -> hooks_mod.Breaker:
        b = self.breakers.get(key)
        if b is None:
            b = self.breakers.setdefault(key, hooks_mod.Breaker(self.clock))
        return b

    def ask(self, p: dict, qid: str, state: str, question: str, options: dict,
            timeout_ms=None, *, count: bool = True, live: bool = False) -> Reply:
        """One question to provider `p` within its timeout (at most `timeout_ms`, the stage's).
        `count`: the breaker sees it and an open breaker or a down probe skips it; `live`: the
        wire even when recorded answers are loaded (the test action). Never raises."""
        spec, model, tmo = self._spec(p, qid, timeout_ms)
        ep = self._endpoint(p, spec)
        if self.replay is not None and not live:
            rep = self.replay.reply(qid, state, question, options)
            rep.endpoint = "replay"
            return rep
        bkey = (p["id"], ep)
        gate = "call"
        if count:
            h = self._health.get((p["id"], p["version"], ep))
            if h is not None and time.monotonic() - h[0] < HEALTH_TTL_S and not h[1].get("up"):
                return Reply(error="provider_down", endpoint=ep)
            gate = self._breaker(bkey).allow()
            if gate is None:
                return Reply(error="breaker_open", endpoint=ep)
        t0 = time.perf_counter()
        try:
            if p["kind"] == "hook":
                rep = self._ask_hook(p, qid, state, question, options, model, tmo)
            elif p["kind"] == "plugin":
                rep = self._ask_plugin(p, qid, state, question, options, model, tmo)
            else:
                c = self._client(p, qid, spec, model, tmo)
                if c is None:
                    rep = Reply(error="no_builtin")
                else:
                    a = c.choice(state, question, options)
                    err = a.error
                    if err is not None and err.startswith("decider_"):
                        err = err[8:]
                    if err is None and a.label not in options:
                        err = "unknown_label"
                    rep = (Reply(error=err, ms=a.ms, model=a.model) if err else
                           Reply(a.label, a.confidence, a.probs, a.model, a.ms))
                    if err is None and hasattr(c, "limits"):
                        rep.limits = c.limits()
        except Exception as exc:                                   # noqa: BLE001
            why = "key_unreadable" if type(exc).__name__ == "SecretError" else "error"
            rep = Reply(error=why, ms=(time.perf_counter() - t0) * 1e3)
        rep.endpoint = ep
        if count:
            br = self._breaker(bkey)
            if rep.error in ("no_builtin", "hook_off", "out_of_scope", "no_runner",
                             "no_host"):
                if gate == "probe":
                    br.probing = False
            else:
                br.record(rep.error is None, rep.ms, float(tmo), gate == "probe")
        return rep

    def _hook_ctx(self) -> dict:
        c = classify.CONTEXT.get() or {}
        out = {k: None for k in hooks_mod.CTX_KEYS}
        out.update(request_id=c.get("request"), session=c.get("session"), user=c.get("user"),
                   endpoint="decision")
        try:
            from proxy import harness
            t = harness.CURRENT.get()
            if t is not None:
                out.update(backend=t.backend.get("id"), model=t.route.model,
                           upstream_model=t.route.model, client_kind=t.rec.client_kind,
                           stream=bool(t.stream))
        except Exception:                                          # noqa: BLE001
            pass
        return out

    def _ask_hook(self, p, qid, state, question, options, model, tmo) -> Reply:
        if self.runner is None:
            return Reply(error="no_runner")
        h = self.runner.store.get(p["hook_id"])
        if h is None or not h["enabled"] or h["mode"] != "on":
            return Reply(error="hook_off")
        ctx = self._hook_ctx()
        if not self.runner.in_scope(h, ctx):
            return Reply(error="out_of_scope")
        q = {"id": qid, "type": "choice", "instructions": question, "options": dict(options),
             "model": model, "deadline_ms": int(tmo), "state_chars": len(state)}
        t0 = time.perf_counter()
        fut = self.pool.submit(self.runner.ask, ctx, q, {"state": state}, [h])
        try:
            dec = fut.result(timeout=tmo / 1000.0)
        except FutureTimeout:
            return Reply(error="timeout", ms=(time.perf_counter() - t0) * 1e3)
        ms = (time.perf_counter() - t0) * 1e3
        a = (dec or {}).get("answer")
        if a is None:
            return Reply(error="no_answer", ms=ms)
        if isinstance(a, str):
            label, conf, probs = a, 1.0, {a: 1.0}
        else:
            label = a.get("choice")
            probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
            conf = float(a["confidence"]) if "confidence" in a else probs.get(label, 1.0)
            probs = probs or {label: conf}
        if label not in options:
            return Reply(error="unknown_label", ms=ms)
        return Reply(label, conf, probs, f"hook:{h['id']}", ms)

    def _ask_plugin(self, p, qid, state, question, options, model, tmo) -> Reply:
        if self.plugin_ask is None:
            return Reply(error="no_host")
        t0 = time.perf_counter()
        got = self.plugin_ask(p["target"], qid, state, question, options, model, tmo)
        ms = (time.perf_counter() - t0) * 1e3
        if got.get("error"):
            return Reply(error=got["error"], ms=ms)
        if got["label"] not in options:
            return Reply(error="unknown_label", ms=ms)
        return Reply(got["label"], got["confidence"], got["probs"], f"plugin:{p['target']}", ms)

    # ------------------------------------------------------------------ the journal
    def record(self, p, qid, arm, used, label, rep: Reply | None, base, agree, fallback, ctx,
               key=None) -> dict:
        rec = {"provider": p["id"], "kind": p["kind"], "question": qid,
               "mode": p["mode"], "arm": arm,
               "used": used, "label": label, "answer": rep.view() if rep is not None else None,
               "endpoint": rep.endpoint if rep is not None else None,
               "builtin": base, "agree": agree, "fallback": fallback, "key": key,
               "limits": rep.limits if rep is not None and rep.limits else None,
               "request": (ctx or {}).get("request"), "session": (ctx or {}).get("session"),
               "ts_ms": int(time.time() * 1000)}
        self.ring.append(rec)
        j = self.journal
        if j is not None:
            try:
                j.note("provider", qid, decision=label,
                       detail={k: v for k, v in rec.items()
                               if k not in ("ts_ms", "request", "session")},
                       session_key=rec["session"], request_id=rec["request"])
            except Exception:                                      # noqa: BLE001
                pass
        return rec

    def records(self, since_ms: int = 0, provider: str | None = None) -> tuple[list[dict], str]:
        """(the provider rows since `since_ms`, newest first, where they came from: the
        conductor's journal file, else this process's memory)."""
        j = self.journal
        if j is not None and getattr(j, "path", None):
            try:
                out = []
                for r in j.rows(kind="provider", since_ms=since_ms, limit=REPORT_MAX_ROWS,
                                texts=False):
                    d = dict(r.get("detail") or {})
                    d.update(ts_ms=r.get("ts_ms"), request=r.get("request_id"),
                             session=r.get("session_key"))
                    if provider is None or d.get("provider") == provider:
                        out.append(d)
                return out, "journal"
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[providers] the journal could not be read: {type(exc).__name__}")
        out = [dict(r) for r in reversed(self.ring) if r["ts_ms"] >= since_ms and
               (provider is None or r["provider"] == provider)]
        return out, "memory"

    def report(self, provider: str | None = None, hours: float = 24.0) -> dict:
        """Per provider and question, per bucket (on, shadow, ab_a, ab_b): calls, answered,
        failures, fallbacks, agreement (shadow: with the conductor's decision), p50/p95 of the
        provider's time (arm a: the built-in decider's), the labels used."""
        since = int(time.time() * 1000 - float(hours) * 3600 * 1000)
        recs, src = self.records(since, provider)
        out: dict = {}
        for r in recs:
            bucket = r.get("mode") or "on"
            if bucket == "ab":
                bucket = f"ab_{r.get('arm') or 'b'}"
            b = out.setdefault(r.get("provider"), {}).setdefault(r.get("question"), {}) \
                .setdefault(bucket, {"calls": 0, "answered": 0, "failures": 0, "fallbacks": 0,
                                     "compared": 0, "agreed": 0, "labels": {}, "_ms": []})
            b["calls"] += 1
            a = r.get("answer")
            if a is not None:
                if a.get("error"):
                    b["failures"] += 1
                else:
                    b["answered"] += 1
                    b["_ms"].append(float(a.get("ms") or 0.0))
            else:
                dec = ((r.get("builtin") or {}).get("decider") or {})
                if dec.get("ms") is not None and not dec.get("error"):
                    b["_ms"].append(float(dec["ms"]))
            if r.get("fallback"):
                b["fallbacks"] += 1
            if r.get("agree") is not None:
                b["compared"] += 1
                b["agreed"] += int(bool(r["agree"]))
            lab = r.get("label") if r.get("mode") != "shadow" else (a or {}).get("label")
            if lab is not None:
                b["labels"][lab] = b["labels"].get(lab, 0) + 1
        for qs in out.values():
            for buckets in qs.values():
                for b in buckets.values():
                    ms = b.pop("_ms")
                    b["p50_ms"], b["p95_ms"] = _rank(ms, 0.5), _rank(ms, 0.95)
                    b["agreement_pct"] = round(100.0 * b["agreed"] / b["compared"], 1) \
                        if b["compared"] else None
        return {"since_ms": since, "hours": float(hours), "source": src, "providers": out}

    def calls(self, pid: str, limit: int = 100) -> list[dict]:
        return [dict(r) for r in reversed(self.ring) if r["provider"] == pid][:limit]

    # ------------------------------------------------------------------ health
    def _targets(self, p: dict) -> list[tuple[str, list[str]]]:
        by: dict = {}
        for qid in sorted(p["questions"]):
            by.setdefault(self._endpoint(p, p["questions"][qid] or {}), []).append(qid)
        return sorted(by.items())

    def _probe(self, p: dict, ep: str, qid: str, force: bool) -> dict:
        ck = (p["id"], p["version"], ep)
        h = self._health.get(ck)
        if not force and h is not None and time.monotonic() - h[0] < HEALTH_TTL_S:
            return dict(h[1], age_s=round(time.monotonic() - h[0], 1))
        spec = p["questions"].get(qid) or {}
        model = spec.get("model") or p["model"] or "auto"
        try:
            if p["kind"] == "plugin":
                pl = str(p.get("target") or "").partition("/")[0]
                up = bool(self.plugin_up(pl))
                v = {"up": up, "ms": None, "detail": {"plugin": pl}}
                if not up:
                    v["error"] = "the plugin is not running"
            elif p["kind"] == "hook":
                hk = self.runner.store.get(p["hook_id"]) if self.runner is not None else None
                if hk is None:
                    v = {"up": False, "ms": None, "error": "the hook is gone"}
                else:
                    br = self.runner.metrics(hk["id"])["breaker"]
                    up = hk["enabled"] and hk["mode"] == "on" and br["state"] != "open"
                    v = {"up": up, "ms": None, "detail": {"hook": hk["id"], "mode": hk["mode"],
                                                         "enabled": hk["enabled"],
                                                         "breaker": br["state"]}}
                    if not up:
                        v["error"] = "the hook is off, in shadow or its breaker is open"
            elif p["kind"] == "builtin":
                c = self._client(p, qid, spec, model, PROBE_TIMEOUT_MS)
                v = c.health() if c is not None else {
                    "up": False, "ms": None, "error": "no built-in decider answers here (the "
                    "role is off, or names this machine's engine, which a controller has not)"}
            else:
                c = classify.JevHttp(ep, model, PROBE_TIMEOUT_MS,
                                     api_key=self._key(p, qid, spec))
                v = c.health()
        except Exception as exc:                                   # noqa: BLE001
            v = {"up": False, "ms": None,
                 "error": "key_unreadable" if type(exc).__name__ == "SecretError" else "error"}
        v = {k: v[k] for k in ("up", "ms", "error", "detail", "limits") if k in v}
        self._health[ck] = (time.monotonic(), v)
        return dict(v, age_s=0.0)

    def health(self, pid: str, force: bool = False) -> dict:
        """The provider's endpoints with their probe (/readyz, /v1/models; cached 10 s), their
        breaker and the questions on each; `status` ok, failing (a breaker not closed, or the
        last call within a minute failed), down (a probe failed) or off."""
        p = self.row(pid)
        eps = []
        for ep, qids in self._targets(p):
            eps.append({"endpoint": ep, "questions": qids,
                        "probe": self._probe(p, ep, qids[0], force),
                        "breaker": self._breaker((pid, ep)).view()})
        last = next((r for r in reversed(self.ring) if r["provider"] == pid and
                     r.get("answer") is not None), None)
        last_view = None if last is None else {
            "ts_ms": last["ts_ms"], "question": last["question"],
            "error": (last["answer"] or {}).get("error"), "ms": (last["answer"] or {}).get("ms")}
        if not p["enabled"]:
            status = "off"
        elif any(not e["probe"].get("up") for e in eps):
            status = "down"
        elif any(e["breaker"]["state"] != "closed" for e in eps) or (
                last_view is not None and last_view["error"] and
                time.time() * 1000 - last_view["ts_ms"] < FAILING_WINDOW_S * 1000):
            status = "failing"
        else:
            status = "ok"
        return {"provider": pid, "kind": p["kind"], "status": status, "endpoints": eps,
                "last_call": last_view}

    # ------------------------------------------------------------------ the admin's views
    def view(self, p: dict, health: bool = False) -> dict:
        hints = self.store.key_hints(p["id"])
        qs = {}
        for qid, spec in sorted(p["questions"].items()):
            q = {"model": spec.get("model"), "endpoint": spec.get("endpoint"),
                 "timeout_ms": spec.get("timeout_ms")}
            if p["kind"] == "systemone-http":
                q["key"] = hints.get(qid) or no_key()
            qs[qid] = q
        out = {k: p[k] for k in ("id", "name", "kind", "endpoint", "hook_id", "model",
                                 "timeout_ms", "mode", "ab_percent", "sticky", "enabled",
                                 "created_at", "updated_at", "updated_by", "version")}
        out["target"] = p.get("target")
        out["questions"] = qs
        out["key"] = hints.get(DEFAULT_SCOPE) or no_key() if p["kind"] == "systemone-http" \
            else None
        out["breakers"] = {ep: self._breaker((p["id"], ep)).view()
                           for ep, _ in self._targets(p)}
        if health:
            out["health"] = self.health(p["id"])
        return out

    def test(self, pid: str, qid: str | None = None, text: str | None = None) -> dict:
        """The provider asked once, on the wire, with a sample question (`qid`: one it maps, the
        first by default; `text`: the text it is about): nothing counted, nothing journaled."""
        p = self.row(pid)
        qid = qid or sorted(p["questions"])[0]
        if qid not in p["questions"]:
            raise ProviderError(422, "invalid", f"question: the provider does not answer "
                                                f"{qid!r}", "question")
        if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= 8000):
            raise ProviderError(422, "invalid", "text: 1..8000 characters", "text")
        question, options, sample = _samples()[qid]
        rep = self.ask(p, qid, text or sample, question, options, count=False, live=True)
        return {"provider": pid, "question": qid, "ok": rep.error is None,
                "endpoint": rep.endpoint, "ms": rep.ms,
                "answer": rep.view() if rep.error is None else None, "error": rep.error}


def questions_view() -> dict:
    """The question ids a provider may answer, what each is, where it is asked."""
    return {q: {"what": w, "asked_in": "controller", "engine": "follow-up (docs)"}
            for q, w in QUESTIONS.items()}
