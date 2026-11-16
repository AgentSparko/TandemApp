"""The hooks scope of the settings interface over the hook registry (SRV-845, proxy/settingscopes.py
`HooksScope`, `HooksAdapter`): proxy/app.py plugs `RegistryHooks` in.

THE KEYS, one set a hook (proxy/hookstore.py; `<id>` the hook's id):

    hooks.<id>.enabled      boolean       called at all
    hooks.<id>.mode         off | shadow | on
    hooks.<id>.priority     integer       the order among its stage's hooks (low first)
    hooks.<id>.scope        object        {models, backends, profiles, users, sessions,
                                          client_kinds}: lists of patterns; unset: everything
    hooks.<id>.policy       open | closed the failure policy; unset: the stage's default
    hooks.<id>.timeout_ms   integer       unset: the stage's budget
    hooks.<id>.payload      meta | redacted | full
    hooks.<id>.secret       secret        an http hook's signing secret: set or unset, never
                                          written here (the hooks' API makes it and answers it
                                          once)

What a hook calls, its stages, role and kind stay on the hooks' API (/v1/fleet/hooks), as adding
and removing a hook do. Each value row carries the hook's name as its `label`. The checks are the
registry's own (`HookStore._check`), run on the hook as it would be; a write is the registry's
update, audited there, and the runner reads it at the next request (restart class none).

THE FULL PAYLOAD (SEC-16) keeps its rules. A set of `payload` to `full` that grants it (the hook is
not full yet, or its grant names another destination) is a grant: `grants()` lists it with the
destination's host and whether it is on the LAN, and the scope plans it only for a caller with
pairing's rights. A destination outside the LAN needs the plan's second confirm (`confirm_grant`);
once the apply carries it, the write hands the registry the confirm the registry would have
answered for that hook, host and version, so the grant records who confirmed it. Without it the
registry refuses as it does on its own API (409 confirm_needed, here `store_refused`).

Reads and checks go through the stores' public reads and `_check`; writes through `update`.
"""

from __future__ import annotations

from proxy.hookstore import HookError
from proxy.settings import SettingsError, key_spec
from proxy.settingscopes import HooksAdapter

#: a hook's keys: field -> (type, nullable, default, the key's sentence)
HOOK_KEYS = {
    "enabled": ("boolean", False, True, "called at all"),
    "mode": ("enum", False, "on", "off: never called; shadow: called on a copy, nothing applied; "
                                  "on: as its role says"),
    "priority": ("integer", False, 100, "the order among the hooks of a stage, low first; ties by "
                                        "id"),
    "scope": ("object", True, {}, "the requests it runs on: lists of patterns under models, "
                                  "backends, profiles, users, sessions or client_kinds; unset: "
                                  "every request"),
    "policy": ("enum", True, None, "when it fails: open (as if absent) or closed (the request is "
                                   "refused); unset: the stage's default"),
    "timeout_ms": ("integer", True, None, "its own time limit in milliseconds, at most the "
                                          "stage's budget; unset: the stage's budget"),
    "payload": ("enum", False, "meta", "what it sees: meta, redacted, or full (prompts and "
                                       "answers as they are: pairing's rights, and a second "
                                       "confirm outside the LAN)"),
}
ENUMS = {"mode": ("off", "shadow", "on"), "policy": ("open", "closed"),
         "payload": ("meta", "redacted", "full")}
SECRET_WHY = "made by the hooks' API and answered there once: rotate it there"


def _refused(exc, key: str | None) -> SettingsError:
    status = exc.status if exc.status in (404, 409) else 422
    return SettingsError(status, "store_refused", f"{key + ': ' if key else ''}{exc.message}",
                         key=key)


