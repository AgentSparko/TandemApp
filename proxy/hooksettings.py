"""The hooks scope of the settings interface over the hook registry and the decision providers
(SRV-845, SRV-851; proxy/settingscopes.py `HooksScope`, `HooksAdapter`): proxy/app.py plugs
`RegistryHooks` in. The providers are keys of this scope, not a scope of their own: a `hook`
provider names a decision.ask hook, and both run around the same requests.

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

THE KEYS, one set a decision provider (proxy/providerstore.py; `<id>` the provider's id):

    providers.<id>.enabled      boolean
    providers.<id>.mode         on | shadow | ab
    providers.<id>.ab_percent   integer 0..100: the share of sessions (users, requests) in ab
    providers.<id>.sticky       session | user | request: what the ab share sticks to
    providers.<id>.questions    object: the questions it answers, question id -> {model?,
                                endpoint?, timeout_ms?} (its keys are keys of their own)
    providers.<id>.api_key      secret: the provider's own key (systemone-http only)
    providers.<id>.questions.<question>.api_key
                                secret: one question's key (systemone-http only)

A key is write-only: set it with a string, clear it with an unset (or null); an answer says only
whether one is set, with its hint. One enabled provider serves a question on or ab: a change set
that moves a question from one provider to another applies the one that lets it go first.

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
from proxy.providerstore import MODES, QUESTION_FIELDS, QUESTIONS, STICKY, ProviderError
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
#: a decision provider's keys: field -> (type, values, default, the key's sentence)
PROVIDER_KEYS = {
    "enabled": ("boolean", None, True, "asked at all"),
    "mode": ("enum", MODES, "shadow", "on: it answers its questions; shadow: asked beside the "
                                      "conductor, nothing it says is used; ab: it answers for a "
                                      "share"),
    "ab_percent": ("integer", None, 0, "in ab, the share (0..100) it answers"),
    "sticky": ("enum", STICKY, "session", "in ab, what the share sticks to"),
    "questions": ("object", None, None, "the questions it answers ("
                                        + ", ".join(QUESTIONS) + "), each {model?, endpoint?, "
                                        "timeout_ms?}"),
}
SECRET_WHY = "made by the hooks' API and answered there once: rotate it there"


def _refused(exc, key: str | None) -> SettingsError:
    status = exc.status if exc.status in (404, 409) else 422
    return SettingsError(status, "store_refused", f"{key + ': ' if key else ''}{exc.message}",
                         key=key)


class RegistryHooks(HooksAdapter):
    """`runner`: proxy/hooks.py Runner (its `store` is the registry)."""

    def __init__(self, runner, providers=None):
        self.runner = runner
        self.providers = providers          # proxy/providerstore.py ProviderStore, or None

    @property
    def hooks(self):
        return self.runner.store

    def _provider_rows(self) -> list[dict]:
        if self.providers is None or not self.providers.ready():
            return []
        return self.providers.list()

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
        for p in self._provider_rows():
            for f in PROVIDER_KEYS:
                out[f"providers.{p['id']}.{f}"] = ("provider", p["id"], f)
            if p["kind"] == "systemone-http":
                out[f"providers.{p['id']}.api_key"] = ("provider", p["id"], "api_key")
                for q in p["questions"]:
                    out[f"providers.{p['id']}.questions.{q}.api_key"] = ("provider", p["id"],
                                                                         "questions." + q)
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
        for row in self._provider_rows():
            p = f"providers.{row['id']}."
            for f, (t, values, default, text) in PROVIDER_KEYS.items():
                out.append(key_spec(p + f, t, values=values, default=default, text=text))
            if row["kind"] == "systemone-http":
                out.append(key_spec(p + "api_key", "string", secret=True, nullable=True,
                                    text="its API key, sent as the bearer"))
                for q in row["questions"]:
                    out.append(key_spec(f"{p}questions.{q}.api_key", "string", secret=True,
                                        nullable=True, text=f"the API key for {q} alone"))
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
        for row in self._provider_rows():
            for f in PROVIDER_KEYS:
                out[f"providers.{row['id']}.{f}"] = row[f]
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
        for row in self._provider_rows():
            p = f"providers.{row['id']}."
            for f in PROVIDER_KEYS:
                out[p + f] = {"label": row["name"]}
            if row["kind"] == "systemone-http":
                hints = self.providers.key_hints(row["id"])
                out[p + "api_key"] = {"label": row["name"],
                                      "hint": (hints.get("*") or {}).get("sha256_6")}
                for q in row["questions"]:
                    out[f"{p}questions.{q}.api_key"] = {
                        "label": row["name"], "hint": (hints.get(q) or {}).get("sha256_6")}
        return out

    def is_set(self, key: str) -> bool:
        got = self._index().get(key)
        if got is None:
            return False
        if got[0] == "provider":
            scope = "*" if got[2] == "api_key" else got[2].partition(".")[2]
            return scope in self.providers.key_hints(got[1])
        if got[2] != "secret":
            return False
        h = self.hooks.get(got[1])
        return bool(h) and self.hooks.secret_hint(h["id"], h["secret_ref"])["set"]

    def empty_why(self) -> str | None:
        return ("no hook and no decision provider yet: they are added on the hooks' API "
                "(/v1/fleet/hooks, /v1/fleet/decision-providers); their switches show here once "
                "there is one")

    # ------------------------------------------------------------------ a change
    def _patches(self, sets: dict, unsets) -> dict:
        """id -> {field: value} for the registry's update, with the key of each field."""
        idx = self._index()
        out: dict = {}
        for k in list(sets) + list(unsets):
            kind, oid, f = idx[k]
            if kind != "hook":
                continue
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
        self._provider_plan(sets, unsets)
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
        plan = self._provider_plan(sets, unsets)
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
        for pid, patch in plan:
            try:
                self.providers.update(pid, patch, who=who)
            except ProviderError as exc:
                raise _refused(exc, self._provider_key(pid, exc.field)) from None

    # ------------------------------------------------------------------ the providers' change
    def _provider_key(self, pid: str, field: str | None) -> str:
        """The key a provider store's refusal is about."""
        f = field or ""
        if f == "api_key" or f in PROVIDER_KEYS:
            return f"providers.{pid}.{f}"
        if f.startswith("questions.") and f.endswith(".api_key"):
            return f"providers.{pid}.{f}"
        if f.startswith("questions"):
            return f"providers.{pid}.questions"
        return f"providers.{pid}.{f}" if f else f"providers.{pid}"

    def _provider_plan(self, sets: dict, unsets) -> list[tuple[str, dict]]:
        """[(id, the store's update patch)] in the order they are written: a provider that lets a
        question go first, then one that takes it. Raises SettingsError for what the store would
        refuse, on the key it is about."""
        idx = self._index()
        fields: dict = {}                    # id -> {field: value}
        keys: dict = {}                      # id -> {"*" | question: key or None}
        for k in list(sets) + list(unsets):
            kind, pid, f = idx[k]
            if kind != "provider":
                continue
            if f == "api_key" or f.startswith("questions."):
                v = sets.get(k) if k in sets else None
                keys.setdefault(pid, {})["*" if f == "api_key" else f.partition(".")[2]] = v
                continue
            if k not in sets:
                raise SettingsError(422, "bad_value", f"{k} has no unset: set a value", key=k)
            v = sets[k]
            if f == "questions":
                if not isinstance(v, dict) or not v:
                    raise SettingsError(422, "bad_value", f"{k} is an object of the questions "
                                        f"it answers ({', '.join(QUESTIONS)})", key=k)
                for q, spec in v.items():
                    if isinstance(spec, dict) and "api_key" in spec:
                        raise SettingsError(422, "bad_value", f"{k}: a question's API key is a key "
                                            f"of its own (providers.{pid}.questions.{q}.api_key)",
                                            key=k)
            fields.setdefault(pid, {})[f] = v
        if not fields and not keys:
            return []
        rows = {p["id"]: p for p in self.providers.list()}
        out, news = [], {}
        for pid in sorted(set(fields) | set(keys)):
            old = rows[pid]
            patch = dict(fields.get(pid, {}))
            mapping = patch.get("questions", old["questions"])
            ks = keys.get(pid, {})
            for q in ks:
                if q != "*" and q not in mapping:
                    raise SettingsError(422, "bad_value", f"providers.{pid}.questions.{q}.api_key: "
                                        f"{pid} does not answer {q}",
                                        key=f"providers.{pid}.questions.{q}.api_key")
            if any(q != "*" for q in ks) or "questions" in patch:
                qs = {q: {f: v for f, v in (spec or {}).items() if f in QUESTION_FIELDS}
                      if isinstance(spec, dict) else spec for q, spec in mapping.items()}
                for q, v in ks.items():
                    if q != "*":
                        qs[q] = dict(qs[q], api_key=v)
                patch["questions"] = qs
            if "*" in ks:
                patch["api_key"] = ks["*"]
            new = dict(old)
            new.update({f: v for f, v in patch.items() if f != "api_key"})
            new["api_key"] = patch.get("api_key")
            try:
                self.providers._check(new)
            except ProviderError as exc:
                key = self._provider_key(pid, exc.field)
                raise SettingsError(422, "bad_value", f"{key}: {exc.message}", key=key) from None
            news[pid] = new
            out.append((pid, patch))
        # one enabled provider a question on or ab, across the providers as they would be
        serving: dict = {}
        for pid, row in sorted(rows.items()):
            row = news.get(pid, row)
            if not row["enabled"] or row["mode"] == "shadow":
                continue
            for q in row["questions"]:
                if q in serving:
                    who = pid if pid in news else serving[q]
                    raise SettingsError(422, "bad_value", (
                        f"providers.{who}: {q} is served {row['mode']} by {serving[q]} and "
                        f"{pid}; one provider a question on or ab (any number in shadow)"),
                        key=f"providers.{who}.mode")
                serving[q] = pid

        def takes(item) -> bool:
            pid, _ = item
            old, new = rows[pid], news[pid]
            had = set(old["questions"]) if old["enabled"] and old["mode"] != "shadow" else set()
            has = set(new["questions"]) if new["enabled"] and new["mode"] != "shadow" else set()
            return bool(has - had)
        return sorted(out, key=lambda item: (takes(item), item[0]))
