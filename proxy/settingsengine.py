"""The engine settings scope and the restarts it schedules (SRV-845, proxy/settings.py).

ONE TARGET A PAIRED ENGINE HOST (transport ssh; its id is the fleet node's). The store is the
engine's managed overlay, reached through `tandem remote settings get|plan|apply|versions|restore`
(contracts/engine-ctl-v1) by EngineHostControl (proxy/enginectl.py). The engine owns the key
schema: each key's type, default, restart class (`hot`: the running server applies it; `engine`:
read at the start), whether it feeds the disk cache's fingerprint, whether the overlay may set it
(`managed`; a key that may hold a secret never is). This side marks a key whose name looks like a
secret (enginectl.SECRET_KEY) or that the engine says may hold one as secret: its value is never
read into an answer.

THE MODE. Two keys of their own stand for the engine's mode: `mode` (engine: the bare engine;
chat: TandemApp's conductor and Chat inside it) and `mode.ext` (the extension chat mode runs,
tandemapp.engine_ext unless one is named). They show what QSE_MODE and QSE_EXT resolve to; those
two are not changed directly. A part that only sets them goes through `tandem remote mode set`
(restart class engine, the same restart as any other change); with other keys of the same engine
they go into its overlay change as QSE_MODE and QSE_EXT, which is what `mode set` writes, so the
engine restarts once. When the result is chat mode and the engine host has no extension of that
module that loads (`ext list`), the plan names the install of this controller's own wheel
(`ext_wheel()`: TANDEM_NODE_WHEEL, else built from its own copy, proxy/nodeinstall.py), bound by
its sha256; the apply sends it (`ext install`) before the switch. A module that is not TandemApp's,
or no wheel here, is refused in the plan with the fix (`extension_missing`).

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

THE ESTIMATE. A plan that restarts the engine says how long the restart itself takes
(`restart_estimate`, {seconds, source}): `measured`, what this controller saw the last time it
restarted that engine (from the job's acting to the end of the warm-up; kept while it runs); else
`engine`, the engine's last start as it reports it (`status`: its last lifecycle job that acted at
once, from its creation to its end); else `default`, DEFAULT_RESTART_S.

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
import hashlib
import json
import os
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
#: an engine restart's length when neither this controller nor the engine knows one: a model loads
DEFAULT_RESTART_S = 180
#: the engine's lifecycle jobs that stop and start it (an upgrade fetches and builds besides)
START_KINDS = ("start", "restart", "settings", "mode", "restore")
#: the engine's mode as two keys of their own, and the overlay keys `mode set` writes for them
MODE_VARS = {"mode": "QSE_MODE", "mode.ext": "QSE_EXT"}
MODE_KEYS = tuple(MODE_VARS)
MODES = ("engine", "chat")
#: the extension chat mode runs unless one is named (docs/chat-mode.md)
DEFAULT_EXT = "tandemapp.engine_ext"
#: the modules this controller's own wheel carries
OWN_EXT = "tandemapp."


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


def shown_key(key: str) -> str:
    """An overlay key as the scope shows it: QSE_MODE and QSE_EXT are the mode's."""
    return {v: k for k, v in MODE_VARS.items()}.get(key, key)


def _shown(key: str, v):
    return (v or None) if key == "QSE_EXT" else v


def _epoch(text) -> float | None:
    from datetime import datetime
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text) \
            .timestamp()
    except ValueError:
        return None


def engine_start_s(status: dict | None) -> int | None:
    """The engine's last start as `status` reports it: its last lifecycle job that acted at once
    (no wait for idle in it), from its creation to its end."""
    job = (status or {}).get("job")
    if not isinstance(job, dict) or job.get("kind") not in START_KINDS or job.get("state") != "done":
        return None
    if ((job.get("when") or {}).get("mode")) != "now":
        return None
    a, b = _epoch(job.get("created_at")), _epoch(job.get("ended_at"))
    if a is None or b is None or not 0 < b - a < START_BUDGET_S:
        return None
    return int(round(b - a))


