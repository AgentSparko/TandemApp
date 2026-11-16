"""The settings scopes over this controller's own stores (SRV-845, proxy/settings.py): app,
backends, conductor and hooks. The engine scope is proxy/settingsengine.py.

APP (target `controller`): what this TandemApp runs, the page's switches (proxy/appparts.py).

    chat        on | off    Chat: this controller's own chatd, or the forward to another host's
    conductor   on | off    the conductor in front of the backends, its documents kept

A set goes through the switch itself (Parts: its plan, then its confirm), so it does what the
switch does: chatd starts or stops, the conductor runs from the next request. Restart class none.
`source` is `page` once switched, `install` while the env file decides (TANDEM_CHAT,
TANDEM_CONDUCTOR); unset hands the key back to the install (switched to the install's choice when
that differs, then forgotten).

BACKENDS (target `controller`): the config database's backends, routes and users (proxy/config.py).

    backends.<id>.<field>   label, url, enabled, models_mode, models, fallback, max_inflight,
                            inject_usage, timeouts.connect|first_byte|idle,
                            harness.mode|profile|writer|..., host.mode|url|dcgm_url
    backends.<id>.key       the backend's API key: a secret, write-only (set or unset)
    routes                  the routing rules in order: [{pattern, backend, enabled, variable?}]
    users.<id>.enabled      a user's key on or off

A field that holds nothing is unset (`set` false); unsetting it again is no change. A field the
environment sets is not writable (its `variable` says which). The checks are the
Backends screen's (config.schema.json, fallbacks, the environment's rules); restart class none.
Adding or removing a backend or a user stays on the Backends screen, which hands the key out once.

CONDUCTOR (target `controller`): this controller's conductor documents (tandemapp/conductor/
store.py): settings, router, roles, packages, monitor, residency, deciders and profiles.<name>, each
key one whole document (type document). The store keeps the versions; a set writes a new version
(the store's checks, `base` the version planned against), unset goes back to the built-in one, a
restore writes a new version equal to an old one. Restart class hot: the conductor reads the new
version at the next request. While the conductor is off the documents are still kept, in the folder
it would use, and read when it is switched on. A version's id is `<key>@<n>`.

HOOKS (target `controller`): an adapter the hook registry plugs into (HooksAdapter below); until one
is given the scope answers an empty schema and refuses every key. proxy/hooksettings.py is the
registry's: each hook's switches and each decision provider's, its API keys write-only
(proxy/app.py plugs it in). A key
that grants a hook the full payload is planned for a caller with pairing's rights only (403
`trust_required`), and one whose destination is outside the LAN adds the second confirm
(`grant_keys`, `confirm_grant`; SEC-16). Restart class none: the runner reads the registry again at
the next request.
"""

from __future__ import annotations

import copy

from proxy import appparts
from proxy import config as config_mod
from proxy import schema as schema_mod
from proxy import settings as settings_mod
from proxy.settings import (KeyValueScope, Scope, SettingsError, change, digest, key_spec,
                            part_plan)


# ======================================================================================== app
class AppScope(KeyValueScope):
    KEYS = {"chat": "Chat: this controller's own chatd, or the forward to another host's",
            "conductor": "the conductor in front of the backends"}

    def __init__(self, cfg, parts):
        super().__init__(cfg)
        self.parts = parts                         # proxy/appparts.py Parts, or None

    def writable(self, target):
        if self.parts is None:
            return False, "this server has no switches (a plain proxy)"
        return True, None

    def _install(self, key: str) -> str:
        p = self.parts
        if key == "chat":
            return "off" if p.install_chat == "off" else "on"
        return "off" if p.install_conductor == "off" else "on"

    def spec(self, target):
        ok, why = self.writable(target)
        out = {}
        for k, text in self.KEYS.items():
            out[k] = key_spec(k, "enum", values=("on", "off"), restart="none",
                              default=self._install(k) if self.parts else None,
                              writable=ok, why=why, text=text)
        return out

    def values(self, target):
        out = {}
        for k in self.KEYS:
            if self.parts is None:
                out[k] = (None, False, "install", None)
                continue
            on = self.parts.chat_on() if k == "chat" else self.parts.conductor_dir() is not None
            stored = appparts.stored(self.cfg, k)
            out[k] = ("on" if on else "off", stored is not None,
                      "page" if stored is not None else "install", None)
        return out

    def check(self, target, sets, unsets, spec):
        for k, v in sets.items():
            if v not in ("on", "off"):
                raise SettingsError(422, "bad_value", f"{k} is on or off", key=k)
        return []

    def _switch(self, key: str, on: bool, who: str) -> None:
        plan = self.parts.plan(key, on)
        if not plan["changed"]:
            # the state holds already: the page's choice is stored all the same, so a later
            # install default does not flip it (appparts: once switched, the page wins)
            self.cfg.set_setting(appparts.KEYS[key], "on" if on else "off", who=who)
            return
        try:
            self.parts.switch(key, {"on": on, "confirm": plan["confirm"]}, who)
        except appparts.PartsError as exc:
            raise SettingsError(exc.status, exc.kind, exc.message, key=key) from None

    def write(self, target, sets, unsets, who):
        for k in sorted(sets):
            self._switch(k, sets[k] == "on", who)
        for k in sorted(unsets):
            self._switch(k, self._install(k) == "on", who)
            self.cfg.delete_setting(appparts.KEYS[k], who=who)


