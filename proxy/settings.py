"""One settings interface over the existing stores (SRV-845, design section 7): scopes, change sets,
versions, restore and audit. contracts/settings-v1 is its truth; docs/settings.md says it in words.

    s = Settings(cfg, scopes, restarts=..., log=...)     proxy/app.py builds it (STATE["settings"])
    s.handle(method, parts, query, body, who)            /v1/settings/* -> (status, answer)

    GET  /v1/settings                                    the scopes in their order, each scope's
                                                         targets, the operations
    GET  /v1/settings/{scope}/schema[?target=]           the keys: type, default, restart class,
                                                         fingerprint flag, secret (write-only)
    GET  /v1/settings/{scope}[?target=]                  the values and where each comes from
    POST /v1/settings/{scope}/plan {target?, set?, unset?}
    POST /v1/settings/{scope}/apply {target?, set?, unset?, confirm, confirm_fingerprint?,
                                     confirm_grant?, when?, deadline_s?, note?}
    GET  /v1/settings/{scope}/versions[?target=]
    POST /v1/settings/{scope}/restore {target?, version, confirm?, confirm_fingerprint?,
                                       confirm_grant?, when?, deadline_s?, note?}
                                                         without confirm: the plan
    POST /v1/settings/plan {changes: [{scope, target?, set?, unset?} | {scope, target?, restore}]}
    POST /v1/settings/apply {changes, confirm, confirm_fingerprint?, confirm_grant?, when?,
                             deadline_s?, note?}
    GET  /v1/settings/restarts[/{id}]                    engine restarts a change scheduled,
                                                         with their countdown
    POST /v1/settings/restarts/{id}/cancel
    GET  /v1/settings/audit[?change_set=&limit=]

THE SCOPES, in dependency order (SCOPES): app (proxy/settingscopes.py, the page's switches),
backends (backends, routes and users in the config database), conductor (this controller's
conductor documents, tandemapp/conductor/store.py), engine (per engine host over SSH, its managed
overlay through `tandem remote settings`, proxy/settingsengine.py) and hooks (an adapter that the
hook registry plugs into; until then a scope with no key). Every scope answers the same six
operations with the same shapes. The stores stay where they are: this module only plans, applies
and records.

A CHANGE SET is a list of parts, at most one per scope and target: a part sets and unsets keys, or
restores one version. Its plan asks each scope what would change (nothing is written; a part that
restarts an engine says how long the restart itself takes, `restart_estimate`), and gives
one `confirm`: an HMAC, keyed with this process's random secret, over each part's changes and the
state its scope planned against (values, versions, the engine's own plan token). A part whose keys
feed an engine's disk-cache fingerprint adds `confirm_fingerprint`, a second token the apply must
carry as well; a part that grants a hook the full payload to a destination outside the LAN adds
`confirm_grant` the same way (its keys in `grant_keys`, SEC-16). Apply plans again: a confirm that
does not match the state now is 409 `stale_plan`, one missing 428 `confirm_required` (and
`fingerprint_confirm_required`, `grant_confirm_required`). The parts then apply in
dependency order (app, backends, conductor, engine, hooks; targets by id within a scope) under one
lock (a second change set meanwhile is 409 `busy`), and the first failure stops the set: the answer
lists each part as applied, scheduled (an engine restart runs it), unchanged, failed or not_run,
with the version it had before and the restore that brings it back. Applying the same confirm again
answers the same result (`replayed`); planning the same changes again after a partial failure plans
only what is still different. Unsetting a key that is unset already is no change: the plan drops
it, in every scope. A refusal while a part is planned names that part's `scope` and
`target` (and its `key` when the refusal is about one key's value), so the page marks that field.

THE CALLER. `handle(..., trust=)` says whether the caller has pairing's rights (`fleet_trust`: the
admin, or this machine while no admin password is set); a scope asks `trusted()` while it plans.
The hooks scope grants the full payload only to such a caller (403 `trust_required`).

SECRETS (a backend's API key, a decision provider's) are write-only: a value is never in an
answer, a version, a log line, an audit row or an export; an answer says `set` true or false. A
plan shows a secret's change as set or unset, and its confirm binds the value through its hash
only. A restore leaves secrets as they are.

AUDIT. Every part's apply or restore, a cancelled restart, and the change set as a whole are rows of
the config database's audit table (proxy/config.py) with the change set's id, the scope and target,
the keys (never a value), who (the admin session: `admin` or `local`), when and the result. Every
row a store writes while a change set applies (a backend's update, a part's switch) carries the
change set's id too (`cfg.change_set`).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

CONTRACT = "1.0"
#: the scopes in dependency order: a change set applies them in this order
SCOPES = ("app", "backends", "conductor", "engine", "hooks")
OPS = ("schema", "get", "plan", "apply", "versions", "restore")
RESTART = ("none", "app", "hot", "engine")
TYPES = ("string", "integer", "number", "boolean", "enum", "list", "object", "document", "path",
         "module", "cidrs", "secret")
MAX_PARTS = 16
MAX_KEYS = 256
NOTE_MAX = 200
REPLAY_KEEP = 64
#: how long the same confirm answers its result again (a retried POST), unless the result stops
#: holding first (a restart it scheduled ended without `done`)
REPLAY_S = 300.0
DEADLINE = (10, 86400)
DEFAULT_DEADLINE_S = 600
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./@:-]{0,159}$")
#: whether this request's caller has pairing's rights (`Settings.handle` sets it, `trusted()` reads)
_CALLER = threading.local()
#: the refusals about one key's value: a part of one key names it (`Settings._placed`)
KEY_ERRORS = ("unknown_key", "locked", "bad_value", "invalid_document", "extension_missing")


class SettingsError(Exception):
    """A refusal: `status` the HTTP answer, `kind` the error's type, `extra` its facts."""

    def __init__(self, status: int, kind: str, message: str, **extra):
        super().__init__(message)
        self.status, self.kind, self.message, self.extra = status, kind, message, extra

    def body(self) -> dict:
        err = {"type": self.kind, "message": self.message}
        err.update({k: v for k, v in self.extra.items() if v is not None})
        return {"error": err}

    def brief(self) -> dict:
        return {"type": self.kind, "message": self.message}


