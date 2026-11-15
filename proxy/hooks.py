"""The remote hook runner (SRV-849, SEC-16): hooks-v1 around the relay, in the controller.

A hook is a row of the registry (proxy/hookstore.py) that names its stages
(proxy/hookstages.py, contracts/hooks-v1/stages.json), its role, priority, mode, scope, failure
policy, timeout and payload level, and how it is called: `http` (a signed webhook) or `builtin`
(code in this process, `BUILTINS`). `plugin`, `mcp` and `inproc` are typed in the contract and
refused until the plugin host and the in-process chain exist.

THE RULES, per stage and request (`Runner.run`):

    order        the hooks in scope, by integer priority, ties broken by id
    transforms   chain: each sees the value the ones before it made, and may `modify` it with a
                 `patch` of the stage's fields only (a content field only at payload `full`)
    guards       run after the transforms, on the transformed value; the first `deny` wins and
                 the rest are not asked
    deciders     after the guards; the first `decide` wins (route.select, request.error,
                 response.after_engine's one retry, decision.ask)
    observers    see the final value, off the request's path, one retry
    budget       a stage's budget is shared by its sync hooks; a hook's own timeout is at most
                 that. Before the first token every sync hook of the request shares CAP_MS
                 (800 ms): a hook with nothing left is not called (`cap`) and its policy applies
    policy       `open`: a failed hook is as if absent; `closed`: the request is refused (503
                 hook_unavailable). Guards default to closed, the rest to the stage's default
    retries      none on the request's path; one for an async observer
    breaker      per hook: 5 failures in a row, or a p95 at or over its timeout across at least
                 20 calls in 60 s, opens it for 30 s (calls skip it, its policy applies); then
                 one probe: a success closes it, a failure opens it again
    modes        off: never called. shadow: called off the request's path on a copy of the value
                 it would have seen; its result and diff are journaled and nothing is applied.
                 on: as above
    scope        model globs (the asked id or the backend's), backends, profiles, users,
                 sessions, client kinds; an empty list or none matches everything

A result that is not hooks-v1 (an unknown action, an action its role may not take, a patch of a
field its stage does not list, a decision key the stage does not take) is refused: journaled as
`invalid` and counted as a failure.

NO HOOK, NO CHANGE. With no hook enabled `Runner.turn` answers None and the relay runs as before,
byte for byte (tests/test_hooks_relay.py). A hook that answers `continue` changes no byte either:
a body is serialised again only when a patch changed it.

THE JOURNAL is in memory, per hook (the last JOURNAL_MAX calls): time, stage, request id, mode,
outcome, action, duration, the breaker's state after, the hook's reason and labels, a shadow's
diff (field names, operations and sizes; never a value) and our own error words. Never a payload.
"""

from __future__ import annotations

import collections
import copy
import fnmatch
import json
import math
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from proxy import hookcall
from proxy import hookstages as S
from proxy.hookstore import HookError, HookStore

JOURNAL_MAX = 500
SAMPLES_MAX = 512
BREAKER_FAILS = 5
BREAKER_WINDOW_S = 60.0
BREAKER_OPEN_S = 30.0
BREAKER_MIN_SAMPLES = 20
RELOAD_CHECK_S = 2.0
REASON_MAX = 300
LABELS_MAX = 32
# outcomes that are the hook's own failure (they count toward its breaker)
HOOK_FAILURES = ("timeout", "error", "invalid")
# outcomes where the hook was not asked or could not be: its policy applies, its breaker does not
SKIPS = ("cap", "breaker_open", "redaction_failed")


# ---------------------------------------------------------------------------------- builtins
def _b_set(env, options):
    patch = options.get("set") if isinstance(options.get("set"), dict) else {}
    return {"action": "modify", "patch": patch} if patch else {"action": "continue"}


def _b_deny(env, options):
    return {"action": "deny", "reason": str(options.get("reason") or "refused by a hook")[:300]}


def _b_route(env, options):
    d = {k: options[k] for k in ("backend", "model", "profile") if isinstance(options.get(k), str)}
    return {"action": "decide", "decision": d} if d else {"action": "continue"}


def _b_fallback(env, options):
    if not isinstance(options.get("backend"), str):
        return {"action": "continue"}
    fb = {"backend": options["backend"]}
    if isinstance(options.get("model"), str):
        fb["model"] = options["model"]
    return {"action": "decide", "decision": {"fallback": fb}}


def _b_noop(env, options):
    return {"action": "continue"}


#: name -> fn(envelope, options) -> result. `set` (a transform: options.set is the patch),
#: `deny` (a guard: options.reason), `route` (route.select: options.backend/model/profile),
#: `fallback` (request.error: options.backend/model), `noop` (an observer that journals)
BUILTINS = {"set": _b_set, "deny": _b_deny, "route": _b_route, "fallback": _b_fallback,
            "noop": _b_noop}