# ======================================================================================== backends
def _flat_fields() -> dict:
    """field -> its JSON Schema, from config.schema.json's backend_patch (`kind` and `key`
    aside)."""
    doc = schema_mod.doc()["$defs"]
    out = {}
    for name, sch in doc["backend_patch"]["properties"].items():
        if name in ("kind", "key"):
            continue
        if "$ref" in sch:
            sub = doc[sch["$ref"].split("/")[-1]]
            for f, fs in sub["properties"].items():
                out[f"{name}.{f}"] = fs
        else:
            out[name] = sch
    return out


def _type_of(sch: dict) -> tuple[str, bool, list | None]:
    t = sch.get("type")
    types = [t] if isinstance(t, str) else list(t or [])
    nullable = "null" in types
    types = [x for x in types if x != "null"]
    if "enum" in sch:
        return "enum", nullable, list(sch["enum"])
    kind = types[0] if types else "string"
    return {"array": "list"}.get(kind, kind), nullable, None


#: the defaults a new backend has (proxy/config.py `_row_backend`), where one is fixed
FIELD_DEFAULTS = {"enabled": True, "models_mode": "discover", "models": [], "fallback": [],
                  "max_inflight": None, "harness.mode": "off", "harness.profile": None,
                  "host.mode": "auto", "host.url": None, "host.dcgm_url": None, "label": None}