def iso(ms_or_s, ms: bool = True) -> str | None:
    if ms_or_s is None:
        return None
    t = ms_or_s / 1000.0 if ms else ms_or_s
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def feeds(keys) -> str:
    """`A feeds`, `A, B feed`: the verb agrees with the number of keys."""
    keys = list(keys)
    return f"{', '.join(keys)} {'feeds' if len(keys) == 1 else 'feed'}"


def trusted() -> bool:
    """The caller of the request being handled has pairing's rights (`fleet_trust`)."""
    return bool(getattr(_CALLER, "trust", False))


def max_restart(classes) -> str:
    got = [c for c in classes if c in RESTART]
    return max(got, key=RESTART.index) if got else "none"


def key_spec(key: str, type_: str, *, restart: str = "none", default=None, values=None,
             secret: bool = False, writable: bool = True, why: str | None = None,
             fingerprint: bool = False, nullable: bool = False, source_var: str | None = None,
             text: str | None = None) -> dict:
    """One key of a scope's schema, as settings-v1 schema.schema.json has it."""
    out = {"key": key, "type": "secret" if secret else type_, "restart": restart,
           "fingerprint": fingerprint, "secret": secret, "write_only": secret,
           "writable": writable, "nullable": nullable, "default": None if secret else default}
    if values is not None:
        out["values"] = list(values)
    if why:
        out["why"] = why
    if source_var:
        out["variable"] = source_var
    if text:
        out["text"] = text
    return out


# ======================================================================================== scopes
class Scope:
    """A settings scope. A subclass answers for its store; every answer here is a plain dict.

        targets() -> [{id, label, writable, why}]
        schema(target) -> {writable, why, keys: [key_spec]}
        get(target) -> {writable, why, version, values: [{key, value, set, source, ...}]}
        plan(target, sets, unsets) -> a part plan (`part_plan`)
        apply(target, plan, ctx) -> {version_before, version_after, restore, restart?, note?}
        versions(target) -> {current, versions: [{id, at, who, note, change_set, keys,
                                                   current, values?}]}
        plan_restore(target, version) -> a part plan
        restore(target, plan, ctx) -> as apply

    A part plan: {changes: [change], restart, fingerprint_keys, grant_keys, warnings, state (what
    the plan read, bound into the confirm; never shown), native (the scope's own, never shown)}.
    `ctx`: {who, change_set, when, deadline_s, note, fingerprint_ok, grant_ok}.
    """

    name = "?"
    single_target = "controller"

    def targets(self) -> list[dict]:
        return [{"id": self.single_target, "label": "this controller", "writable": True,
                 "why": None}]

    def check_target(self, target: str | None) -> str:
        ts = self.targets()
        if target is None:
            if len(ts) == 1:
                return ts[0]["id"]
            raise SettingsError(400, "target_required",
                                f"the {self.name} scope has {len(ts)} targets: name one "
                                f"({', '.join(t['id'] for t in ts) or 'none yet'})")
        if not any(t["id"] == target for t in ts):
            raise SettingsError(404, "no_target", f"the {self.name} scope has no target {target!r}")
        return target

    def target_info(self, target: str) -> dict:
        for t in self.targets():
            if t["id"] == target:
                return t
        raise SettingsError(404, "no_target", f"the {self.name} scope has no target {target!r}")


def change(key: str, before, after, *, was_set: bool, is_set: bool, restart: str = "none",
           fingerprint: bool = False, secret: bool = False, diff: list | None = None) -> dict:
    out = {"key": key, "from": None if secret else before, "to": None if secret else after,
           "was_set": was_set, "is_set": is_set, "secret": secret, "restart": restart,
           "fingerprint": fingerprint}
    if diff is not None:
        out["diff"] = diff[:50]
        out["diff_count"] = len(diff)
    return out


