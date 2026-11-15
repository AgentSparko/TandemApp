"""The engine settings scope and the restarts it schedules (SRV-845, proxy/settings.py).

ONE TARGET A PAIRED ENGINE HOST (transport ssh; its id is the fleet node's). The store is the
engine's managed overlay, reached through `tandem remote settings get|plan|apply|versions|restore`
(contracts/engine-ctl-v1) by EngineHostControl (proxy/enginectl.py). The engine owns the key
schema: each key's type, default, restart class (`hot`: the running server applies it; `engine`:
read at the start), whether it feeds the disk cache's fingerprint, whether the overlay may set it
(`managed`; a key that may hold a secret never is). This side marks a key whose name looks like a
secret (enginectl.SECRET_KEY) or that the engine says may hold one as secret: its value is never
read into an answer.

An engine reached over HTTP only (a TandemLLM backend whose host is not paired over SSH) is a target
too, `http:<backend id>`, read-only: its hot keys as GET /v1/engine/settings (engine API 1.9)
answers them, and why nothing is writable from here.

A PLAN is the engine's own `settings plan`: the effect on what serve.env resolves, the restart it
needs, the fingerprint keys (then the change set's second confirm), the engine's confirm token,
bound to the overlay version it read. A change of hot keys only applies through PATCH
/v1/engine/settings with the version as `base` (no restart). Any other change applies through
`settings apply --when idle|now|at=... --deadline S` (idle and 600 s by default): the engine writes
a job that waits, stops the engine, makes the new overlay version current and starts it, and puts
the last good version back when the start fails (its rollback, A1). A restore is `settings restore
N` the same way. Versions are the overlay's, `n` as the id.

THE RESTART (Restarts) follows the engine's job from the controller:

    scheduled   when=at: until that time (nothing drained yet)
    draining    the gateway answers 503 with Retry-After for the backends on that engine host
                (`svc.restart_drain_of`, proxy/relay.py) while other backends keep serving; the
                engine's job waits for idle, up to its deadline; the countdown is that deadline
    restarting  the job acts: stop, the new version, start
    warming     the job is done: one token through each backend on the host, then undrained
    done | failed | timeout | cancelled

A job that fails reports the engine's own message and what it restored (`restored`: the version
the engine runs again and whether it answers); a job that ran out of its deadline is `timeout`
(the engine changed nothing). Cancel (POST /v1/settings/restarts/{id}/cancel) is possible while
the job waits: the controller sends `start`, which on the engine host replaces a job that still
waits and finds the engine healthy, so nothing changes; once the job acts it is too late (409).
Every end is an audit row (`settings.restart`, `settings.cancel`) with the change set's id. The
drains live in this process: a controller restart ends them (the engine's job goes on by itself).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from proxy import enginectl
from proxy.settings import (DEFAULT_DEADLINE_S, Scope, SettingsError, change, digest, iso,
                            key_spec, part_plan)

POLL_S = 2.0
WARM_WAIT_S = 300.0
#: past the deadline, how long the controller waits for the job's start (the engine's
#: START_TIMEOUT_S is 2400 s) before it reports a timeout and undrains
START_BUDGET_S = 2700.0
KEEP = 50
HTTP_TIMEOUT_S = 5.0
TYPES = {"string": "string", "integer": "integer", "number": "number", "path": "path",
         "enum": "enum", "module": "module", "cidrs": "cidrs"}
FINAL = ("done", "failed", "timeout", "cancelled")


def is_secret(row: dict) -> bool:
    return bool(enginectl.SECRET_KEY.search(row.get("key", ""))) or \
        (row.get("why") or "") == "may hold a secret"


def ctl_error(exc: enginectl.CtlError) -> SettingsError:
    """An engine control error as a settings refusal (its message is enginectl's, scrubbed)."""
    msg = exc.message or exc.kind
    if isinstance(exc, enginectl.HostKeyChanged):
        return SettingsError(409, "host_key_changed", "the engine host offers another SSH host "
                                                      "key than the pinned one: nothing ran",
                             pinned=exc.pinned, offered=exc.offered)
    if isinstance(exc, enginectl.CtlTransport):
        return SettingsError(502, "engine_unreachable", msg, code=exc.code)
    if isinstance(exc, enginectl.CtlTimeout):
        return SettingsError(504, "engine_timeout", msg)
    if isinstance(exc, enginectl.CtlBadCall):
        return SettingsError(422, "bad_value", msg)
    if isinstance(exc, enginectl.CtlUsage):
        if exc.code in ("unknown_verb", "unknown_subcommand"):
            return SettingsError(502, "engine_too_old", msg, code=exc.code)
        return SettingsError(422, "unknown_key" if exc.code == "unknown_key" else "bad_value",
                             msg, code=exc.code)
    if isinstance(exc, enginectl.CtlRefused):
        if exc.code in ("key_not_managed", "not_in_overlay", "unknown_version", "key_unknown"):
            return SettingsError(422, "bad_value", msg, code=exc.code)
        if exc.code in ("confirm_mismatch", "confirm_required"):
            return SettingsError(409, "stale_plan", "the engine's overlay changed since the plan: "
                                                    "plan again", code=exc.code)
        return SettingsError(409, "engine_refused", msg, code=exc.code)
    if isinstance(exc, enginectl.CtlBusy):
        return SettingsError(409, "engine_busy", msg, code=exc.code)
    return SettingsError(502, "engine_failed", msg, code=exc.code)


def _value(key: str, v) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise SettingsError(422, "bad_value", f"{key}: an engine setting is a string or a number",
                            key=key)
    return str(v)


# ======================================================================================== the scope
class EngineScope(Scope):
    """`rows()`: the fleet's engine hosts over SSH; `ctl_for(row)`: an EngineHostControl;
    `http_only()`: [{id, label, backend}] of engines reached over HTTP only; `http_read(item)`:
    GET /v1/engine/settings of one; `hot_patch(row, body)`: (status, answer) of PATCH
    /v1/engine/settings on a paired host; `restarts`: the scheduler."""

    def __init__(self, *, rows, ctl_for, restarts, http_only=None, http_read=None,
                 hot_patch=None):
        self.rows = rows
        self.ctl_for = ctl_for
        self.restarts = restarts
        self.http_only = http_only or (lambda: [])
        self.http_read = http_read
        self.hot_patch = hot_patch

    # ------------------------------------------------------------------ targets
    def targets(self):
        out = [{"id": r["id"], "label": r.get("alias") or r["id"], "writable": True, "why": None,
                "transport": "ssh"} for r in self.rows()]
        out += [{"id": h["id"], "label": h.get("label") or h["id"], "writable": False,
                 "why": "reached over HTTP only: change its settings on its own host (tandem "
                        "remote settings), or pair it over SSH", "transport": "http"}
                for h in self.http_only()]
        return out

    def _row(self, target: str) -> dict:
        for r in self.rows():
            if r["id"] == target:
                return r
        raise SettingsError(409, "read_only", "this engine is reached over HTTP only: its "
                                              "settings change on its own host or after pairing "
                                              "it over SSH")

    @contextlib.contextmanager
    def _ctl(self, target: str):
        row = self._row(target)
        try:
            ctl = self.ctl_for(row)
        except enginectl.CtlError as exc:
            raise ctl_error(exc) from None
        try:
            yield ctl
        except enginectl.CtlError as exc:
            raise ctl_error(exc) from None
        finally:
            ctl.close()

    def _http(self, target: str) -> tuple[dict | None, str]:
        item = next((h for h in self.http_only() if h["id"] == target), None)
        why = ("reached over HTTP only: change its settings on its own host (tandem remote "
               "settings), or pair it over SSH")
        if item is None or self.http_read is None:
            return None, why
        try:
            return self.http_read(item), why
        except Exception as exc:                                     # noqa: BLE001
            return None, f"{why}; its hot settings could not be read ({type(exc).__name__})"

    # ------------------------------------------------------------------ reads
    def schema(self, target):
        if not self.target_info(target)["writable"]:
            doc, why = self._http(target)
            keys = [key_spec(k["key"], TYPES.get(k.get("type"), "string"), restart="hot",
                             writable=False, why="read-only over HTTP")
                    for k in (doc or {}).get("keys") or []]
            return {"writable": False, "why": why, "keys": keys}
        with self._ctl(target) as ctl:
            doc = ctl.settings_get()
        keys = []
        for r in doc["keys"]:
            secret = is_secret(r)
            keys.append(key_spec(r["key"], TYPES.get(r["type"], "string"),
                                 restart=r["restart"], fingerprint=bool(r["fingerprint"]),
                                 default=r.get("default"), values=r.get("values"),
                                 secret=secret, writable=bool(r["managed"]) and not secret,
                                 why=r.get("why")))
        return {"writable": bool(doc.get("hook", True)),
                "why": None if doc.get("hook", True) else "the engine's serve.env does not read "
                                                          "the overlay: run its installer again",
                "keys": keys}

    def get(self, target):
        if not self.target_info(target)["writable"]:
            doc, why = self._http(target)
            rows = [{"key": k["key"], "value": k.get("value"), "set": bool(k.get("set")),
                     "source": k.get("source") or "default"} for k in (doc or {}).get("keys") or []]
            ov = (doc or {}).get("overlay") or {}
            return {"writable": False, "why": why,
                    "version": str(ov["current"]) if "current" in ov else None, "values": rows}
        with self._ctl(target) as ctl:
            doc = ctl.settings_get()
        rows = []
        for r in doc["keys"]:
            secret = is_secret(r)
            rows.append({"key": r["key"], "value": None if secret else r.get("value"),
                         "set": bool(r["set"]), "source": r["source"]})
        why = None if doc.get("hook", True) else "the engine's serve.env does not read the overlay"
        if doc.get("config_error"):
            why = f"the engine's configuration does not load now: {doc['config_error'][:200]}"
        return {"writable": bool(doc.get("hook", True)), "why": why,
                "version": str(doc["overlay"]["current"]), "values": rows}

    def versions(self, target):
        if not self.target_info(target)["writable"]:
            raise SettingsError(409, "read_only", "this engine is reached over HTTP only: its "
                                                  "versions are read on its own host")
        with self._ctl(target) as ctl:
            doc = ctl.settings_versions()
        out = []
        for v in sorted(doc["versions"], key=lambda x: -x["n"]):
            vals = {k: x for k, x in (v.get("set") or {}).items()
                    if not enginectl.SECRET_KEY.search(k)}
            out.append({"id": str(v["n"]), "at": v.get("at"), "who": None,
                        "note": v.get("note") or "", "change_set": None,
                        "keys": sorted((v.get("set") or {}).keys()),
                        "current": v["n"] == doc["current"], "good": v["n"] == doc.get("good"),
                        "values": vals})
        return {"current": str(doc["current"]), "versions": out}

    # ------------------------------------------------------------------ plans
    def _plan(self, target: str, sets: dict, unsets: list, *, restore: int | None = None) -> dict:
        if not self.target_info(target)["writable"]:
            self._row(target)
        changes = {k: _value(k, v) for k, v in sets.items()}
        with self._ctl(target) as ctl:
            try:
                doc = ctl.settings_plan(changes, unset=list(unsets))
            except enginectl.CtlRefused as exc:
                if exc.code == "no_change":
                    return part_plan([], state=digest(["no_change", target, restore]))
                raise
        p = doc["plan"]
        shadowed = set(p.get("shadowed") or [])
        out = []
        for e in p["effect"]:
            secret = bool(enginectl.SECRET_KEY.search(e["key"]))
            out.append(change(e["key"], e["from"], e["to"], was_set=e["was_set"],
                              is_set=e["is_set"], restart=e["restart"],
                              fingerprint=bool(e["fingerprint"]), secret=secret))
        for o in p["overlay"]:
            if o["key"] in shadowed:
                out.append(change(o["key"], o["from"], o["to"], was_set=o["from"] is not None,
                                  is_set=o["to"] is not None, restart="engine",
                                  secret=bool(enginectl.SECRET_KEY.search(o["key"]))))
        hot_only = (bool(p["effect"]) and not shadowed and not p["restart_needed"]
                    and all(e["restart"] == "hot" for e in p["effect"]) and restore is None)
        warnings = list(p.get("warnings") or [])
        if not hot_only and not p["restart_needed"]:
            warnings.append("applied over SSH, this change restarts the engine all the same")
        native = {"base": p["base"], "confirm": p["confirm"],
                  "confirm_fingerprint": p.get("confirm_fingerprint"), "hot_only": hot_only,
                  "changes": changes, "unset": list(unsets), "restore": restore}
        return part_plan(out, state=digest([p["confirm"], p.get("confirm_fingerprint"),
                                            p["base"]]),
                         warnings=warnings, native=native,
                         restart="hot" if hot_only else "engine",
                         fingerprint_keys=p.get("fingerprint_keys") or [])

    def plan(self, target, sets, unsets):
        return self._plan(target, sets, unsets)

    def plan_restore(self, target, version):
        try:
            n = int(version)
        except (TypeError, ValueError):
            raise SettingsError(404, "no_version", f"no version {version!r} of this engine's "
                                                   f"overlay") from None
        if not self.target_info(target)["writable"]:
            self._row(target)
        with self._ctl(target) as ctl:
            doc = ctl.settings_versions()
        by = {v["n"]: v for v in doc["versions"]}
        if n not in by:
            raise SettingsError(404, "no_version", f"no version {n} of this engine's overlay")
        cur = (by.get(doc["current"]) or {}).get("set") or {}
        want = by[n].get("set") or {}
        sets = {k: v for k, v in want.items() if cur.get(k) != v}
        unsets = [k for k in cur if k not in want]
        if not sets and not unsets:
            return part_plan([], state=digest(["no_change", target, n, doc["current"]]))
        try:
            return self._plan(target, sets, unsets, restore=n)
        except SettingsError as exc:
            if exc.status == 422:
                raise SettingsError(422, "restore_invalid", f"version {n} is not valid now: "
                                                            f"{exc.message}") from None
            raise

    # ------------------------------------------------------------------ apply
    def _when(self, ctx: dict) -> tuple[str, int | None]:
        when = ctx.get("when") or "idle"
        deadline = ctx.get("deadline_s")
        if when != "now" and deadline is None:
            deadline = DEFAULT_DEADLINE_S
        return when, deadline

    def apply(self, target, plan, ctx):
        nat = plan["native"]
        row = self._row(target)
        base = str(nat["base"])
        restore = [{"scope": "engine", "target": target, "version": base}]
        if nat["hot_only"]:
            if self.hot_patch is None:
                raise SettingsError(409, "no_http", "a hot change goes through the engine's HTTP "
                                                    "route, which this controller cannot reach")
            try:
                st, doc = self.hot_patch(row, {"set": nat["changes"], "unset": nat["unset"],
                                               "base": nat["base"]})
            except SettingsError:
                raise
            except Exception as exc:                                 # noqa: BLE001
                raise SettingsError(502, "engine_unreachable", f"PATCH /v1/engine/settings: "
                                                               f"{type(exc).__name__}") from None
            if st != 200 or not doc.get("applied"):
                e = doc.get("error") if isinstance(doc.get("error"), dict) else {}
                kind = e.get("type") or f"http_{st}"
                if kind == "base_moved":
                    raise SettingsError(409, "stale_plan", "the engine's overlay changed since "
                                                           "the plan: plan again")
                raise SettingsError(409 if st in (400, 409) else 502, "engine_refused",
                                    f"PATCH /v1/engine/settings: {kind}: "
                                    f"{str(e.get('message') or '')[:200]}")
            return {"version_before": base, "version_after": str(doc.get("version")),
                    "restore": restore}
        when, deadline = self._when(ctx)
        fp = nat.get("confirm_fingerprint") if ctx.get("fingerprint_ok") else None
        with self._ctl(target) as ctl:
            if nat.get("restore") is not None:
                doc = ctl.settings_restore(nat["restore"], confirm=nat["confirm"],
                                           confirm_fingerprint=fp, when=when, deadline=deadline)
            else:
                doc = ctl.settings_apply(nat["changes"], unset=nat["unset"],
                                         confirm=nat["confirm"], confirm_fingerprint=fp,
                                         when=when, deadline=deadline)
        view = self.restarts.schedule(row, doc["job"], change_set=ctx["change_set"],
                                      who=ctx["who"], when=when, deadline_s=deadline,
                                      kind="restore" if nat.get("restore") is not None
                                      else "settings")
        return {"version_before": base, "version_after": None, "restore": restore,
                "restart": view}

    def restore(self, target, plan, ctx):
        return self.apply(target, plan, ctx)


# ======================================================================================== restarts
def _brief(job: dict | None) -> dict | None:
    if not isinstance(job, dict):
        return None
    return {k: job.get(k) for k in ("id", "kind", "state", "phase", "message", "waiting_for",
                                    "created_at", "ended_at")}


class Restarts:
    """The engine restarts settings changes scheduled. `ctl_for(row)`, `backends_of(host id)` ->
    [backend id], `warm(backend id, model)` -> (ok, why), `audit(who, action, obj_id, keys,
    change_set, result, detail)`."""

    def __init__(self, *, ctl_for, backends_of, warm, audit=None, log=None, clock=time.time,
                 sleep=time.sleep, poll_s: float = POLL_S, warm_wait_s: float = WARM_WAIT_S,
                 start_budget_s: float = START_BUDGET_S):
        self.ctl_for = ctl_for
        self.backends_of = backends_of
        self.warm = warm
        self.audit = audit or (lambda *a, **k: None)
        self.log = log or (lambda m: print(m, flush=True))
        self.clock, self.sleep = clock, sleep
        self.poll_s, self.warm_wait_s, self.start_budget_s = poll_s, warm_wait_s, start_budget_s
        self._items: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.on_end = None                     # (restart id, state): the settings' replay map

    # ------------------------------------------------------------------ the gateway's question
    def drained(self, bid: str) -> str | None:
        """The engine host's alias while `bid` runs on an engine a settings change drains."""
        with self._lock:
            for r in self._items.values():
                if r["draining"] and bid in r["backends"]:
                    return f"{r['alias']} (restarting for a settings change)"
        return None

    # ------------------------------------------------------------------ views
    def _view(self, r: dict) -> dict:
        now = self.clock()
        rem = None
        if r["state"] in ("scheduled", "draining") and r["deadline_at"] is not None:
            rem = max(0, int(round(r["deadline_at"] - now)))
        return {"id": r["id"], "host": r["host"], "alias": r["alias"],
                "change_set": r["change_set"], "kind": r["kind"], "who": r["who"],
                "state": r["state"], "message": r["message"], "when": r["when"],
                "deadline_s": r["deadline_s"], "deadline_at": iso(r["deadline_at"], ms=False),
                "remaining_s": rem, "created_at": iso(r["created_at"], ms=False),
                "ended_at": iso(r["ended_at"], ms=False), "job": r["job"],
                "backends": list(r["backends"]), "draining": r["draining"],
                "warm": r["warm"], "restored": r["restored"],
                "can_cancel": r["state"] in ("scheduled", "draining")}

    def list(self) -> list[dict]:
        with self._lock:
            return [self._view(r) for r in sorted(self._items.values(),
                                                  key=lambda x: -x["created_at"])]

    def view(self, rid: str) -> dict:
        with self._lock:
            r = self._items.get(rid)
            if r is None:
                raise SettingsError(404, "no_restart", f"no restart {rid!r}")
            return self._view(r)

    # ------------------------------------------------------------------ scheduling
    def schedule(self, row: dict, job: dict, *, change_set: str, who: str, when: str,
                 deadline_s: int | None, kind: str = "settings", start: bool = True) -> dict:
        now = self.clock()
        at = None
        if when.startswith("at="):
            raw = when[3:]
            from datetime import datetime
            at = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
            at = at.timestamp()
        rid = "rs-" + secrets.token_hex(6)
        r = {"id": rid, "host": row["id"], "alias": row.get("alias") or row["id"], "row": row,
             "change_set": change_set, "kind": kind, "who": who, "when": when,
             "deadline_s": deadline_s, "at": at, "created_at": now,
             "deadline_at": None, "ended_at": None, "job": _brief(job),
             "state": "scheduled" if at and at > now else "draining", "message": None,
             "backends": [], "draining": False, "warm": None, "restored": None,
             "cancel": False, "pending": False}
        with self._lock:
            self._items[rid] = r
            old = sorted(self._items.values(), key=lambda x: x["created_at"])
            for x in old[:max(0, len(old) - KEEP)]:
                if x["state"] in FINAL:
                    self._items.pop(x["id"], None)
            if r["state"] == "draining":
                self._drain(r)
        self.log(f"[settings] {change_set}: {r['alias']} restarts ({when}"
                 f"{f', deadline {deadline_s} s' if deadline_s else ''}), restart {rid}")
        if start:
            threading.Thread(target=self.run, args=(rid,), name=f"settings-restart-{rid}",
                             daemon=True).start()
        return self.view(rid)

    def _drain(self, r: dict) -> None:
        now = self.clock()
        try:
            r["backends"] = list(self.backends_of(r["host"]))
        except Exception:                                            # noqa: BLE001
            r["backends"] = []
        r["draining"] = True
        r["state"] = "draining"
        if r["deadline_s"] and r["deadline_at"] is None:
            r["deadline_at"] = now + r["deadline_s"]

    def _end(self, r: dict, state: str, message: str | None = None) -> None:
        with self._lock:
            if r["state"] in FINAL:
                return
            r["state"] = state
            r["message"] = message or r["message"]
            r["draining"] = False
            r["ended_at"] = self.clock()
        self.audit(r["who"], "settings.restart", f"engine/{r['host']}", None, r["change_set"],
                   state, {"restart": r["id"], "job": (r["job"] or {}).get("id")})
        if self.on_end is not None:
            self.on_end(r["id"], state)
        self.log(f"[settings] {r['change_set']}: restart {r['id']} of {r['alias']} {state}"
                 f"{': ' + message if message else ''}")

    def run(self, rid: str) -> None:
        with self._lock:
            r = self._items[rid]
        try:
            self._run(r)
        except Exception as exc:                                     # noqa: BLE001
            self._end(r, "failed", f"the controller's watch stopped: {type(exc).__name__}")
        finally:
            with self._lock:
                r["draining"] = False

    def _status(self, r: dict) -> dict | None:
        try:
            ctl = self.ctl_for(r["row"])
        except enginectl.CtlError:
            return None
        try:
            return ctl.status()
        except enginectl.CtlError:
            return None
        finally:
            ctl.close()

    def _run(self, r: dict) -> None:
        # scheduled: until the time (the engine's job waits for it on its side as well)
        while r["at"] is not None and self.clock() < r["at"]:
            if r["cancel"] or r["state"] in FINAL:
                return
            self.sleep(min(self.poll_s, max(0.0, r["at"] - self.clock())))
        with self._lock:
            if r["state"] in FINAL:
                return
            if not r["draining"]:
                self._drain(r)
        hard = self.clock() + (r["deadline_s"] or 0) + self.start_budget_s
        model = None
        while True:
            if r["cancel"] or r["state"] in FINAL:
                return
            if self.clock() > hard:
                self._end(r, "timeout", "the engine did not finish its job in time: see tandem "
                                        "remote status on its host")
                return
            st = self._status(r)
            if st is None:
                self.sleep(self.poll_s)
                continue
            job = st.get("job")
            if not isinstance(job, dict) or job.get("id") != (r["job"] or {}).get("id"):
                if r["cancel"]:
                    return
                if r["pending"]:
                    self.sleep(self.poll_s)
                    continue
                self._end(r, "cancelled", "replaced by another call on the engine host"
                          + (f" ({job.get('kind')} job {job.get('id')})" if isinstance(job, dict)
                             else ""))
                return
            with self._lock:
                r["job"] = _brief(job)
            js = job.get("state")
            if js == "waiting":
                with self._lock:
                    if r["state"] not in FINAL:
                        r["state"] = "draining"
            elif js == "acting":
                with self._lock:
                    if r["state"] not in FINAL:
                        r["state"] = "restarting"
            elif js == "done":
                model = ((st.get("engine") or {}).get("model")) or "TandemLLM"
                break
            elif js == "failed":
                with self._lock:
                    r["restored"] = job.get("restored")
                was = r["state"]
                self._end(r, "timeout" if was == "draining" else "failed",
                          job.get("message") or "the engine's job failed")
                return
            elif js == "cancelled":
                if r["pending"]:
                    self.sleep(self.poll_s)
                    continue
                if not r["cancel"]:
                    self._end(r, "cancelled", job.get("message") or "cancelled on the engine "
                                                                    "host")
                return
            else:
                self._end(r, "failed", job.get("message") or f"the engine's job is {js}")
                return
            self.sleep(self.poll_s)
        # warming: one token through each backend on the host, then undrained
        with self._lock:
            r["state"] = "warming"
        results = {}
        end = self.clock() + self.warm_wait_s
        for bid in r["backends"]:
            ok, why = False, "not tried"
            while True:
                try:
                    ok, why = self.warm(bid, model)
                except Exception as exc:                             # noqa: BLE001
                    ok, why = False, type(exc).__name__
                if ok or self.clock() >= end or r["cancel"]:
                    break
                self.sleep(self.poll_s)
            results[bid] = {"ok": bool(ok), "why": None if ok else why}
        with self._lock:
            r["warm"] = {"ok": all(v["ok"] for v in results.values()), "backends": results}
        self._end(r, "done", "the engine runs the new settings"
                  + ("" if r["warm"]["ok"] else "; a backend gave no warm-up token, the gateway's "
                                                 "health check takes it from here"))

    # ------------------------------------------------------------------ cancel
    def cancel(self, rid: str, who: str) -> dict:
        with self._lock:
            r = self._items.get(rid)
            if r is None:
                raise SettingsError(404, "no_restart", f"no restart {rid!r}")
            if r["state"] in FINAL:
                raise SettingsError(409, "ended", f"restart {rid} is {r['state']} already")
            if r["state"] not in ("scheduled", "draining"):
                raise SettingsError(409, "too_late", f"the engine is {r['state']}: a restart that "
                                                     f"acts cannot be cancelled")
            r["pending"] = True                 # the watch waits while the cancel runs
        # a lifecycle call on the engine host replaces a job that still waits: `start` finds the
        # engine healthy and changes nothing
        try:
            try:
                ctl = self.ctl_for(r["row"])
            except enginectl.CtlError as exc:
                raise ctl_error(exc) from None
            try:
                ctl.start()
            except enginectl.CtlBusy:
                raise SettingsError(409, "too_late", "the engine's job acts already: it cannot "
                                                     "be cancelled") from None
            except enginectl.CtlError as exc:
                raise ctl_error(exc) from None
            finally:
                ctl.close()
        except SettingsError:
            with self._lock:
                r["pending"] = False
            raise
        with self._lock:
            r["cancel"], r["pending"] = True, False
        self.audit(who, "settings.cancel", f"engine/{r['host']}", None, r["change_set"],
                   "cancelled", {"restart": rid})
        self._end(r, "cancelled", f"cancelled by {who}: the engine keeps the settings it runs")
        return self.view(rid)


# ======================================================================================== HTTP
def http_read(backend: dict, key: str | None) -> dict:
    """GET /v1/engine/settings of an engine reached over HTTP (its backend's URL without /v1)."""
    base = backend["url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    h = {"Accept": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + "/v1/engine/settings", headers=h)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        doc = json.loads(r.read(1 << 20) or b"{}")
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    return doc