class BackendsScope(KeyValueScope):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.fields = _flat_fields()

    @staticmethod
    def _get(b: dict, field: str):
        a, _, sub = field.partition(".")
        v = b.get(a)
        return (v or {}).get(sub) if sub else v

    def spec(self, target):
        out = {}
        for b in self.cfg.backends():
            locks = self.cfg.backend_locks(b["id"])
            for f, sch in self.fields.items():
                k = f"backends.{b['id']}.{f}"
                t, nullable, values = _type_of(sch)
                var = locks.get(f) or locks.get(f.partition(".")[0])
                out[k] = key_spec(k, t, values=values, nullable=nullable,
                                  default=FIELD_DEFAULTS.get(f), writable=var is None,
                                  why=f"set by {var}" if var else None, source_var=var)
            var = locks.get("key")
            out[f"backends.{b['id']}.key"] = key_spec(
                f"backends.{b['id']}.key", "string", secret=True, nullable=True,
                writable=var is None, why=f"set by {var}" if var else None, source_var=var,
                text="the backend's API key")
        env_rules = [r["variable"] for r in self.cfg.rules() if r["source"] == "env"]
        out["routes"] = key_spec("routes", "list", default=[],
                                 text="the routing rules in order: a model id that matches a "
                                      "pattern goes to its backend"
                                      + (f"; {', '.join(env_rules)} keep their place and value"
                                         if env_rules else ""))
        for u in self.cfg.users():
            if u["id"] is None:
                continue
            k = f"users.{u['id']}.enabled"
            out[k] = key_spec(k, "boolean", default=True, text=f"the key of {u['label']}")
        return out

    def values(self, target):
        out = {}
        for b in self.cfg.backends():
            locks = self.cfg.backend_locks(b["id"])
            for f in self.fields:
                var = locks.get(f) or locks.get(f.partition(".")[0])
                v = copy.deepcopy(self._get(b, f))
                # set: the database (or the environment) holds a value; a field that holds
                # nothing is unset, and unsetting it again is no change
                out[f"backends.{b['id']}.{f}"] = (
                    v, v is not None, "env" if var else "database",
                    {"variable": var} if var else None)
            h = self.cfg.key_hint(b["id"])
            out[f"backends.{b['id']}.key"] = (
                None, bool(h.get("key_set")), h.get("source") or "unset",
                {"hint": h.get("sha256_6")})
        out["routes"] = ([{"pattern": r["pattern"], "backend": r["backend"],
                           "enabled": r["enabled"],
                           **({"variable": r["variable"]} if r["variable"] else {})}
                          for r in self.cfg.rules()], True, "database", None)
        for u in self.cfg.users():
            if u["id"] is None:
                continue
            out[f"users.{u['id']}.enabled"] = (u["enabled"], True, "database",
                                               {"label": u["label"]})
        return out

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        _, bid, f = key.split(".", 2)
        return bid, f

    def _patches(self, sets: dict, unsets: list, spec: dict) -> dict:
        patches: dict = {}
        for k in list(sets) + list(unsets):
            if not k.startswith("backends."):
                continue
            bid, f = self._split(k)
            if k in unsets and not spec[k]["nullable"]:
                raise SettingsError(422, "bad_value", f"{k} has no unset: set a value", key=k)
            v = sets.get(k) if k in sets else None
            a, _, sub = f.partition(".")
            p = patches.setdefault(bid, {})
            if sub:
                p.setdefault(a, {})[sub] = v
            else:
                p[a] = v
        return patches

    def _rules(self, value) -> list:
        if not isinstance(value, list):
            raise SettingsError(422, "bad_value", "routes is a list of {pattern, backend, "
                                                  "enabled}", key="routes")
        envs = {r["variable"]: r for r in self.cfg.rules() if r["source"] == "env"}
        out = []
        for i, r in enumerate(value):
            if not isinstance(r, dict) or set(r) - {"pattern", "backend", "enabled", "variable"}:
                raise SettingsError(422, "bad_value", f"routes[{i}] is {{pattern, backend, "
                                                      f"enabled, variable?}}", key="routes")
            r = dict(r)
            var = r.pop("variable", None)
            if var is not None:
                if var not in envs:
                    raise SettingsError(422, "bad_value", f"routes[{i}]: no rule is set by "
                                                          f"{var}", key="routes")
                r["id"] = envs[var]["id"]
            out.append(r)
        return out

    def check(self, target, sets, unsets, spec):
        patches = self._patches(sets, unsets, spec)
        for bid, patch in patches.items():
            body = {k: v for k, v in patch.items() if k != "key"}
            errs = schema_mod.validate(body, "backend_patch")
            if errs:
                raise SettingsError(422, "bad_value", f"backends.{bid}.{errs[0][0]} {errs[0][1]}",
                                    key=f"backends.{bid}.{errs[0][0]}")
            k = patch.get("key")
            if k is not None and (not isinstance(k, str) or not k or len(k) > 4096):
                raise SettingsError(422, "bad_value", f"backends.{bid}.key is a key of at most "
                                                      f"4096 characters", key=f"backends.{bid}.key")
            if "fallback" in patch:
                try:
                    self.cfg._check_fallback(bid, patch["fallback"])
                except config_mod.ConfigError as exc:
                    raise SettingsError(422, "bad_value", f"backends.{bid}.{exc.message}",
                                        key=f"backends.{bid}.fallback") from None
        if "routes" in unsets:
            raise SettingsError(422, "bad_value", "routes has no unset: set [] for none",
                                key="routes")
        if "routes" in sets:
            try:
                self.cfg.check_rules(self._rules(sets["routes"]))
            except config_mod.ConfigError as exc:
                raise SettingsError(422, "bad_value", f"routes: {exc.message}", key="routes",
                                    variable=exc.var) from None
        for k, v in sets.items():
            if k.startswith("users.") and not isinstance(v, bool):
                raise SettingsError(422, "bad_value", f"{k} is true or false", key=k)
        for k in unsets:
            if k.startswith("users."):
                raise SettingsError(422, "bad_value", f"{k} has no unset: set true or false",
                                    key=k)
        return []

    def write(self, target, sets, unsets, who):
        spec = self.spec(target)
        try:
            for bid, patch in sorted(self._patches(sets, unsets, spec).items()):
                self.cfg.update_backend(bid, patch, who=who)
            if "routes" in sets:
                self.cfg.set_rules(self._rules(sets["routes"]), who=who)
            for k in sorted(sets):
                if k.startswith("users."):
                    self.cfg.update_user(int(k.split(".")[1]), {"enabled": sets[k]}, who=who)
        except config_mod.ConfigError as exc:
            raise SettingsError(exc.status if exc.status in (404, 409) else 422,
                                "store_refused", exc.message, variable=exc.var) from None