def part_plan(changes: list, *, state, warnings=(), native=None, restart: str | None = None,
              fingerprint_keys=None, estimate: dict | None = None, grant_keys=()) -> dict:
    """`estimate`: how long an engine restart itself takes, {seconds, source}. `grant_keys`: the
    keys that grant the full payload outside the LAN (the second confirm, `confirm_grant`)."""
    return {"changes": changes,
            "restart": restart or max_restart(c["restart"] for c in changes),
            "fingerprint_keys": sorted(fingerprint_keys if fingerprint_keys is not None else
                                       {c["key"] for c in changes if c["fingerprint"]}),
            "grant_keys": sorted(grant_keys),
            "warnings": list(warnings), "state": state, "native": native or {},
            "estimate": estimate}


class KeyValueScope(Scope):
    """A scope of flat keys over a store with no versions of its own: the versions are the
    scope's values after each change, kept in the config database (`settings_versions`), secrets
    left out. A subclass gives:

        spec(target) -> {key: key_spec}         the keys now
        values(target) -> {key: (value, set, source, extra)}
        check(target, sets, unsets, spec) -> warnings   raise SettingsError for a refusal
        write(target, sets, unsets, who) -> None        the store's own writes
    """

    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------------ to give
    def spec(self, target: str) -> dict:                                 # pragma: no cover
        raise NotImplementedError

    def values(self, target: str) -> dict:                               # pragma: no cover
        raise NotImplementedError

    def check(self, target, sets, unsets, spec) -> list:                 # pragma: no cover
        raise NotImplementedError

    def write(self, target, sets, unsets, who) -> None:                  # pragma: no cover
        raise NotImplementedError

    def writable(self, target: str) -> tuple[bool, str | None]:
        return True, None

    # ------------------------------------------------------------------ the six operations
    def schema(self, target: str) -> dict:
        ok, why = self.writable(target)
        return {"writable": ok, "why": why, "keys": [self.spec(target)[k]
                                                     for k in sorted(self.spec(target))]}

    def snapshot(self, target: str, spec: dict | None = None) -> dict:
        """The values a version keeps: every key but the secrets."""
        spec = spec or self.spec(target)
        return {k: copy.deepcopy(v[0]) for k, v in self.values(target).items()
                if not spec.get(k, {}).get("secret")}

    def latest(self, target: str) -> dict | None:
        got = self.cfg.settings_versions(self.name, target, 1)
        return got[0] if got else None

    def get(self, target: str) -> dict:
        ok, why = self.writable(target)
        spec = self.spec(target)
        vals = self.values(target)
        last = self.latest(target)
        cur = last is not None and last["doc"] == self.snapshot(target, spec)
        rows = []
        for k in sorted(vals):
            v, is_set, source, extra = vals[k]
            secret = spec.get(k, {}).get("secret", False)
            row = {"key": k, "value": None if secret else v, "set": bool(is_set),
                   "source": source}
            row.update(extra or {})
            rows.append(row)
        return {"writable": ok, "why": why,
                "version": str(last["n"]) if cur else None, "values": rows}

    def _state(self, target: str, keys) -> str:
        vals = self.values(target)
        spec = self.spec(target)
        out = {}
        for k in sorted(keys):
            if k in vals:
                v, is_set, source, extra = vals[k]
                out[k] = [digest(v) if spec.get(k, {}).get("secret") else v, is_set, source,
                          (extra or {}).get("hint")]
            else:
                out[k] = None
        return digest(out)

    def plan(self, target: str, sets: dict, unsets: list) -> dict:
        ok, why = self.writable(target)
        if not ok:
            raise SettingsError(409, "read_only", why or f"the {self.name} scope is read-only here")
        spec = self.spec(target)
        unknown = [k for k in list(sets) + list(unsets) if k not in spec]
        if unknown:
            raise SettingsError(422, "unknown_key", f"not a key of the {self.name} scope: "
                                f"{', '.join(sorted(unknown)[:10])}", keys=sorted(unknown)[:50])
        locked = [k for k in list(sets) + list(unsets) if not spec[k]["writable"]]
        if locked:
            k = locked[0]
            raise SettingsError(422, "locked", f"{k} cannot be changed here: "
                                f"{spec[k].get('why') or 'read-only'}",
                                key=k, variable=spec[k].get("variable"))
        warnings = self.check(target, sets, unsets, spec) or []
        vals = self.values(target)
        changes = []
        for k in sorted(set(sets) | set(unsets)):
            s = spec[k]
            cur = vals.get(k, (None, False, None, None))
            if k in sets:
                new, is_set = sets[k], sets[k] is not None
                if s["secret"]:
                    # a key is never read back to compare; nothing on nothing is no change
                    same = new is None and not cur[1]
                else:
                    same = (cur[1] and cur[0] == new) or (new is None and not cur[1])
            else:
                new, is_set = s.get("default"), False
                same = not cur[1] and not s["secret"]
                if s["secret"]:
                    same = not cur[1]
            if same:
                continue
            changes.append(change(k, cur[0], new, was_set=bool(cur[1]), is_set=is_set,
                                  restart=s["restart"], fingerprint=s["fingerprint"],
                                  secret=s["secret"]))
        state = self._state(target, list(sets) + list(unsets))
        return part_plan(changes, state=state, warnings=warnings)

    def _record(self, target: str, ctx: dict, note: str, keys=()) -> int:
        return self.cfg.add_settings_version(self.name, target, self.snapshot(target),
                                             who=ctx.get("who"), change_set=ctx.get("change_set"),
                                             note=note, keys=keys)

    def _before(self, target: str, ctx: dict) -> int:
        """The version the scope is at before a change: the latest one when it still holds,
        else a new one of the values as found (a change made on another screen)."""
        last = self.latest(target)
        if last is not None and last["doc"] == self.snapshot(target):
            return last["n"]
        return self.cfg.add_settings_version(self.name, target, self.snapshot(target),
                                             who=None, change_set=ctx.get("change_set"),
                                             note="as found before a change")

    def apply(self, target: str, plan: dict, ctx: dict) -> dict:
        before = self._before(target, ctx)
        keys = [c["key"] for c in plan["changes"]]
        sets = {c["key"]: plan["native"]["sets"][c["key"]] for c in plan["changes"]
                if c["key"] in plan["native"]["sets"]}
        unsets = [k for k in keys if k not in sets]
        self.write(target, sets, unsets, ctx["who"])
        after = self._record(target, ctx, ctx.get("note") or f"change set {ctx['change_set']}",
                             keys)
        return {"version_before": str(before), "version_after": str(after),
                "restore": [{"scope": self.name, "target": target, "version": str(before)}]}

    def versions(self, target: str) -> dict:
        rows = self.cfg.settings_versions(self.name, target)
        spec = self.spec(target)
        cur_doc = self.snapshot(target, spec)
        cur = rows[0]["n"] if rows and rows[0]["doc"] == cur_doc else None
        out = []
        for r in rows:
            out.append({"id": str(r["n"]), "at": iso(r["at_ms"]), "who": r["who"],
                        "note": r["note"] or "", "change_set": r["change_set"],
                        "keys": r["keys"], "current": r["n"] == cur,
                        "values": {k: v for k, v in r["doc"].items()
                                   if not spec.get(k, {}).get("secret")}})
        return {"current": None if cur is None else str(cur), "versions": out}

    def plan_restore(self, target: str, version: str) -> dict:
        try:
            n = int(version)
        except (TypeError, ValueError):
            raise SettingsError(404, "no_version", f"no version {version!r} of the {self.name} "
                                                   f"scope") from None
        got = self.cfg.settings_version(self.name, target, n)
        if got is None:
            raise SettingsError(404, "no_version", f"no version {version!r} of the {self.name} "
                                                   f"scope")
        spec = self.spec(target)
        cur = self.snapshot(target, spec)
        sets, unsets, warnings = {}, [], []
        gone = sorted(k for k in got["doc"] if k not in spec)
        if gone:
            warnings.append(f"{len(gone)} key(s) of version {n} no longer exist and stay as they "
                            f"are: {', '.join(gone[:8])}")
        added = sorted(k for k in cur if k not in got["doc"] and not spec[k]["secret"])
        if added:
            warnings.append(f"{len(added)} key(s) came after version {n} and stay as they are: "
                            f"{', '.join(added[:8])}")
        fixed = []
        for k, v in got["doc"].items():
            if k not in spec or spec[k]["secret"] or cur.get(k) == v:
                continue
            if not spec[k]["writable"]:
                fixed.append(k)
                continue
            sets[k] = v
        if fixed:
            warnings.append(f"{', '.join(fixed[:8])}: set elsewhere now (the environment), not "
                            f"restored")
        if any(s["secret"] for s in spec.values()):
            warnings.append("secrets are not kept in versions: they stay as they are")
        p = self.plan(target, sets, unsets) if sets or unsets else \
            part_plan([], state=self._state(target, []))
        p["warnings"] = warnings + p["warnings"]
        p["native"] = {"sets": sets, "version": n}
        return p

    def restore(self, target: str, plan: dict, ctx: dict) -> dict:
        n = plan["native"]["version"]
        ctx = dict(ctx, note=ctx.get("note") or f"restore of version {n}")
        return self.apply(target, plan, ctx)