# ---------------------------------------------------------------------------------- results
def check_result(stage: str, role: str, level: str, res) -> tuple[dict | None, str | None]:
    """(the result, None) when `res` is a hooks-v1 result this hook may give here, else
    (None, why)."""
    spec = S.STAGES[stage]
    if not isinstance(res, dict):
        return None, "the result is not a JSON object"
    extra = set(res) - {"action", "patch", "decision", "reason", "labels"}
    if extra:
        return None, f"unknown key {sorted(extra)[0]!r}"
    a = res.get("action")
    if a not in S.ACTIONS:
        return None, f"action {a!r} is not one of {', '.join(S.ACTIONS)}"
    if a not in S.ROLE_ACTIONS[role]:
        return None, f"a {role} may not answer {a}"
    if a not in spec["actions"]:
        return None, f"{stage} takes no {a}"
    r = res.get("reason")
    if r is not None and not isinstance(r, str):
        return None, "reason: a string"
    lab = res.get("labels")
    if lab is not None:
        if not isinstance(lab, dict) or len(lab) > LABELS_MAX or not all(
                isinstance(k, str) and len(k) <= 64 and isinstance(v, (str, int, float, bool))
                and len(str(v)) <= 256 for k, v in lab.items()):
            return None, f"labels: an object of at most {LABELS_MAX} short values"
    if a == "modify":
        p = res.get("patch")
        if not isinstance(p, dict) or not p:
            return None, "modify needs a non-empty patch object"
        for k in p:
            if k not in spec["fields"]:
                return None, f"patch field {k!r} is not one {stage} lets a hook change"
            if k in spec["content"] and level != "full":
                return None, f"patch field {k!r} holds content: only a hook with payload full " \
                             f"may change it"
    elif res.get("patch") is not None:
        return None, f"a patch comes only with modify"
    if a == "decide":
        d = res.get("decision")
        if not isinstance(d, dict) or not d:
            return None, "decide needs a non-empty decision object"
        for k, v in d.items():
            if k not in spec["decision"]:
                return None, f"decision key {k!r} is not one {stage} takes"
            if k in ("backend", "model", "profile") and not (isinstance(v, str) and v):
                return None, f"decision.{k}: a string"
            if k == "retry" and v is not True:
                return None, "decision.retry: true"
            if k == "fallback" and not (isinstance(v, dict) and isinstance(v.get("backend"), str)
                                        and set(v) <= {"backend", "model"}
                                        and isinstance(v.get("model", ""), str)):
                return None, "decision.fallback: {backend, model?}"
    elif res.get("decision") is not None:
        return None, "a decision comes only with decide"
    return res, None


def merge_patch(value: dict, patch: dict) -> dict:
    """A top-level patch: each key set, null removes it. A new dict."""
    out = dict(value)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def diff_of(value, patch: dict) -> dict:
    """field -> {op, bytes}: what a patch would change, without a value."""
    out = {}
    for k, v in patch.items():
        if v is None:
            out[k] = {"op": "remove"}
        else:
            had = isinstance(value, dict) and k in value
            if had and value.get(k) == v:
                continue
            out[k] = {"op": "replace" if had else "add",
                      "bytes": len(json.dumps(v, ensure_ascii=False).encode())}
    return out


def rank(ds: list, p: float) -> float:
    """The nearest-rank quantile of sorted `ds`: 20 calls' p95 is the second slowest."""
    return ds[max(0, math.ceil(p * len(ds)) - 1)]


# ---------------------------------------------------------------------------------- the breaker
class Breaker:
    def __init__(self, clock):
        self.clock = clock
        self.state = "closed"
        self.fails = 0
        self.open_until = 0.0
        self.opened = 0
        self.probing = False
        self.window: collections.deque = collections.deque()      # (t, duration_ms)
        self.lock = threading.Lock()

    def allow(self) -> str | None:
        """'call', 'probe' (the one call of a half-open breaker), or None (open)."""
        with self.lock:
            if self.state == "closed":
                return "call"
            if self.state == "open" and self.clock() >= self.open_until:
                self.state = "half_open"
                self.probing = False
            if self.state == "half_open" and not self.probing:
                self.probing = True
                return "probe"
            return None

    def record(self, ok: bool, duration_ms: float, timeout_ms: float, probe: bool) -> None:
        with self.lock:
            now = self.clock()
            if probe:
                self.probing = False
                if ok:
                    self.state, self.fails = "closed", 0
                    self.window.clear()
                else:
                    self._open(now)
                return
            if self.state != "closed":
                return
            self.window.append((now, duration_ms))
            while self.window and now - self.window[0][0] > BREAKER_WINDOW_S:
                self.window.popleft()
            self.fails = 0 if ok else self.fails + 1
            if self.fails >= BREAKER_FAILS:
                self._open(now)
                return
            if len(self.window) >= BREAKER_MIN_SAMPLES and \
                    rank(sorted(d for _, d in self.window), 0.95) >= timeout_ms:
                self._open(now)

    def _open(self, now: float) -> None:
        self.state, self.open_until, self.probing = "open", now + BREAKER_OPEN_S, False
        self.opened += 1
        self.fails = 0
        self.window.clear()

    def view(self) -> dict:
        with self.lock:
            st = self.state
            if st == "open" and self.clock() >= self.open_until:
                st = "half_open"
            left = max(0.0, self.open_until - self.clock()) if st == "open" else 0.0
            return {"state": st, "consecutive_failures": self.fails,
                    "open_for_s": round(left, 1), "opened": self.opened}


# ---------------------------------------------------------------------------------- off the path
class Background:
    """Async observers and shadows: a bounded queue and a few workers. A full queue drops."""

    def __init__(self, workers: int = 4, size: int = 2000):
        self.q: queue.Queue = queue.Queue(maxsize=size)
        self.n = workers
        self.started = False
        self.dropped = 0
        self.lock = threading.Lock()

    def submit(self, fn) -> bool:
        if not self.started:
            with self.lock:
                if not self.started:
                    for i in range(self.n):
                        threading.Thread(target=self._work, name=f"hook-bg-{i}",
                                         daemon=True).start()
                    self.started = True
        try:
            self.q.put_nowait(fn)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _work(self) -> None:
        while True:
            fn = self.q.get()
            try:
                if fn is None:
                    return
                fn()
            except Exception:                                      # noqa: BLE001
                pass
            finally:
                self.q.task_done()

    def drain(self, timeout: float = 10.0) -> bool:
        end = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)
        return not self.q.unfinished_tasks