# ======================================================================================== conductor
class ConductorScope(Scope):
    """`store_of()` -> (the conductor's Store, whether the conductor runs now), or (None, why)."""

    def __init__(self, store_of):
        self.store_of = store_of

    def _store(self):
        got = self.store_of()
        st = got[0] if isinstance(got, tuple) else got
        if st is None:
            raise SettingsError(409, "read_only", (got[1] if isinstance(got, tuple) else None)
                                or "this server keeps no conductor documents")
        return st, (got[1] if isinstance(got, tuple) else True)

    @staticmethod
    def _kinds(store) -> list[tuple[str, str, str | None]]:
        from tandemapp.conductor import store as store_mod
        out = [(k, k, None) for k in store_mod.KINDS if k != "profiles"]
        out += [(f"profiles.{n}", "profiles", n) for n in store.names()]
        return out

    def _key(self, store, key: str) -> tuple[str, str | None]:
        for k, kind, name in self._kinds(store):
            if k == key:
                return kind, name
        raise SettingsError(422, "unknown_key", f"not a document of the conductor: {key}",
                            keys=[key])

    def schema(self, target):
        from tandemapp.conductor import schema as cschema
        store, on = self._store()
        keys = []
        for k, kind, name in self._kinds(store):
            locked = name in cschema.LOCKED if kind == "profiles" else False
            ro = store.readonly
            keys.append(key_spec(k, "document", restart="hot", default=None,
                                 writable=not locked and not ro,
                                 why=(f"locked: {cschema.LOCKED[name]}" if locked else
                                      "this server's documents are read-only" if ro else None),
                                 text=f"the conductor's {kind[:-1] if kind == 'profiles' else kind}"
                                      f"{' ' + name if name else ''} document"))
        return {"writable": not store.readonly,
                "why": None if on else "the conductor is off: its documents are kept and read "
                                       "when it is switched on", "keys": keys}

    def get(self, target):
        store, on = self._store()
        rows = []
        for k, kind, name in self._kinds(store):
            v, doc = store.get(kind, name)
            rows.append({"key": k, "value": doc, "set": v > 0,
                         "source": "stored" if v > 0 else "built-in", "version": f"{k}@{v}"})
        return {"writable": not store.readonly,
                "why": None if on else "the conductor is off: its documents are kept and read "
                                       "when it is switched on",
                "version": None, "values": rows}

    def _doc_change(self, store, key, kind, name, new) -> tuple[dict | None, list]:
        from tandemapp.conductor import store as store_mod
        v, cur = store.get(kind, name)
        errs = store.check(kind, name, new)
        if errs:
            raise SettingsError(422, "invalid_document", f"{key}: {errs[0]}", key=key,
                                errors=errs[:20])
        d = store_mod.diff(cur, new)
        if not d:
            return None, []
        return change(key, {"version": v}, {"version": "new"}, was_set=v > 0, is_set=True,
                      restart="hot", diff=d), d

    def plan(self, target, sets, unsets):
        from tandemapp.conductor import schema as cschema
        store, on = self._store()
        if store.readonly:
            raise SettingsError(409, "read_only", "this server's conductor documents are "
                                                  "read-only")
        changes, state, docs = [], {}, {}
        for key in sorted(set(sets) | set(unsets)):
            kind, name = self._key(store, key)
            if kind == "profiles" and name in cschema.LOCKED:
                raise SettingsError(422, "locked", f"{key} is locked: {cschema.LOCKED[name]}",
                                    key=key)
            v, _ = store.get(kind, name)
            state[key] = v
            if key in sets:
                new = sets[key]
                if not isinstance(new, dict):
                    raise SettingsError(422, "bad_value", f"{key} is a whole document (an "
                                                          f"object)", key=key)
            else:
                if v == 0:
                    continue
                new = store.version(kind, name, 0)
            c, _ = self._doc_change(store, key, kind, name, new)
            if c is not None:
                changes.append(c)
                docs[key] = {"doc": new, "base": v}
        warnings = [] if on else ["the conductor is off: the documents are stored and read when "
                                  "it is switched on"]
        return part_plan(changes, state=digest(state), warnings=warnings,
                         native={"docs": docs})

    def apply(self, target, plan, ctx):
        from tandemapp.conductor import store as store_mod
        store, _ = self._store()
        before, after, restore = [], [], []
        note = ctx.get("note") or f"settings change set {ctx['change_set']}"
        for key, item in sorted(plan["native"]["docs"].items()):
            kind, name = self._key(store, key)
            try:
                if item.get("restore") is not None:
                    v = store.restore(kind, name, item["restore"], author=ctx["who"],
                                      note=f"{note}: restore of v{item['restore']}"[:200])
                else:
                    v = store.put(kind, name, item["doc"], note=note[:200], author=ctx["who"],
                                  base=item["base"])
            except store_mod.ConfigError as exc:
                raise SettingsError(exc.status, "store_refused", f"{key}: {exc}",
                                    key=key) from None
            before.append(f"{key}@{item['base']}")
            after.append(f"{key}@{v}")
            restore.append({"scope": "conductor", "target": target,
                            "version": f"{key}@{item['base']}"})
        return {"version_before": ",".join(before), "version_after": ",".join(after),
                "restore": restore}

    def versions(self, target):
        store, _ = self._store()
        out = []
        for k, kind, name in self._kinds(store):
            for r in store.versions(kind, name)[:20]:
                note = r.get("note") or ""
                cs = next((w for w in note.replace(":", " ").split() if w.startswith("cs-")), None)
                out.append({"id": f"{k}@{r['version']}", "at": r.get("created_at"),
                            "who": r.get("author"), "note": note, "change_set": cs,
                            "keys": [k], "current": bool(r.get("current"))})
        return {"current": None, "versions": out}

    def plan_restore(self, target, version):
        store, on = self._store()
        key, _, n = (version or "").rpartition("@")
        try:
            kind, name = self._key(store, key) if key else (None, None)
            n = int(n)
        except (SettingsError, ValueError):
            kind, n = None, -1
        if kind is None or n < 0:
            raise SettingsError(404, "no_version", f"no version {version!r} of the conductor: "
                                                   f"<document>@<n>")
        from tandemapp.conductor import store as store_mod
        try:
            doc = store.version(kind, name, n)
        except store_mod.ConfigError:
            raise SettingsError(404, "no_version", f"no version {version!r} of the "
                                                   f"conductor") from None
        v, _ = store.get(kind, name)
        try:
            c, _ = self._doc_change(store, key, kind, name, doc)
        except SettingsError as exc:
            raise SettingsError(422, "restore_invalid", f"version {version} is not valid now: "
                                f"{exc.message}", key=key, errors=exc.extra.get("errors")) \
                from None
        changes = [c] if c else []
        return part_plan(changes, state=digest({key: v}),
                         native={"docs": {key: {"restore": n, "base": v}} if c else {}})

    def restore(self, target, plan, ctx):
        return self.apply(target, plan, ctx)