class RegistryHooks(HooksAdapter):
    """`runner`: proxy/hooks.py Runner (its `store` is the registry)."""

    def __init__(self, runner):
        self.runner = runner

    @property
    def hooks(self):
        return self.runner.store

    # ------------------------------------------------------------------ the keys
    def _hook_rows(self) -> list[dict]:
        return self.hooks.list() if self.hooks.ready() else []

    def _index(self) -> dict:
        """key -> (kind, id, field): every key there is now."""
        out = {}
        for h in self._hook_rows():
            for f in HOOK_KEYS:
                out[f"hooks.{h['id']}.{f}"] = ("hook", h["id"], f)
            if h["kind"] == "http":
                out[f"hooks.{h['id']}.secret"] = ("hook", h["id"], "secret")
        return out

    def keys(self) -> list:
        out = []
        for h in self._hook_rows():
            p = f"hooks.{h['id']}."
            for f, (t, nullable, default, text) in HOOK_KEYS.items():
                out.append(key_spec(p + f, t, values=ENUMS.get(f), nullable=nullable,
                                    default=default, text=text))
            if h["kind"] == "http":
                out.append(key_spec(p + "secret", "string", secret=True, writable=False,
                                    why=SECRET_WHY, text="the secret that signs its calls"))
        return out

    def values(self) -> dict:
        out = {}
        for h in self._hook_rows():
            p = f"hooks.{h['id']}."
            for f in HOOK_KEYS:
                v = h[f]
                if v is None or (f == "scope" and not v):
                    continue                  # holds nothing: unset
                out[p + f] = v
        return out

    def extras(self) -> dict:
        out = {}
        for h in self._hook_rows():
            p = f"hooks.{h['id']}."
            for f in HOOK_KEYS:
                out[p + f] = {"label": h["name"]}
            if h["kind"] == "http":
                hint = self.hooks.secret_hint(h["id"], h["secret_ref"])
                out[p + "secret"] = {"label": h["name"], "hint": hint.get("sha256_6")}
        return out

    def is_set(self, key: str) -> bool:
        got = self._index().get(key)
        if got is None or got[2] != "secret":
            return False
        h = self.hooks.get(got[1])
        return bool(h) and self.hooks.secret_hint(h["id"], h["secret_ref"])["set"]

    def empty_why(self) -> str | None:
        return ("no hook yet: hooks are added on the hooks' API (/v1/fleet/hooks); their "
                "switches show here once there is one")

    # ------------------------------------------------------------------ a change
    def _patches(self, sets: dict, unsets) -> dict:
        """id -> {field: value} for the registry's update, with the key of each field."""
        idx = self._index()
        out: dict = {}
        for k in list(sets) + list(unsets):
            kind, oid, f = idx[k]
            if k in sets:
                v = sets[k]
                if f == "scope" and v is None:
                    v = {}
            else:
                if not HOOK_KEYS[f][1]:
                    raise SettingsError(422, "bad_value", f"{k} has no unset: set a value", key=k)
                v = {} if f == "scope" else None
            out.setdefault(oid, {})[f] = v
        return out

    def _new(self, hid: str, patch: dict) -> tuple[dict, dict]:
        old = self.hooks.need(hid)
        new = dict(old)
        new.update(patch)
        return old, new

    def check(self, sets, unsets) -> list:
        for hid, patch in self._patches(sets, unsets).items():
            try:
                _, new = self._new(hid, patch)
                self.hooks._check(new)
            except HookError as exc:
                f = (exc.field or "").partition(".")[0]
                key = f"hooks.{hid}.{f}" if f in HOOK_KEYS else None
                raise SettingsError(422, "bad_value", f"{key or 'hooks.' + hid}: {exc.message}",
                                    key=key) from None
        return []

    def grants(self, sets) -> list:
        out = []
        for hid, patch in self._patches({k: v for k, v in sets.items()
                                         if k.endswith(".payload")}, []).items():
            if patch.get("payload") != "full":
                continue
            old, new = self._new(hid, patch)
            dest = self.hooks.destination(new)
            g = old.get("grant") or {}
            if old["payload"] == "full" and g and g.get("destination") == dest["host"]:
                continue                      # granted already, to the same destination
            out.append({"key": f"hooks.{hid}.payload", "hook": hid,
                        "destination": dest["host"], "lan": bool(dest["lan"]),
                        "version": old["version"]})
        return out

    def write(self, sets, unsets, who, grants=None) -> None:
        grants = grants or {}
        for hid, patch in sorted(self._patches(sets, unsets).items()):
            g = grants.get(f"hooks.{hid}.payload")
            confirm = self.hooks.confirm_token(hid, g["destination"], g["version"]) if g else None
            try:
                self.hooks.update(hid, patch, who=who, confirm=confirm)
            except HookError as exc:
                f = (exc.field or "").partition(".")[0]
                raise _refused(exc, f"hooks.{hid}.{f}" if f in HOOK_KEYS
                               else f"hooks.{hid}") from None