class Stats:
    def __init__(self):
        self.calls = self.failures = self.timeouts = self.skipped = 0
        self.outcomes: collections.Counter = collections.Counter()
        self.durations: collections.deque = collections.deque(maxlen=SAMPLES_MAX)
        self.journal: collections.deque = collections.deque(maxlen=JOURNAL_MAX)
        self.lock = threading.Lock()

    def view(self) -> dict:
        with self.lock:
            ds = sorted(self.durations)
            q = (lambda p: round(rank(ds, p), 1) if ds else None)
            return {"calls": self.calls, "failures": self.failures, "timeouts": self.timeouts,
                    "skipped": self.skipped, "outcomes": dict(self.outcomes),
                    "p50_ms": q(0.5), "p95_ms": q(0.95)}


class Call:
    """One call's end: outcome (ok, timeout, error, invalid, cap, breaker_open,
    redaction_failed), the checked result, the duration and our own words for a failure."""
    __slots__ = ("outcome", "result", "ms", "error")

    def __init__(self, outcome, result=None, ms=0.0, error=None):
        self.outcome, self.result, self.ms, self.error = outcome, result, ms, error

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def action(self) -> str | None:
        return self.result.get("action") if self.result else None


class StageResult:
    """What a stage's hooks decided: the value (patched or not), `changed`, `deny` (the first
    guard's, or a closed policy's), `decision` ({hook, ...}), the labels the hooks gave."""

    def __init__(self, value):
        self.value = value
        self.changed = False
        self.deny: dict | None = None
        self.decision: dict | None = None
        self.decided_by: str | None = None
        self.labels: dict = {}