# ================================================================================ change sets
def _bad(msg: str, **extra) -> SettingsError:
    return SettingsError(400, "bad_request", msg, **extra)


def _parse_change(c, i: int) -> dict:
    where = f"changes[{i}]"
    if not isinstance(c, dict):
        raise _bad(f"{where} is an object {{scope, target?, set?, unset?}} or {{scope, target?, "
                   f"restore}}")
    extra = set(c) - {"scope", "target", "set", "unset", "restore"}
    if extra:
        raise _bad(f"{where}: unknown field {sorted(extra)[0]!r}")
    scope = c.get("scope")
    if scope not in SCOPES:
        raise SettingsError(404, "no_scope", f"{where}: no scope {scope!r} (one of "
                                             f"{', '.join(SCOPES)})")
    target = c.get("target")
    if target is not None and (not isinstance(target, str) or not TARGET_RE.match(target)):
        raise _bad(f"{where}.target is a target's id")
    if "restore" in c:
        if "set" in c or "unset" in c:
            raise _bad(f"{where}: a restore sets nothing else")
        v = c["restore"]
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str) or not VERSION_RE.match(v):
            raise _bad(f"{where}.restore is a version's id")
        return {"scope": scope, "target": target, "kind": "restore", "version": v,
                "sets": {}, "unsets": []}
    sets = c.get("set") or {}
    unsets = c.get("unset") or []
    if not isinstance(sets, dict) or not isinstance(unsets, list):
        raise _bad(f"{where}: set is {{key: value}}, unset a list of keys")
    if not sets and not unsets:
        raise _bad(f"{where}: set or unset at least one key")
    if len(sets) + len(unsets) > MAX_KEYS:
        raise _bad(f"{where}: at most {MAX_KEYS} keys")
    for k in list(sets) + unsets:
        if not isinstance(k, str) or not KEY_RE.match(k):
            raise _bad(f"{where}: {str(k)[:40]!r} is not a key")
    both = set(sets) & set(unsets)
    if both:
        raise _bad(f"{where}: {sorted(both)[0]} is both set and unset")
    if len(set(unsets)) != len(unsets):
        raise _bad(f"{where}: a key is unset twice")
    return {"scope": scope, "target": target, "kind": "set", "version": None,
            "sets": dict(sets), "unsets": list(unsets)}