def _sha_of(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# ======================================================================================== the scope
class EngineScope(Scope):
    """`rows()`: the fleet's engine hosts over SSH; `ctl_for(row)`: an EngineHostControl;
    `http_only()`: [{id, label, backend}] of engines reached over HTTP only; `http_read(item)`:
    GET /v1/engine/settings of one; `hot_patch(row, body)`: (status, answer) of PATCH
    /v1/engine/settings on a paired host; `restarts`: the scheduler; `ext_wheel()`: the path of
    this controller's own wheel (the chat extension), or an exception that says why there is
    none."""

    def __init__(self, *, rows, ctl_for, restarts, http_only=None, http_read=None,
                 hot_patch=None, ext_wheel=None):
        self.rows = rows
        self.ext_wheel = ext_wheel
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
        rows = {r["key"]: r for r in doc["keys"]}
        keys = []
        if "QSE_MODE" in rows:
            m = rows["QSE_MODE"]
            keys.append(key_spec("mode", "enum", values=MODES, restart="engine",
                                 default=m.get("default") or "engine",
                                 writable=bool(m["managed"]), why=m.get("why"),
                                 text="how the engine runs: engine, the bare engine; chat, with "
                                      "TandemApp's conductor and Chat inside it"))
            keys.append(key_spec("mode.ext", "module", restart="engine", default=DEFAULT_EXT,
                                 nullable=True, writable=bool(m["managed"]), why=m.get("why"),
                                 text="the extension chat mode runs; TandemApp installs its own "
                                      "when the engine host lacks it"))
        for r in doc["keys"]:
            if r["key"] in MODE_VARS.values():
                continue
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
        by = {r["key"]: r for r in doc["keys"]}
        if "QSE_MODE" in by:
            m, e = by["QSE_MODE"], by.get("QSE_EXT") or {}
            rows.append({"key": "mode", "value": m.get("value") or None, "set": bool(m["set"]),
                         "source": m["source"]})
            rows.append({"key": "mode.ext", "value": e.get("value") or None,
                         "set": bool(e.get("set") and e.get("value")),
                         "source": e.get("source") or "default"})
        for r in doc["keys"]:
            if r["key"] in MODE_VARS.values():
                continue
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
            # the mode's overlay keys as the scope shows them
            vals = {shown_key(k): _shown(k, x) for k, x in (v.get("set") or {}).items()
                    if not enginectl.SECRET_KEY.search(k)}
            out.append({"id": str(v["n"]), "at": v.get("at"), "who": None,
                        "note": v.get("note") or "", "change_set": None,
                        "keys": sorted(shown_key(k) for k in (v.get("set") or {})),
                        "current": v["n"] == doc["current"], "good": v["n"] == doc.get("good"),
                        "values": vals})
        return {"current": str(doc["current"]), "versions": out}

    # ------------------------------------------------------------------ plans
    def _overlay(self, target: str, sets: dict, unsets: list, rows: dict) -> tuple[dict, list]:
        """A part's keys as the overlay's: the mode as QSE_MODE and QSE_EXT, as `mode set` writes
        them. Refusals name the key the page shows."""
        for k in list(sets) + list(unsets):
            if k in MODE_VARS.values():
                raise SettingsError(422, "locked", f"{k} changes through the key "
                                                   f"{shown_key(k)}: the mode installs the "
                                                   f"extension chat mode needs", key=k)
        changes = {k: _value(k, v) for k, v in sets.items() if k not in MODE_KEYS}
        unset = [k for k in unsets if k not in MODE_KEYS]
        touched = [k for k in list(sets) + list(unsets) if k in MODE_KEYS]
        if not touched:
            return changes, unset
        if "QSE_MODE" not in rows:
            raise SettingsError(422, "unknown_key", "this engine has no mode to switch: update it "
                                                    "on its host", key=touched[0],
                                keys=touched)
        cur = rows["QSE_MODE"].get("value") or "engine"
        cur_ext = (rows.get("QSE_EXT") or {}).get("value") or None
        mode, ext = self._mode_args(sets, cur)
        if "mode" in sets:
            changes["QSE_MODE"] = mode
            if "QSE_EXT" in rows:
                changes["QSE_EXT"] = (ext or (cur_ext if cur == "chat" and cur_ext else
                                              DEFAULT_EXT)) if mode == "chat" else ""
        elif "mode.ext" in sets:
            changes["QSE_EXT"] = ext or DEFAULT_EXT
        if "mode" in unsets:
            unset += [k for k in ("QSE_MODE", "QSE_EXT") if k in rows]
        elif "mode.ext" in unsets:
            unset.append("QSE_EXT")
        return changes, unset

    @staticmethod
    def _mode_args(sets: dict, cur: str | None) -> tuple[str | None, str | None]:
        """(the mode asked, the extension asked) of a part's mode keys, checked."""
        mode = sets.get("mode")
        if "mode" in sets and mode not in MODES:
            raise SettingsError(422, "bad_value", "mode is engine or chat", key="mode")
        ext = sets.get("mode.ext")
        if ext is not None and (not isinstance(ext, str) or not enginectl.MODULE_RE.match(ext)):
            raise SettingsError(422, "bad_value", "mode.ext is a dotted module name, like "
                                                  f"{DEFAULT_EXT}", key="mode.ext")
        if "mode.ext" in sets and (mode or cur) != "chat":
            raise SettingsError(422, "bad_value", "mode.ext goes with chat mode: choose chat as "
                                                  "well", key="mode.ext")
        return mode, ext

    def _ext_need(self, ctl, ext: str) -> dict | None:
        """None when the engine host has `ext` and it loads; else this controller's wheel to
        install first ({path, name, sha256, bytes, module}), or the refusal with its fix."""
        doc = ctl.ext_list()
        for e in doc.get("extensions") or []:
            if e.get("module") == ext and e.get("imports") and e.get("accepted"):
                return None
        if not ext.startswith(OWN_EXT):
            raise SettingsError(409, "extension_missing",
                                f"the engine host has no extension {ext} that loads, and "
                                f"TandemApp installs only its own ({OWN_EXT}*): install {ext} "
                                f"there first (tandem remote ext install), or choose "
                                f"{DEFAULT_EXT}", key="mode.ext")
        why = "this controller has no wheel to send"
        path = None
        if self.ext_wheel is not None:
            try:
                path = self.ext_wheel()
            except Exception as exc:                                 # noqa: BLE001
                why = str(getattr(exc, "message", None) or exc or type(exc).__name__)[:300]
        if not path or not os.path.isfile(path):
            raise SettingsError(409, "extension_missing",
                                f"the engine host has no {ext} that loads, and {why}: set "
                                f"TANDEM_NODE_WHEEL to the tandemapp wheel of this release and "
                                f"plan again, or install the extension on the engine host "
                                f"(tandem remote ext install)", key="mode.ext")
        sha, size = _sha_of(path)
        return {"path": path, "name": os.path.basename(path), "sha256": sha, "bytes": size,
                "module": ext}

    @staticmethod
    def _install_note(inst: dict) -> str:
        return (f"the engine host has no {inst['module']} that loads: TandemApp installs its own "
                f"wheel first ({inst['name']}, {max(1, round(inst['bytes'] / 1e6))} MB)")

    def _plan_mode(self, target: str, sets: dict) -> dict:
        """A part that only sets the mode: `mode set`, after the extension's install."""
        with self._ctl(target) as ctl:
            cur = ctl.mode_get()
            cur_mode, cur_ext = cur.get("mode"), cur.get("ext") or None
            inferred = bool(cur.get("inferred"))
            mode, ext = self._mode_args(sets, cur_mode)
            new_mode = mode or cur_mode
            new_ext = None
            if new_mode == "chat":
                new_ext = ext or (cur_ext if cur_mode == "chat" and cur_ext else DEFAULT_EXT)
            prev = {k: cur.get(k) for k in ("mode", "ext", "source", "inferred")}
            if new_mode == cur_mode and not inferred and new_ext == (cur_ext if new_mode == "chat"
                                                                     else None):
                return part_plan([], state=digest(["no_change", target, prev]),
                                 native={"kind": "mode", "previous": prev})
            install = self._ext_need(ctl, new_ext) if new_mode == "chat" else None
            st = self._status(ctl)
        base = ((st or {}).get("overlay") or {}).get("current")
        out = []
        if new_mode != cur_mode or inferred:
            out.append(change("mode", cur_mode, new_mode, was_set=not inferred, is_set=True,
                              restart="engine"))
        if new_ext != cur_ext:
            out.append(change("mode.ext", cur_ext, new_ext, was_set=cur_ext is not None,
                              is_set=new_ext is not None, restart="engine"))
        warnings = [self._install_note(install)] if install else []
        native = {"kind": "mode", "mode": new_mode, "ext": new_ext, "base": base,
                  "install": install, "hot_only": False, "previous": prev}
        return part_plan(out, state=digest([prev, base, (install or {}).get("sha256")]),
                         warnings=warnings, native=native, restart="engine", fingerprint_keys=[],
                         estimate=self._estimate(target, st))

    @staticmethod
    def _status(ctl) -> dict | None:
        try:
            return ctl.status()
        except enginectl.CtlError:
            return None

    def _estimate(self, target: str, status: dict | None) -> dict:
        """How long the engine's restart itself takes: measured here, else the engine's own last
        start, else DEFAULT_RESTART_S."""
        took = self.restarts.took(target) if self.restarts is not None else None
        if took is not None:
            return {"seconds": took, "source": "measured"}
        got = engine_start_s(status)
        if got is not None:
            return {"seconds": got, "source": "engine"}
        return {"seconds": DEFAULT_RESTART_S, "source": "default"}

    def _plan(self, target: str, sets: dict, unsets: list, *, restore: int | None = None) -> dict:
        if not self.target_info(target)["writable"]:
            self._row(target)
        if restore is None and sets and not unsets and all(k in MODE_KEYS for k in sets):
            return self._plan_mode(target, sets)
        install = None
        with self._ctl(target) as ctl:
            if restore is None:
                rows = {r["key"]: r for r in ctl.settings_get()["keys"]}
                changes, unset = self._overlay(target, sets, unsets, rows)
                new_mode = changes.get("QSE_MODE", (rows.get("QSE_MODE") or {}).get("value"))
                if "QSE_EXT" in changes and new_mode == "chat":
                    install = self._ext_need(ctl, changes["QSE_EXT"] or DEFAULT_EXT)
            else:
                changes, unset = {k: _value(k, v) for k, v in sets.items()}, list(unsets)
            try:
                doc = ctl.settings_plan(changes, unset=unset)
            except enginectl.CtlRefused as exc:
                if exc.code == "no_change":
                    return part_plan([], state=digest(["no_change", target, restore]))
                raise
            hot = (not doc["plan"]["restart_needed"] and restore is None and install is None
                   and all(e["restart"] == "hot" for e in doc["plan"]["effect"]))
            st = None if hot else self._status(ctl)
        p = doc["plan"]
        shadowed = set(p.get("shadowed") or [])
        out = []
        for e in p["effect"]:
            secret = bool(enginectl.SECRET_KEY.search(e["key"]))
            out.append(change(shown_key(e["key"]), _shown(e["key"], e["from"]),
                              _shown(e["key"], e["to"]), was_set=e["was_set"],
                              is_set=e["is_set"], restart=e["restart"],
                              fingerprint=bool(e["fingerprint"]), secret=secret))
        for o in p["overlay"]:
            if o["key"] in shadowed:
                out.append(change(shown_key(o["key"]), o["from"], o["to"],
                                  was_set=o["from"] is not None, is_set=o["to"] is not None,
                                  restart="engine",
                                  secret=bool(enginectl.SECRET_KEY.search(o["key"]))))
        hot_only = (bool(p["effect"]) and not shadowed and not p["restart_needed"]
                    and all(e["restart"] == "hot" for e in p["effect"]) and restore is None
                    and install is None)
        warnings = list(p.get("warnings") or [])
        if install:
            warnings.insert(0, self._install_note(install))
        if not hot_only and not p["restart_needed"]:
            warnings.append("applied over SSH, this change restarts the engine all the same")
        native = {"base": p["base"], "confirm": p["confirm"],
                  "confirm_fingerprint": p.get("confirm_fingerprint"), "hot_only": hot_only,
                  "changes": changes, "unset": list(unset), "restore": restore,
                  "install": install}
        return part_plan(out, state=digest([p["confirm"], p.get("confirm_fingerprint"),
                                            p["base"], (install or {}).get("sha256")]),
                         warnings=warnings, native=native,
                         restart="hot" if hot_only else "engine",
                         fingerprint_keys=[shown_key(k) for k in p.get("fingerprint_keys") or []],
                         estimate=None if hot_only else self._estimate(target, st))

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

    def _install(self, ctl, inst: dict | None) -> None:
        """The extension's wheel the plan named, sent before the switch (no restart of its own)."""
        if not inst:
            return
        path = self.ext_wheel() if self.ext_wheel is not None else None
        if not path or not os.path.isfile(path) or _sha_of(path)[0] != inst["sha256"]:
            raise SettingsError(409, "stale_plan", "this controller's wheel changed since the "
                                                   "plan: plan again")
        ctl.ext_install(path, name=inst["name"], sha256=inst["sha256"], module=inst["module"])

    def apply(self, target, plan, ctx):
        nat = plan["native"]
        row = self._row(target)
        base = None if nat.get("base") is None else str(nat["base"])
        restore = [] if base is None else [{"scope": "engine", "target": target, "version": base}]
        if nat.get("kind") == "mode":
            when, deadline = self._when(ctx)
            with self._ctl(target) as ctl:
                self._install(ctl, nat.get("install"))
                doc = ctl.mode_set(nat["mode"], ext=nat.get("ext"), when=when, deadline=deadline)
            view = self.restarts.schedule(row, doc["job"], change_set=ctx["change_set"],
                                          who=ctx["who"], when=when, deadline_s=deadline,
                                          kind="mode")
            return {"version_before": base, "version_after": None, "restore": restore,
                    "restart": view}
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
            self._install(ctl, nat.get("install"))
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
    return {k: job.get(k) for k in ("id", "kind", "state", "phase", "when", "message",
                                    "waiting_for", "created_at", "ended_at")}


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
        self._took: dict[str, int] = {}         # host -> seconds its last restart took here
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

    def took(self, host: str) -> int | None:
        """How long the last restart of `host` this controller followed took, from the job's
        acting to the end of its warm-up; None before one ended here."""
        with self._lock:
            return self._took.get(host)

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
             "cancel": False, "pending": False, "acting_at": None}
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
                    if r["acting_at"] is None:
                        r["acting_at"] = self.clock()
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
            if r["acting_at"] is not None:
                self._took[r["host"]] = max(0, int(round(self.clock() - r["acting_at"])))
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