# ---------------------------------------------------------------------------------- the runner
class Runner:
    def __init__(self, cfg, log=None, clock=time.monotonic):
        self.cfg = cfg
        self.log = log or (lambda m: None)
        self.clock = clock
        self.builtins = dict(BUILTINS)
        self.store = HookStore(cfg, builtins=self.builtins)
        self._hooks: list[dict] = []
        self._gen = -1
        self._dv = None
        self._checked = 0.0
        self._lock = threading.Lock()
        self._secrets: dict = {}
        self.pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="hook")
        self.bg = Background()
        self.breakers: dict[str, Breaker] = {}
        self.stats: dict[str, Stats] = {}
        self.redactor = None            # tests put a failing one here (SEC-16 redaction failure)
        self.decision_provider = None   # E2's seam: fn(question, ctx) -> answer | None

    def stop(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ the registry, cached
    def hooks(self) -> list[dict]:
        """The enabled hooks whose mode is not off, by (priority, id); reloaded when the registry
        changed here, or (checked every 2 s) in another process."""
        now = time.monotonic()
        dv = None
        if now - self._checked >= RELOAD_CHECK_S:
            self._checked = now
            dv = self.store.data_version()
        if self.store.gen == self._gen and (dv is None or dv == self._dv):
            return self._hooks
        with self._lock:
            try:
                rows = self.store.list() if self.store.ready() else []
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[hooks] the registry could not be read: {type(exc).__name__}")
                rows = []
            self._hooks = [h for h in rows if h["enabled"] and h["mode"] != "off"]
            self._secrets = {}
            self._gen = self.store.gen
            if dv is not None:
                self._dv = dv
        return self._hooks

    def idle(self) -> bool:
        return not self.hooks()

    def builtin(self, name: str, fn) -> None:
        """Register a builtin (a test's own, or code that ships later)."""
        self.builtins[name] = fn

    def _breaker(self, hid: str) -> Breaker:
        b = self.breakers.get(hid)
        if b is None:
            b = self.breakers.setdefault(hid, Breaker(self.clock))
        return b

    def _stats(self, hid: str) -> Stats:
        s = self.stats.get(hid)
        if s is None:
            s = self.stats.setdefault(hid, Stats())
        return s

    @staticmethod
    def in_scope(h: dict, ctx: dict) -> bool:
        sc = h.get("scope") or {}
        for key, vals in (("models", (ctx.get("model"), ctx.get("upstream_model"))),
                          ("backends", (ctx.get("backend"),)),
                          ("profiles", (ctx.get("profile"),)),
                          ("users", (ctx.get("user"),)),
                          ("sessions", (ctx.get("session"),)),
                          ("client_kinds", (ctx.get("client_kind"),))):
            pats = sc.get(key)
            if not pats:
                continue
            if not any(isinstance(v, str) and fnmatch.fnmatchcase(v, p)
                       for v in vals for p in pats):
                return False
        return True

    def for_stage(self, stage: str, ctx: dict) -> list[dict]:
        return [h for h in self.hooks() if stage in h["stages"] and self.in_scope(h, ctx)]

    def policy(self, h: dict, stage: str) -> str:
        return h["policy"] or S.default_policy(stage, h["role"])

    # ------------------------------------------------------------------ one call
    def _envelope(self, h, stage, ctx, meta, value, mode) -> dict:
        ev = hookcall.event_at(h["payload"], meta, value, self.redactor)
        return {"hooks": S.VERSION, "id": "hd-" + uuid.uuid4().hex, "hook": h["id"],
                "stage": stage, "ts": round(time.time(), 3), "payload": h["payload"],
                "mode": mode, "request": {k: ctx.get(k) for k in CTX_KEYS}, "event": ev}

    def _secrets_of(self, h: dict) -> list[str]:
        key = (h["id"], h["secret_ref"], h["version"])
        got = self._secrets.get(key)
        if got is None or got[1] < time.monotonic():
            vals = self.store.secrets_for(h["id"], h["secret_ref"])
            self._secrets[key] = got = (vals, time.monotonic() + 30.0)
        return got[0]

    def _invoke(self, h: dict, env: dict, timeout_s: float) -> dict:
        """The call itself, on a pool thread: the raw result (unchecked)."""
        if h["kind"] == "builtin":
            fn = self.builtins.get(h["target"])
            if fn is None:
                raise hookcall.CallError("error", f"no builtin {h['target']!r}")
            # a copy: code in this process changes the request only through its answer
            return fn(copy.deepcopy(env), copy.deepcopy(h.get("options") or {}))
        if h["kind"] != "http":
            raise hookcall.CallError("error", f"kind {h['kind']!r} is not available in this "
                                              f"version")
        body = json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode()
        ts = int(time.time())
        hdrs = [("Content-Type", "application/json"), ("User-Agent", hookcall.USER_AGENT),
                (hookcall.HOOK_HEADER, h["id"]), (hookcall.DELIVERY_HEADER, env["id"]),
                (hookcall.TS_HEADER, str(ts))]
        secrets = self._secrets_of(h)
        if secrets:
            hdrs.append((hookcall.SIG_HEADER, hookcall.sign(secrets, ts, body)))
        g = h.get("grant") or {}
        need_lan = h["payload"] == "full" and bool(g.get("lan", True))
        status, data = hookcall.post(h["endpoint"], body, hdrs, timeout_s, need_lan=need_lan)
        if status == 204 or (status == 200 and not data.strip()):
            return {"action": "continue"}
        if status != 200:
            raise hookcall.CallError(f"http_{status}", f"the hook answered HTTP {status}")
        try:
            return json.loads(data)
        except ValueError:
            raise hookcall.CallError("bad_json", "the answer is not JSON")

    def call(self, h: dict, stage: str, ctx: dict, meta: dict, value, *, timeout_ms: float,
             mode: str, count: bool = True, streamed: bool = False) -> Call:
        """Call `h` once within `timeout_ms`, check its result, keep its figures (`count`: the
        breaker and the metrics; a test call keeps neither). A shadow call's diff is journaled;
        `streamed`: the answer went out already, so anything but continue is `refused`. Never
        raises."""
        hid = h["id"]
        own_ms = self._timeout_of(h, stage)
        br = self._breaker(hid)
        gate = br.allow() if count else "call"
        if gate is None:
            return self._end(h, stage, ctx, mode, Call("breaker_open", error="the breaker is "
                             "open"), count)
        if timeout_ms <= 0:
            if gate == "probe":
                br.probing = False
            return self._end(h, stage, ctx, mode, Call("cap", error="no time left before the "
                             "first token"), count)
        try:
            env = self._envelope(h, stage, ctx, meta, value, mode)
        except hookcall.RedactionFailed:
            if gate == "probe":
                br.probing = False
            return self._end(h, stage, ctx, mode, Call("redaction_failed", error="the "
                             "redaction failed; the hook got no payload"), count)
        t0 = time.monotonic()
        fut = self.pool.submit(self._invoke, h, env, timeout_ms / 1000.0)
        try:
            raw = fut.result(timeout=timeout_ms / 1000.0)
            ms = (time.monotonic() - t0) * 1000
            res, why = check_result(stage, h["role"], h["payload"], raw)
            c = Call("ok", res, ms) if why is None else Call("invalid", None, ms, why)
        except FutureTimeout:
            fut.cancel()
            ms = (time.monotonic() - t0) * 1000
            cut = timeout_ms < own_ms
            c = Call("cap" if cut else "timeout", None, ms,
                     f"no answer within {timeout_ms:.0f} ms" + (" (the request's budget)"
                                                                if cut else ""))
        except hookcall.CallError as exc:
            ms = (time.monotonic() - t0) * 1000
            c = Call("timeout" if exc.code == "timeout" else "error", None, ms,
                     exc.message if exc.code == "timeout" else f"{exc.code}: {exc.message}")
        except Exception as exc:                                   # noqa: BLE001
            ms = (time.monotonic() - t0) * 1000
            c = Call("error", None, ms, f"{type(exc).__name__}")
        if count:
            if c.outcome == "cap":
                if gate == "probe":
                    br.probing = False
            else:
                br.record(c.ok, c.ms, own_ms, gate == "probe")
        diff = None
        if mode == "shadow" and c.ok and c.action != "continue":
            diff = (diff_of(value, c.result["patch"]) if c.action == "modify" else
                    {"deny": True} if c.action == "deny" else
                    {"decision": _decision_view(c.result["decision"])})
        if streamed and c.ok and c.action != "continue":
            c = Call("refused", c.result, c.ms, "the answer went out as a stream: nothing "
                     "applied")
        return self._end(h, stage, ctx, mode, c, count, diff)

    def _timeout_of(self, h: dict, stage: str) -> float:
        b = S.STAGES[stage]["budget_ms"]
        if b is None:
            return float(h["timeout_ms"] or S.ASYNC_TIMEOUT_MS)
        return float(min(h["timeout_ms"] or b, b))

    def _end(self, h, stage, ctx, mode, c: Call, count: bool, diff=None) -> Call:
        st = self._stats(h["id"])
        entry = {"ts": int(time.time() * 1000), "stage": stage,
                 "request": ctx.get("request_id"), "mode": mode, "outcome": c.outcome,
                 "action": c.action, "duration_ms": round(c.ms, 1),
                 "breaker": self._breaker(h["id"]).view()["state"]}
        if c.result:
            if isinstance(c.result.get("reason"), str):
                entry["reason"] = c.result["reason"][:REASON_MAX]
            if c.result.get("labels"):
                entry["labels"] = c.result["labels"]
            if c.result.get("decision") is not None:
                entry["decision"] = _decision_view(c.result["decision"])
            if c.result.get("patch") is not None:
                entry["fields"] = sorted(c.result["patch"])
        if c.error:
            entry["error"] = c.error[:REASON_MAX]
        if diff is not None:
            entry["diff"] = diff
        with st.lock:
            st.journal.append(entry)
            if count:
                if c.outcome in SKIPS:
                    st.skipped += 1
                else:
                    st.calls += 1
                    st.durations.append(c.ms)
                if c.outcome in HOOK_FAILURES:
                    st.failures += 1
                if c.outcome == "timeout":
                    st.timeouts += 1
                st.outcomes[c.outcome if not c.ok else (c.action or "continue")] += 1
        if c.outcome == "refused" and c.result is not None:
            entry["action"] = c.result.get("action")
        return c

    # ------------------------------------------------------------------ a stage
    def run(self, stage: str, ctx: dict, value, meta_of, *, apply=merge_patch, turn=None,
            hooks=None) -> StageResult:
        """Run `stage`'s hooks over `value` (see THE RULES). `meta_of(value)` is the meta event
        of a value; `apply(value, patch)` a patched copy. `turn` carries the request's budgets."""
        spec = S.STAGES[stage]
        out = StageResult(value)
        hs = self.for_stage(stage, ctx) if hooks is None else hooks
        if not hs:
            return out
        if not spec["sync"]:
            meta = meta_of(value)
            for h in hs:
                self._observe(h, stage, ctx, meta, value)
            return out
        by_role = {r: [h for h in hs if h["role"] == r] for r in S.ROLES}
        # transforms chain; a shadow transform sees the value at its place in the chain
        for h in by_role["transform"]:
            if h["mode"] == "shadow":
                self._shadow(h, stage, ctx, meta_of, copy.deepcopy(out.value))
                continue
            c = self.call(h, stage, ctx, meta_of(out.value), out.value,
                          timeout_ms=self._left(h, stage, turn), mode="on")
            if turn is not None:
                turn.spend(stage, c.ms)
            self._labels(out, c)
            if not c.ok:
                if self._fail(out, h, stage, c):
                    return out
                continue
            if c.action == "modify":
                try:
                    out.value = apply(out.value, c.result["patch"])
                    out.changed = True
                except Exception as exc:                           # noqa: BLE001
                    self._journal_note(h, stage, ctx, "invalid", f"the patch does not apply: "
                                       f"{type(exc).__name__}")
        # guards: any deny wins
        for h in by_role["guard"]:
            if h["mode"] == "shadow":
                self._shadow(h, stage, ctx, meta_of, copy.deepcopy(out.value))
                continue
            c = self.call(h, stage, ctx, meta_of(out.value), out.value,
                          timeout_ms=self._left(h, stage, turn), mode="on")
            if turn is not None:
                turn.spend(stage, c.ms)
            self._labels(out, c)
            if not c.ok:
                if self._fail(out, h, stage, c):
                    return out
                continue
            if c.action == "deny":
                out.deny = {"hook": h["id"], "status": 403, "code": "hook_denied",
                            "message": (c.result.get("reason") or
                                        f"refused by the hook {h['name']!r}")[:REASON_MAX]}
                return out
        # deciders: the first decide wins
        for h in by_role["decider"]:
            if h["mode"] == "shadow":
                self._shadow(h, stage, ctx, meta_of, copy.deepcopy(out.value))
                continue
            if out.decision is not None:
                continue
            c = self.call(h, stage, ctx, meta_of(out.value), out.value,
                          timeout_ms=self._left(h, stage, turn), mode="on")
            if turn is not None:
                turn.spend(stage, c.ms)
            self._labels(out, c)
            if not c.ok:
                if self._fail(out, h, stage, c):
                    return out
                continue
            if c.action == "decide":
                out.decision, out.decided_by = dict(c.result["decision"]), h["id"]
        # observers: the final value, off the path
        if by_role["observer"]:
            final = copy.deepcopy(out.value)
            meta = meta_of(final)
            for h in by_role["observer"]:
                self._observe(h, stage, ctx, meta, final)
        return out

    def _left(self, h: dict, stage: str, turn) -> float:
        own = self._timeout_of(h, stage)
        if turn is None:
            return own
        return min(own, turn.left(stage))

    def _labels(self, out: StageResult, c: Call) -> None:
        if c.ok and c.result.get("labels"):
            out.labels.update(c.result["labels"])

    def _fail(self, out: StageResult, h: dict, stage: str, c: Call) -> bool:
        """A failed sync hook: True when its closed policy refuses the request."""
        if self.policy(h, stage) != "closed":
            return False
        out.deny = {"hook": h["id"], "status": 503, "code": "hook_unavailable",
                    "message": f"the hook {h['name']!r} did not answer ({c.outcome}) and its "
                               f"policy is closed"}
        return True

    def _journal_note(self, h, stage, ctx, outcome, error) -> None:
        self._end(h, stage, ctx, "on", Call(outcome, error=error), False)

    def _observe(self, h: dict, stage: str, ctx: dict, meta: dict, value,
                 streamed: bool = False) -> None:
        """An observer, off the request's path: one retry, a failure dropped."""
        mode = "shadow" if h["mode"] == "shadow" else "on"
        ctx = dict(ctx)

        def go():
            t = self._timeout_of(h, stage)
            c = self.call(h, stage, ctx, meta, value, timeout_ms=t, mode=mode,
                          streamed=streamed)
            if c.outcome in HOOK_FAILURES:
                self.call(h, stage, ctx, meta, value, timeout_ms=t, mode=mode,
                          streamed=streamed)
        if not self.bg.submit(go):
            self._end(h, stage, ctx, mode, Call("dropped", error="the observers' queue is "
                                                                 "full"), False)

    def _shadow(self, h: dict, stage: str, ctx: dict, meta_of, value) -> None:
        """A shadow hook: called off the path on `value` (a copy), its result and diff
        journaled, nothing applied."""
        ctx = dict(ctx)

        def go():
            self.call(h, stage, ctx, meta_of(value), value,
                      timeout_ms=self._timeout_of(h, stage), mode="shadow")
        if not self.bg.submit(go):
            self._end(h, stage, ctx, "shadow", Call("dropped", error="the observers' queue is "
                                                                     "full"), False)

    # ------------------------------------------------------------------ seams for later work
    def ask(self, ctx: dict, question: dict, content=None) -> dict | None:
        """decision.ask (E2's seam): the first decider's decision for `question`, or None (the
        built-in answer)."""
        r = self.run("decision.ask", ctx, content, lambda v: {"question": question})
        return r.decision

    def notify(self, stage: str, ctx: dict, meta: dict, content=None) -> None:
        """An async stage's observers (request.finished, engine.lifecycle, ...)."""
        self.run(stage, ctx, content, lambda v: meta)

    def turn(self, ctx: dict) -> "Turn | None":
        """The request's hook turn, or None when no hook is enabled (the relay as before)."""
        if self.idle():
            return None
        return Turn(self, ctx)

    # ------------------------------------------------------------------ the admin's views
    def test(self, hid: str, stage: str | None, event: dict | None) -> dict:
        """The playground's call: the hook once against `event` (the stage's example by
        default), outside its scope, mode and breaker; nothing counted, the call journaled with
        mode `test`."""
        h = self.store.need(hid)
        stage = stage or h["stages"][0]
        if stage not in h["stages"]:
            raise HookError(422, "invalid", f"the hook does not run at {stage}", "stage")
        from proxy import hooksample
        sample = hooksample.sample(stage)
        meta = dict(sample["meta"])
        content = sample.get("content")
        if isinstance(event, dict):
            content = event.pop("content", content)
            meta.update(event)
        ctx = dict(sample["request"], request_id="test-" + uuid.uuid4().hex[:12])
        c = self.call(h, stage, ctx, meta, content, timeout_ms=self._timeout_of(h, stage),
                      mode="test", count=False)
        out = {"hook": hid, "stage": stage, "outcome": c.outcome,
               "duration_ms": round(c.ms, 1), "result": c.result, "error": c.error}
        if c.ok and c.action == "modify" and isinstance(content, dict):
            out["diff"] = diff_of(content, c.result["patch"])
        return out

    def calls(self, hid: str, limit: int = 100) -> list[dict]:
        st = self._stats(hid)
        with st.lock:
            return list(st.journal)[-limit:][::-1]

    def metrics(self, hid: str) -> dict:
        out = self._stats(hid).view()
        out["breaker"] = self._breaker(hid).view()
        return out

    def families(self, registry) -> None:
        """The hooks' Prometheus families on the controller's /metrics page."""
        from tandemapp import metrics as m

        def per(fn):
            def collect():
                return {(hid,): float(fn(st)) for hid, st in list(self.stats.items())}
            return collect
        registry.add(m.Counter("qse_hook_calls_total", "calls of a hook that reached it, by "
                               "hook id", ("hook",), collect=per(lambda s: s.calls)))
        registry.add(m.Counter("qse_hook_failures_total", "calls of a hook that failed "
                               "(timeout, error, an invalid result), by hook id", ("hook",),
                               collect=per(lambda s: s.failures)))
        registry.add(m.Counter("qse_hook_timeouts_total", "calls of a hook that ran out of "
                               "time, by hook id", ("hook",), collect=per(lambda s: s.timeouts)))

        def quant():
            out = {}
            for hid, st in list(self.stats.items()):
                v = st.view()
                for q, k in (("0.5", "p50_ms"), ("0.95", "p95_ms")):
                    if v[k] is not None:
                        out[(hid, q)] = v[k]
            return out
        registry.add(m.Gauge("qse_hook_latency_ms", "a hook's call time over its last 512 "
                             "calls, by hook id and quantile", ("hook", "quantile"),
                             collect=quant))

        def breaker():
            return {(hid, s): float(b.view()["state"] == s)
                    for hid, b in list(self.breakers.items())
                    for s in ("closed", "open", "half_open")}
        registry.add(m.Gauge("qse_hook_breaker", "1 for a hook's breaker state now", ("hook",
                             "state"), collect=breaker))


CTX_KEYS = ("request_id", "endpoint", "model", "upstream_model", "backend", "profile", "user",
            "session", "client_kind", "stream")


def _decision_view(d: dict) -> dict:
    """A decision as the journal keeps it: its ids, never an answer's text."""
    out = {}
    for k, v in d.items():
        if k in ("backend", "model", "profile", "retry"):
            out[k] = v
        elif k == "fallback" and isinstance(v, dict):
            out[k] = {x: v[x] for x in ("backend", "model") if x in v}
        else:
            out[k] = "set"
    return out


# ---------------------------------------------------------------------------------- a request
class Turn:
    """One request's hooks: its context, the budgets left, the stages' outcomes. The relay calls
    it at each wired stage (proxy/service.py, proxy/relay.py)."""

    def __init__(self, runner: Runner, ctx: dict):
        self.r = runner
        self.ctx = ctx
        self.spent = 0.0                    # sync hook time before the first token
        self.stage_spent: dict[str, float] = {}
        self.first_out = False
        self.retried = False
        self.labels: dict = {}
        self._before = None                 # request.before_engine's outcome, kept for retries
        self._has: dict[str, bool] = {}
        self.attempts = 0
        self.chunks = 0
        self.stream_bytes = 0
        self.tool_names: dict[int, str] = {}

    # budgets
    def left(self, stage: str) -> float:
        spec = S.STAGES[stage]
        left = float(spec["budget_ms"]) - self.stage_spent.get(stage, 0.0)
        if spec["first_token"] and not self.first_out:
            left = min(left, S.CAP_MS - self.spent)
        return left

    def spend(self, stage: str, ms: float) -> None:
        self.stage_spent[stage] = self.stage_spent.get(stage, 0.0) + ms
        if S.STAGES[stage]["first_token"] and not self.first_out:
            self.spent += ms

    def _run(self, stage, value, meta_of, **kw) -> StageResult:
        r = self.r.run(stage, self.ctx, value, meta_of, turn=self, **kw)
        self.labels.update(r.labels)
        return r

    def has(self, stage: str) -> bool:
        """Any hook in scope at `stage` (asked once per stage and request: the route's context
        is set before the relay asks)."""
        got = self._has.get(stage)
        if got is None:
            got = self._has[stage] = bool(self.r.for_stage(stage, self.ctx))
        return got

    # request.received
    def received(self, body: dict, raw: bytes) -> tuple[dict, bytes, dict | None]:
        if not self.has("request.received"):
            return body, raw, None
        n = len(raw)
        r = self._run("request.received", body,
                      lambda b: hookcall.request_meta(b, n if b is body else None))
        if r.deny is not None:
            return body, raw, r.deny
        if r.changed:
            body = r.value
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.ctx["model"] = body.get("model") if isinstance(body.get("model"), str) \
                else self.ctx.get("model")
        return body, raw, None

    # route.select
    def select(self, route, backends: list[str]) -> tuple[dict | None, dict | None]:
        """(the decision, a deny)."""
        self._has.clear()                   # request.received may have changed the model
        if not self.has("route.select"):
            return None, None
        view = {"route": {"backend": route.backend, "model": route.model,
                          "fallbacks": [{"backend": b, "model": m} for b, m in route.fallbacks]},
                "backends": backends}
        r = self._run("route.select", None, lambda v: view)
        return r.decision, r.deny

    # request.before_engine
    def before_engine(self, body: dict, backend: str, model: str
                      ) -> tuple[dict, bool, dict | None]:
        """(the body, whether it changed, a deny). The hooks run once per request; a retry or a
        fallback gets the same patches again."""
        attempt = self.attempts
        self.attempts += 1
        if self._before is None:
            self._has.clear()               # route.select may have changed the route
            if not self.has("request.before_engine"):
                self._before = ([], None)
            else:
                patches: list = []
                rt = {"backend": backend, "model": model, "attempt": attempt}

                def keep(v, p):
                    patches.append(p)
                    return merge_patch(v, p)
                r = self._run("request.before_engine", body,
                              lambda b: dict(hookcall.request_meta(b), route=rt), apply=keep)
                self._before = (patches if r.changed else [], r.deny)
        patches, deny = self._before
        if deny is not None:
            return body, False, deny
        if not patches:
            return body, False, None
        for p in patches:
            body = merge_patch(body, p)
        return body, True, None

    # stream.chunk
    def watches_stream(self) -> bool:
        """The relay reads each event for the hooks: stream.chunk observers, or an answer stage
        that wants the tool names after the stream."""
        return self.has("stream.chunk") or self.has("tool_call.detected") or \
            self.has("response.after_engine")

    def see(self, event: bytes) -> None:
        index = self.chunks
        self.chunks += 1
        self.stream_bytes += len(event)
        data = None
        for line in event.split(b"\n"):
            if line.startswith(b"data:"):
                data = line[5:].strip()
        kind, obj = "other", None
        if data == b"[DONE]":
            kind = "done"
        elif data:
            try:
                obj = json.loads(data)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                kind = _chunk_kind(obj)
                if kind == "tool":
                    for tc in obj["choices"][0]["delta"].get("tool_calls") or []:
                        f = (tc or {}).get("function") or {}
                        if isinstance(f.get("name"), str) and isinstance(tc.get("index", 0), int):
                            self.tool_names.setdefault(tc.get("index", 0), f["name"][:128])
        if self.has("stream.chunk"):
            meta = {"index": index, "kind": kind, "bytes": len(event)}
            self.r.run("stream.chunk", self.ctx, obj, lambda v: meta)

    # the whole answer (not streamed)
    def answer(self, data: bytes, status: int, path: str) -> tuple[bytes, dict | None, bool]:
        """(the answer's bytes, a deny, whether one retry is asked)."""
        want_calls = self.has("tool_call.detected")
        want_answer = self.has("response.after_engine")
        if not (want_calls or want_answer):
            return data, None, False
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            return data, None, False
        changed = False
        msg = _first_message(obj)
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if want_calls and isinstance(calls, list) and calls:
            r = self._run("tool_call.detected", calls,
                          lambda v: {"calls": hookcall.calls_meta(v), "streamed": False},
                          apply=lambda v, p: p["tool_calls"])
            if r.deny is not None:
                return data, r.deny, False
            if r.changed:
                msg["tool_calls"] = r.value
                changed = True
        retry = False
        if want_answer:
            n = len(data)
            r = self._run("response.after_engine", obj,
                          lambda v: hookcall.answer_meta(
                              v, status, n if (v is obj and not changed) else len(
                                  json.dumps(v).encode()), False))
            if r.deny is not None:
                return data, r.deny, False
            if r.changed:
                obj, changed = r.value, True
            if r.decision and r.decision.get("retry"):
                if self.retried:
                    self.r._journal_note({"id": r.decided_by, "name": r.decided_by},
                                         "response.after_engine", self.ctx, "refused",
                                         "one retry per request; the answer goes as it is")
                else:
                    self.retried = retry = True
        if changed:
            data = json.dumps(obj, ensure_ascii=False).encode()
        return data, None, retry

    # a streamed answer, after it went out: observers only
    def stream_done(self, status: int, finish: str | None, usage) -> None:
        self.first_out = True
        if not (self.has("tool_call.detected") or self.has("response.after_engine")):
            return
        tool_names = [self.tool_names[i] for i in sorted(self.tool_names)]
        u = None
        if isinstance(usage, dict):
            u = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                       "input_tokens", "output_tokens")
                 if isinstance(usage.get(k), int)}
        ans = {"status": int(status), "finish_reason": finish, "streamed": True, "usage": u,
               "sizes": {"body_bytes": self.stream_bytes, "content_chars": None,
                         "reasoning_chars": None, "tool_calls": len(tool_names)},
               "tool_names": tool_names}
        for stage, meta in (("tool_call.detected", {"calls": [
                {"index": i, "id": None, "name": n, "args_bytes": None}
                for i, n in enumerate(tool_names)], "streamed": True}),
                ("response.after_engine", ans)):
            if stage == "tool_call.detected" and not tool_names:
                continue
            for h in self.r.for_stage(stage, self.ctx):
                # the answer went out already: what a hook says is journaled, nothing applied
                self.r._observe(h, stage, self.ctx, meta, None, streamed=True)

    # request.error
    def error(self, status: int, code: str | None, backend: str, model: str, attempt: int,
              left: list) -> tuple[str, str | None] | None:
        """A fallback (backend, model or None) a decider chose, or None."""
        if not self.has("request.error"):
            return None
        meta = {"error": {"status": int(status), "code": code}, "route": {"backend": backend,
                "model": model}, "attempt": attempt,
                "candidates_left": [{"backend": b, "model": m} for b, m in left]}
        r = self._run("request.error", None, lambda v: meta)
        fb = (r.decision or {}).get("fallback")
        if isinstance(fb, dict):
            return fb["backend"], fb.get("model")
        return None

    # request.finished
    def finished(self, rec) -> None:
        if not self.has("request.finished"):
            return
        meta = {"row": {"status": rec.status, "finish_reason": rec.finish_reason,
                        "error_type": rec.error_type, "backend": rec.backend,
                        "backend_model": rec.backend_model,
                        "tokens": {"prompt": rec.prompt_tokens,
                                   "completion": rec.completion_tokens,
                                   "cached": rec.cached_tokens,
                                   "reasoning": rec.reasoning_tokens},
                        "cost_micros": rec.cost_micros,
                        "ttft_ms": _num(getattr(rec, "ttft_ms", None)),
                        "retries": rec.retries, "fallback_from": rec.fallback_from},
                "labels": dict(self.labels)}
        self.r.notify("request.finished", self.ctx, meta)


def _num(v):
    return round(float(v), 1) if isinstance(v, (int, float)) else None


def _first_message(obj: dict):
    ch = obj.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        m = ch[0].get("message")
        return m if isinstance(m, dict) else None
    return None


def _chunk_kind(obj: dict) -> str:
    if obj.get("error") is not None:
        return "error"
    ch = obj.get("choices")
    if isinstance(ch, list) and ch and isinstance(ch[0], dict):
        d = ch[0].get("delta") or {}
        if d.get("tool_calls"):
            return "tool"
        if d.get("reasoning_content") or d.get("reasoning"):
            return "reasoning"
        if d.get("content") or ch[0].get("text"):
            return "content"
        if ch[0].get("finish_reason"):
            return "finish"
        return "other"
    if isinstance(ch, list) and not ch and obj.get("usage") is not None:
        return "usage"
    if isinstance(obj.get("type"), str):
        return "event"
    return "other"