def _parse_when(body: dict) -> tuple[str | None, int | None]:
    when = body.get("when")
    if when is not None:
        if not isinstance(when, str) or not (when in ("now", "idle") or when.startswith("at=")):
            raise _bad("when is now, idle or at=<ISO 8601 time with a zone>")
        if when.startswith("at="):
            raw = when[3:]
            try:
                at = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
            except ValueError:
                raise _bad("when: at= takes an ISO 8601 time") from None
            if at.tzinfo is None:
                raise _bad("when: the time needs a zone (Z or +hh:mm)")
    dl = body.get("deadline_s")
    if dl is not None:
        if not isinstance(dl, int) or isinstance(dl, bool) or not DEADLINE[0] <= dl <= DEADLINE[1]:
            raise _bad(f"deadline_s is a whole number {DEADLINE[0]}..{DEADLINE[1]}")
        if when == "now":
            raise _bad("deadline_s is the wait for idle: it goes with when idle or at=")
    return when, dl


class Settings:
    def __init__(self, cfg, scopes: dict, *, restarts=None, log=None, clock=time.time):
        self.cfg = cfg
        self.scopes = {n: scopes[n] for n in SCOPES if n in scopes}
        for n, s in self.scopes.items():
            s.name = n
        self.restarts = restarts
        self.log = log or (lambda m: print(m, flush=True))
        self.clock = clock
        self._secret = secrets.token_bytes(32)
        self._apply = threading.Lock()
        self._done: dict[str, tuple] = {}           # confirm -> (body, result, at): replay
        if restarts is not None:
            restarts.on_end = self._restart_ended

    def scope(self, name: str) -> Scope:
        s = self.scopes.get(name)
        if s is None:
            raise SettingsError(404, "no_scope", f"no scope {name!r} (one of "
                                                 f"{', '.join(self.scopes)})")
        return s

    # ------------------------------------------------------------------ reads
    def overview(self) -> dict:
        out = []
        for i, (n, s) in enumerate(self.scopes.items()):
            try:
                ts = s.targets()
                err = None
            except SettingsError as exc:
                ts, err = [], exc.message
            out.append({"scope": n, "order": i, "targets": ts, "why": err})
        return {"contract_version": CONTRACT, "scopes": out, "order": list(self.scopes),
                "operations": list(OPS)}

    def schema(self, scope: str, target: str | None) -> dict:
        s = self.scope(scope)
        t = s.check_target(target)
        return {"contract_version": CONTRACT, "scope": scope, "target": t, **s.schema(t)}

    def get(self, scope: str, target: str | None) -> dict:
        s = self.scope(scope)
        t = s.check_target(target)
        return {"contract_version": CONTRACT, "scope": scope, "target": t, **s.get(t)}

    def versions(self, scope: str, target: str | None) -> dict:
        s = self.scope(scope)
        t = s.check_target(target)
        return {"contract_version": CONTRACT, "scope": scope, "target": t, **s.versions(t)}

    # ------------------------------------------------------------------ plans
    def _parts(self, body) -> list[dict]:
        if not isinstance(body, dict):
            raise _bad("the body is {changes: [...]}")
        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            raise _bad("changes is a list of one or more parts")
        if len(changes) > MAX_PARTS:
            raise _bad(f"at most {MAX_PARTS} parts in one change set")
        parts = [_parse_change(c, i) for i, c in enumerate(changes)]
        seen = set()
        for p in parts:
            s = self.scope(p["scope"])
            p["target"] = s.check_target(p["target"])
            k = (p["scope"], p["target"])
            if k in seen:
                raise _bad(f"two parts for {p['scope']}/{p['target']}: one part a scope and "
                           f"target")
            seen.add(k)
        parts.sort(key=lambda p: (SCOPES.index(p["scope"]), p["target"]))
        return parts

    def _plan_parts(self, parts: list[dict]) -> None:
        for p in parts:
            s = self.scope(p["scope"])
            try:
                if p["kind"] == "restore":
                    pl = s.plan_restore(p["target"], p["version"])
                else:
                    pl = s.plan(p["target"], p["sets"], p["unsets"])
            except SettingsError as exc:
                raise self._placed(exc, p) from None
            pl["native"].setdefault("sets", dict(p["sets"]))
            p["plan"] = pl

    @staticmethod
    def _placed(exc: SettingsError, p: dict) -> SettingsError:
        """A part's refusal names its scope and target, and the key of a part of one key when it
        is about a value: the page marks that field and no other."""
        exc.extra.setdefault("scope", p["scope"])
        exc.extra.setdefault("target", p["target"])
        keys = list(p["sets"]) + list(p["unsets"])
        if exc.extra.get("key") is None and len(keys) == 1 and exc.kind in KEY_ERRORS:
            exc.extra["key"] = keys[0]
        return exc

    def _tokens(self, parts: list[dict]) -> tuple[str, str | None]:
        body = [[p["scope"], p["target"], p["kind"], p["version"], digest(p["sets"]),
                 sorted(p["unsets"]), p["plan"]["state"],
                 [c["key"] for c in p["plan"]["changes"]]] for p in parts]
        msg = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        confirm = hmac.new(self._secret, b"confirm\0" + msg, hashlib.sha256).hexdigest()[:32]
        fp = cg = None
        if any(p["plan"]["fingerprint_keys"] for p in parts):
            fp = hmac.new(self._secret, b"fingerprint\0" + msg, hashlib.sha256).hexdigest()[:32]
        if any(p["plan"].get("grant_keys") for p in parts):
            cg = hmac.new(self._secret, b"grant\0" + msg, hashlib.sha256).hexdigest()[:32]
        return confirm, fp, cg

    @staticmethod
    def _part_view(p: dict) -> dict:
        pl = p["plan"]
        return {"scope": p["scope"], "target": p["target"], "kind": p["kind"],
                "version": p["version"], "changed": bool(pl["changes"]),
                "changes": pl["changes"], "restart": pl["restart"] if pl["changes"] else "none",
                "restart_needed": bool(pl["changes"]) and pl["restart"] in ("app", "engine"),
                "restart_estimate": pl.get("estimate") if pl["changes"] and pl["restart"] == "engine"
                else None,
                "fingerprint_keys": pl["fingerprint_keys"],
                "grant_keys": pl.get("grant_keys") or [], "warnings": pl["warnings"]}

    def plan(self, body) -> dict:
        parts = self._parts(body)
        self._plan_parts(parts)
        return self._plan_view(parts)

    def _plan_view(self, parts: list[dict]) -> dict:
        confirm, fp, cg = self._tokens(parts)
        views = [self._part_view(p) for p in parts]
        changed = any(v["changed"] for v in views)
        return {"contract_version": CONTRACT, "change_set": "cs-" + confirm[:12],
                "parts": views, "changed": changed,
                "restart": max_restart(v["restart"] for v in views),
                "restart_needed": any(v["restart_needed"] for v in views),
                "fingerprint_keys": sorted({f"{v['scope']}/{v['target']}:{k}" for v in views
                                            for k in v["fingerprint_keys"]}),
                "grant_keys": sorted({f"{v['scope']}/{v['target']}:{k}" for v in views
                                      for k in v["grant_keys"]}),
                "needs_confirm": changed, "confirm": confirm if changed else None,
                "confirm_fingerprint": fp if changed else None,
                "confirm_grant": cg if changed else None}

    # ------------------------------------------------------------------ apply
    def apply(self, body, who: str) -> dict:
        parts = self._parts(body)
        confirm = body.get("confirm")
        cfp = body.get("confirm_fingerprint")
        cgr = body.get("confirm_grant")
        when, deadline = _parse_when(body)
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
            raise _bad(f"note is text of at most {NOTE_MAX} characters")
        extra = set(body) - {"changes", "confirm", "confirm_fingerprint", "confirm_grant", "when",
                             "deadline_s", "note"}
        if extra:
            raise _bad(f"unknown field {sorted(extra)[0]!r}")
        if confirm is not None and not isinstance(confirm, str):
            raise _bad("confirm is the plan's token")
        same = digest([[p["scope"], p["target"], p["kind"], p["version"], digest(p["sets"]),
                        sorted(p["unsets"])] for p in parts])
        got = self._replay(confirm, same)
        if got is not None:
            return got
        if not self._apply.acquire(blocking=False):
            raise SettingsError(409, "busy", "another change set is being applied: plan again "
                                             "when it is done")
        try:
            got = self._replay(confirm, same)
            if got is not None:
                return got
            self._plan_parts(parts)
            view = self._plan_view(parts)
            if not view["changed"]:
                return self._unchanged(view, parts)
            if not confirm:
                raise SettingsError(428, "confirm_required", "apply needs the plan's confirm: "
                                                             "POST the plan first", plan=view)
            if not hmac.compare_digest(confirm, view["confirm"]):
                raise SettingsError(409, "stale_plan", "the settings changed since this plan, or "
                                                       "the confirm is not its: plan again",
                                    plan=view)
            if view["confirm_fingerprint"]:
                if not cfp:
                    raise SettingsError(428, "fingerprint_confirm_required",
                                        f"{feeds(view['fingerprint_keys'])} the disk cache's "
                                        f"fingerprint: every saved prefix misses after the "
                                        f"restart; apply needs confirm_fingerprint as well",
                                        plan=view)
                if not isinstance(cfp, str) or not hmac.compare_digest(
                        cfp, view["confirm_fingerprint"]):
                    raise SettingsError(409, "stale_plan", "confirm_fingerprint is not this "
                                                           "plan's: plan again", plan=view)
            if view["confirm_grant"]:
                if not cgr:
                    raise SettingsError(428, "grant_confirm_required",
                                        f"{', '.join(view['grant_keys'])}: the full payload "
                                        f"(prompts and answers as they are) would go to a "
                                        f"destination outside the LAN; apply needs confirm_grant "
                                        f"as well", plan=view)
                if not isinstance(cgr, str) or not hmac.compare_digest(cgr, view["confirm_grant"]):
                    raise SettingsError(409, "stale_plan", "confirm_grant is not this plan's: "
                                                           "plan again", plan=view)
            out = self._run(parts, view, who=who, when=when, deadline=deadline, note=note)
            self._done[confirm] = (same, out, self.clock())
            while len(self._done) > REPLAY_KEEP:
                self._done.pop(next(iter(self._done)))
            return dict(out, replayed=False)
        finally:
            self._apply.release()

    def _replay(self, confirm, same: str) -> dict | None:
        """The result this confirm got, when the same body comes again soon and it still holds."""
        got = self._done.get(confirm) if confirm else None
        if got is None or got[0] != same or self.clock() - got[2] > REPLAY_S:
            return None
        return dict(got[1], replayed=True)

    def _restart_ended(self, rid: str, state: str) -> None:
        """A restart a result scheduled ended: unless it is done, that result holds no longer,
        and the same change set applied again is a new apply, not a replay."""
        if state == "done":
            return
        for k, v in list(self._done.items()):
            if any((p.get("restart") or {}).get("id") == rid for p in v[1]["parts"]):
                self._done.pop(k, None)

    def _unchanged(self, view: dict, parts: list[dict]) -> dict:
        return {"contract_version": CONTRACT, "change_set": view["change_set"],
                "state": "unchanged", "replayed": False,
                "parts": [self._result(p, "unchanged") for p in parts],
                "applied": [], "failed": None, "not_run": []}

    @staticmethod
    def _result(p: dict, state: str, got: dict | None = None, error=None) -> dict:
        got = got or {}
        return {"scope": p["scope"], "target": p["target"], "kind": p["kind"], "state": state,
                "keys": [c["key"] for c in p.get("plan", {}).get("changes", [])],
                "version_before": got.get("version_before"),
                "version_after": got.get("version_after"),
                "restore": got.get("restore") or [], "restart": got.get("restart"),
                "error": error}

    def _run(self, parts: list[dict], view: dict, *, who: str, when, deadline, note) -> dict:
        cs = view["change_set"]
        ctx = {"who": who, "change_set": cs, "when": when, "deadline_s": deadline, "note": note,
               "fingerprint_ok": bool(view["confirm_fingerprint"]),
               "grant_ok": bool(view["confirm_grant"])}
        results, failed = [], None
        for p in parts:
            name = f"{p['scope']}/{p['target']}"
            if failed is not None:
                results.append(self._result(p, "not_run"))
                continue
            if not p["plan"]["changes"]:
                results.append(self._result(p, "unchanged"))
                continue
            s = self.scope(p["scope"])
            keys = [c["key"] for c in p["plan"]["changes"]]
            action = "settings.restore" if p["kind"] == "restore" else "settings.apply"
            try:
                with self.cfg.change_set(cs):
                    got = (s.restore if p["kind"] == "restore" else s.apply)(
                        p["target"], p["plan"], ctx)
            except SettingsError as exc:
                failed = name
                results.append(self._result(p, "failed", error=exc.brief()))
                self._audit(who, action, name, keys, cs, "failed", {"error": exc.kind})
                self.log(f"[settings] {cs}: {name} failed ({exc.kind}); the rest is not run")
                continue
            except Exception as exc:                                # noqa: BLE001
                failed = name
                err = {"type": "internal", "message": f"{type(exc).__name__} while applying "
                                                      f"{name}"}
                results.append(self._result(p, "failed", error=err))
                self._audit(who, action, name, keys, cs, "failed",
                            {"error": type(exc).__name__})
                self.log(f"[settings] {cs}: {name} failed ({type(exc).__name__}); the rest is "
                         f"not run")
                continue
            state = "scheduled" if got.get("restart") else "applied"
            results.append(self._result(p, state, got))
            self._audit(who, action, name, keys, cs, state,
                        {"version_before": got.get("version_before"),
                         "version_after": got.get("version_after"),
                         "restart": (got.get("restart") or {}).get("id")})
            self.log(f"[settings] {cs}: {name} {state} by {who} (keys: {', '.join(keys)})")
        applied = [f"{r['scope']}/{r['target']}" for r in results
                   if r["state"] in ("applied", "scheduled")]
        not_run = [f"{r['scope']}/{r['target']}" for r in results if r["state"] == "not_run"]
        state = ("applied" if failed is None else "partial" if applied else "failed")
        self._audit(who, "settings.change_set", None, sorted({k for r in results
                                                              for k in r["keys"]}),
                    cs, state, {"applied": applied, "failed": failed, "not_run": not_run})
        return {"contract_version": CONTRACT, "change_set": cs, "state": state,
                "parts": results, "applied": applied, "failed": failed, "not_run": not_run}

    def _audit(self, who, action, obj_id, keys, cs, result, detail=None) -> None:
        d = {"result": result, **{k: v for k, v in (detail or {}).items() if v is not None}}
        with self.cfg.change_set(cs):
            self.cfg.log_audit(who, action, "settings", obj_id, detail=d, fields=keys or None)

    def audit(self, q: dict) -> dict:
        cs = q.get("change_set")
        if cs is not None and not re.match(r"^cs-[0-9a-f]{12}$", cs):
            raise _bad("change_set is a change set's id (cs-...)")
        try:
            limit = int(q.get("limit") or 100)
        except ValueError:
            raise _bad("limit is a whole number 1..500") from None
        if not 1 <= limit <= 500:
            raise _bad("limit is a whole number 1..500")
        rows = self.cfg.audit(limit, change_set=cs, objects=("settings",))
        out = []
        for r in rows:
            d = r.get("detail") or {}
            scope, _, target = (r["object_id"] or "").partition("/")
            if r["object"] != "settings":
                scope, target = None, None
            out.append({"at": iso(r["ts_ms"]), "ts_ms": r["ts_ms"], "who": r["who"] or "",
                        "action": r["action"], "object": r["object"],
                        "object_id": r["object_id"], "scope": scope or None,
                        "target": target or None, "keys": r["fields"] or [],
                        "change_set": r["change_set"], "result": d.get("result")})
        return {"contract_version": CONTRACT, "rows": out}

    # ------------------------------------------------------------------ one scope's routes
    def _one(self, scope: str, body, restore: bool = False) -> dict:
        if not isinstance(body, dict):
            raise _bad("the body is an object")
        known = {"target", "confirm", "confirm_fingerprint", "confirm_grant", "when",
                 "deadline_s", "note"}
        known |= {"version"} if restore else {"set", "unset"}
        extra = set(body) - known
        if extra:
            raise _bad(f"unknown field {sorted(extra)[0]!r}")
        c = {"scope": scope}
        if body.get("target") is not None:
            c["target"] = body["target"]
        if restore:
            if "version" not in body:
                raise _bad("version is the id of the version to restore")
            c["restore"] = body["version"]
        else:
            for k in ("set", "unset"):
                if k in body:
                    c[k] = body[k]
        out = {"changes": [c]}
        for k in ("confirm", "confirm_fingerprint", "confirm_grant", "when", "deadline_s", "note"):
            if k in body:
                out[k] = body[k]
        return out

    def handle(self, method: str, parts: list[str], q: dict, body, who: str,
               trust: bool = False) -> tuple[int, dict]:
        """`parts`: the path after /v1/settings. `trust`: the caller has pairing's rights."""
        _CALLER.trust = bool(trust)
        try:
            return 200, self._route(method, parts, q, body, who)
        except SettingsError as exc:
            return exc.status, exc.body()
        finally:
            _CALLER.trust = False

    def _route(self, method, parts, q, body, who) -> dict:
        target = q.get("target")
        if method == "GET" and not parts:
            return self.overview()
        if parts == ["plan"] and method == "POST":
            return self.plan(body)
        if parts == ["apply"] and method == "POST":
            return self.apply(body, who)
        if parts == ["audit"] and method == "GET":
            return self.audit(q)
        if parts[:1] == ["restarts"]:
            if self.restarts is None:
                raise SettingsError(404, "not_found", "no engine scope here")
            if method == "GET" and len(parts) == 1:
                return {"contract_version": CONTRACT, "restarts": self.restarts.list()}
            if method == "GET" and len(parts) == 2:
                return {"contract_version": CONTRACT, **self.restarts.view(parts[1])}
            if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                return {"contract_version": CONTRACT, **self.restarts.cancel(parts[1], who)}
            raise SettingsError(404, "not_found", f"no route {method} /v1/settings/"
                                                  f"{'/'.join(parts)}")
        if parts and parts[0] in SCOPES:
            scope = parts[0]
            rest = parts[1:]
            if method == "GET" and not rest:
                return self.get(scope, target)
            if method == "GET" and rest == ["schema"]:
                return self.schema(scope, target)
            if method == "GET" and rest == ["versions"]:
                return self.versions(scope, target)
            if method == "POST" and rest == ["plan"]:
                b = self._one(scope, body)
                return self.plan(b)
            if method == "POST" and rest == ["apply"]:
                return self.apply(self._one(scope, body), who)
            if method == "POST" and rest == ["restore"]:
                b = self._one(scope, body, restore=True)
                if not b.get("confirm"):
                    return self.plan(b)
                return self.apply(b, who)
        raise SettingsError(404, "not_found", f"no route {method} /v1/settings"
                                              f"{'/' if parts else ''}{'/'.join(parts)}")