# ======================================================================================== hooks
class HooksAdapter:
    """What the hook registry gives the hooks scope (E1, contracts/hooks-v1). Every method is the
    registry's; the scope does the plan, the confirm, the versions and the audit.

        keys() -> [key_spec]          its settings: a hook's mode, priority, scope, failure
                                      policy, a plugin's options (secrets marked `secret`); the
                                      restart class `engine` for an in-process hook
        values() -> {key: value}      the values now (a secret: None); a key that holds nothing
                                      is left out (unset)
        extras() -> {key: {...}}      what a value row adds: a `label` (the hook's name), a
                                      secret's `hint`
        is_set(key) -> bool           a secret's set or unset
        check(sets, unsets) -> [str]  refusals in words, empty when the change may be written (or
                                      raise SettingsError naming the key)
        grants(sets) -> [grant]       the full payload each set grants: {key, hook, destination,
                                      lan, version}; empty when none
        empty_why() -> str | None     why there is no key yet
        keys_for(sets) -> [key_spec]  the keys a change's own sets make (a question's key in the
                                      change that adds the question); none by default
        write(sets, unsets, who, grants=None)
                                      the change into the registry's own tables; `grants` the
                                      grants outside the LAN whose second confirm was given
    """

    def keys(self) -> list:
        return []

    def values(self) -> dict:
        return {}

    def extras(self) -> dict:
        return {}

    def is_set(self, key: str) -> bool:
        return False

    def check(self, sets, unsets) -> list:
        return []

    def grants(self, sets) -> list:
        return []

    def empty_why(self) -> str | None:
        return None

    def keys_for(self, sets) -> list:
        return []

    def write(self, sets, unsets, who) -> None:
        return None


class HooksScope(KeyValueScope):
    def __init__(self, cfg, adapter: HooksAdapter | None = None):
        super().__init__(cfg)
        self.adapter = adapter

    def plug(self, adapter: HooksAdapter) -> None:
        """The hook registry plugs in here once it runs."""
        self.adapter = adapter

    def writable(self, target):
        if self.adapter is None:
            return True, "no hook is registered yet: the hooks scope has no key"
        if not self.adapter.keys():
            return True, self.adapter.empty_why()
        return True, None

    def spec(self, target):
        if self.adapter is None:
            return {}
        return {k["key"]: k for k in self.adapter.keys()}

    def spec_for(self, target, sets):
        out = self.spec(target)
        if self.adapter is not None and sets:
            for k in self.adapter.keys_for(sets):
                out.setdefault(k["key"], k)
        return out

    def values(self, target):
        if self.adapter is None:
            return {}
        spec = self.spec(target)
        vals = self.adapter.values()
        extras = self.adapter.extras()
        out = {}
        for k, s in spec.items():
            source = k.partition(".")[0] if k.startswith(("hooks.", "providers.")) else "hooks"
            if s.get("secret"):
                out[k] = (None, self.adapter.is_set(k), source, extras.get(k))
            else:
                out[k] = (vals.get(k), k in vals, source, extras.get(k))
        return out

    def check(self, target, sets, unsets, spec):
        errs = self.adapter.check(sets, unsets) if self.adapter else []
        if errs:
            raise SettingsError(422, "bad_value", errs[0], errors=errs[:20])
        return []

    def plan(self, target, sets, unsets):
        """The plan of KeyValueScope, and the grants of the full payload it makes: only for a
        caller with pairing's rights; one outside the LAN needs the second confirm. The grants'
        destinations and the hooks' versions are bound into the confirm."""
        p = super().plan(target, sets, unsets)
        if self.adapter is None:
            return p
        changed = {c["key"] for c in p["changes"]}
        grants = self.adapter.grants({k: v for k, v in sets.items() if k in changed})
        if not grants:
            return p
        if not settings_mod.trusted():
            raise SettingsError(403, "trust_required", (
                f"{grants[0]['key']}: the full payload takes pairing's rights: set an admin "
                f"password (App settings) to grant it from another machine, or grant it from "
                f"this one"), key=grants[0]["key"])
        for g in grants:
            where = g["destination"] or "this controller"
            p["warnings"].append(
                f"{g['key']}: the hook {g['hook']} gets the full payload (prompts and answers as "
                f"they are) at {where}" + ("" if g["lan"] else ", outside the LAN: apply asks "
                                                              "a second time"))
        p["grant_keys"] = sorted(g["key"] for g in grants if not g["lan"])
        p["grants"] = {g["key"]: g for g in grants}
        p["state"] = digest([p["state"], sorted(grants, key=lambda g: g["key"])])
        return p

    def apply(self, target, plan, ctx):
        # the grants outside the LAN go to the registry only when the second confirm was given
        self._confirmed = {k: g for k, g in (plan.get("grants") or {}).items()
                           if not g["lan"] and ctx.get("grant_ok")}
        try:
            return super().apply(target, plan, ctx)
        finally:
            self._confirmed = {}

    def write(self, target, sets, unsets, who):
        g = getattr(self, "_confirmed", None)
        if g:
            self.adapter.write(sets, unsets, who, grants=g)
        else:
            self.adapter.write(sets, unsets, who)
