"""The plugins scope of the settings interface (SRV-857, proxy/settings.py): one target a plugin
(its id), its keys made from its manifest, written into the plugin registry (proxy/pluginstore.py).

WHY A SCOPE OF ITS OWN, not keys of the hooks scope (where the decision providers went): a plugin's
settings come from a schema its author wrote, so its keys and their types differ plugin by plugin;
a target a plugin gives each its own versions, so restoring one plugin (or the hooks) never rolls
another plugin's settings or grants back; and its capability grants are a plugin's, not a hook's.
It applies after the hooks scope in a change set (dependency order app, backends, conductor, engine,
hooks, plugins), so a change set that turns off the hooks naming a plugin and the plugin itself
lets go of the hooks first. Adding it is settings-v1 1.1 (MINOR: a scope).

THE KEYS of a plugin `<id>` (target `<id>`):

    enabled                  boolean   the plugin host may start it (F2): its tree is checked
                                       against its sha256 and its compatibility first
    settings.<property>      the type of the property in the manifest's settings schema (string,
                             integer, number, boolean, enum, list, object); its default is the
                             schema's; a set value must hold against the property's schema
    secrets.<NAME>           secret, write-only: a manifest `secrets` name; sealed in the registry
    capabilities.<cap>       boolean   a requested capability granted (true) or not (false, the
                                       default: deny by default)

A GRANT (a capability set true that is not granted yet) and enabling a plugin are planned only for
a caller with pairing's rights (403 trust_required; the hooks' full payload's rule, SEC-16).
prompts:full, engine:control and network:<host> outside the LAN also take the plan's second confirm
(`grant_keys`, `confirm_grant`): without it the apply is 428 grant_confirm_required, and the grant
records who confirmed it. A restore that brings a grant back is planned the same way. Unsetting a
capability revokes it. Restart class none: the plugin host reads the registry again (F2).
"""

from __future__ import annotations

from proxy import pluginmanifest as PM
from proxy import schemaform
from proxy import settings as settings_mod
from proxy.pluginstore import PluginError
from proxy.settings import KeyValueScope, SettingsError, digest, key_spec

_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
          "array": "list", "object": "object"}


def _type_of(sch: dict, root: dict) -> tuple[str, list | None, bool]:
    """(settings type, enum values, nullable) of a property's schema."""
    try:
        node, _ = schemaform._resolve(sch, root)
    except schemaform.SchemaError:
        return "object", None, True
    form = schemaform.describe({"type": "object", "properties": {"x": node}})["fields"][0]
    if form["control"] in ("select", "choice"):
        return "enum", [o["value"] for o in form["options"]], form["nullable"]
    return _TYPES.get(form["type"], "object"), None, form["nullable"]


class PluginsScope(KeyValueScope):
    name = "plugins"

    def __init__(self, cfg, store):
        super().__init__(cfg)
        self.store = store                       # proxy/pluginstore.py PluginStore
        self._confirmed: dict = {}

    # ------------------------------------------------------------------ targets
    def _rows(self) -> list[dict]:
        return self.store.list() if self.store.ready() else []

    def targets(self) -> list[dict]:
        return [{"id": p["id"], "label": p["title"] or p["name"], "writable": True,
                 "why": None if p["integrity"] == "ok" else
                 f"its tree is {p['integrity']} since the install"} for p in self._rows()]

    def check_target(self, target):
        if target is None and not self._rows():
            raise SettingsError(404, "no_target", "no plugin is installed: install one on "
                                                  "/v1/fleet/plugins, then its settings are here")
        return super().check_target(target)

    def _plugin(self, target: str) -> dict:
        try:
            return self.store.need(target)
        except PluginError:
            raise SettingsError(404, "no_target", f"the plugins scope has no target "
                                                  f"{target!r}") from None

    # ------------------------------------------------------------------ the keys
    def spec(self, target: str) -> dict:
        p = self._plugin(target)
        out = {"enabled": key_spec("enabled", "boolean", default=False,
                                   text="the plugin host may start it")}
        sch = self.store.settings_schema(p)
        if sch:
            req = set(sch.get("required") or [])
            for k, sub in (sch.get("properties") or {}).items():
                t, values, nullable = _type_of(sub, sch)
                node = schemaform._resolve(sub, sch)[0] if isinstance(sub, dict) else {}
                out[f"settings.{k}"] = key_spec(
                    f"settings.{k}", t, values=values, nullable=nullable or k not in req,
                    default=node.get("default"),
                    text=node.get("description") or node.get("title"))
        for s in p["manifest"].get("secrets") or []:
            out[f"secrets.{s['name']}"] = key_spec(f"secrets.{s['name']}", "string", secret=True,
                                                   nullable=True, text=s.get("description"))
        for c in self.store.requested(p):
            text = "granted: the plugin may use it (deny by default)"
            if _confirm(c):
                text += "; granting it takes a second confirm"
            out[f"capabilities.{c}"] = key_spec(f"capabilities.{c}", "boolean", default=False,
                                                text=text)
        return out

    def values(self, target: str) -> dict:
        p = self._plugin(target)
        label = {"label": p["title"] or p["name"]}
        out = {"enabled": (p["enabled"], p["enabled"], "plugin", label)}
        sch = self.store.settings_schema(p)
        if sch:
            eff = self.store.effective_settings(p)
            for k in (sch.get("properties") or {}):
                out[f"settings.{k}"] = (eff.get(k), k in p["settings"], "plugin", label)
        hints = self.store.secret_hints(target)
        for n, h in hints.items():
            out[f"secrets.{n}"] = (None, h["set"], "plugin", dict(label, hint=h["sha256_6"]))
        for c in self.store.requested(p):
            on = c in p["granted"]
            out[f"capabilities.{c}"] = (on, on, "plugin", label)
        return out

    # ------------------------------------------------------------------ checks
    def check(self, target, sets, unsets, spec):
        p = self._plugin(target)
        sch = self.store.settings_schema(p) or {}
        props = sch.get("properties") or {}
        for k, v in sets.items():
            if k == "enabled":
                if not isinstance(v, bool):
                    raise SettingsError(422, "bad_value", "enabled: true or false", key=k)
                if v:
                    try:
                        self.store.enable_check(target, record=False)
                    except PluginError as exc:
                        raise SettingsError(422, "bad_value", f"enabled: {exc.message}",
                                            key=k) from None
            elif k.startswith("settings."):
                prop = k.split(".", 1)[1]
                if v is None:
                    continue
                errs = schemaform.validate(v, props[prop], root=sch, path=k, limit=5)
                if errs:
                    raise SettingsError(422, "bad_value", errs[0], key=k, errors=errs)
            elif k.startswith("secrets."):
                if v is not None and (not isinstance(v, str) or not 1 <= len(v) <= 8192):
                    raise SettingsError(422, "bad_value", f"{k}: a string of 1 to 8192 "
                                                          f"characters", key=k)
            elif k.startswith("capabilities."):
                if not isinstance(v, bool):
                    raise SettingsError(422, "bad_value", f"{k}: true or false", key=k)
        for k in unsets:
            if k.startswith("settings."):
                prop = k.split(".", 1)[1]
                if prop in (sch.get("required") or []) and "default" not in \
                        schemaform._resolve(props[prop], sch)[0]:
                    raise SettingsError(422, "bad_value", f"{k} is required and has no default: "
                                                          f"set a value", key=k)
        return []

    def _grants(self, target: str, sets: dict) -> list[dict]:
        p = self._plugin(target)
        out = []
        for k, v in sets.items():
            if k.startswith("capabilities.") and v is True:
                cap = k.split(".", 1)[1]
                if cap not in p["granted"]:
                    out.append({"key": k, "capability": cap, "confirm": _confirm(cap),
                                "row_version": p["row_version"]})
        return out

    def plan(self, target, sets, unsets):
        p = super().plan(target, sets, unsets)
        changed = {c["key"] for c in p["changes"]}
        grants = self._grants(target, {k: v for k, v in sets.items() if k in changed})
        enabling = "enabled" in changed and sets.get("enabled") is True
        if not grants and not enabling:
            return p
        if not settings_mod.trusted():
            key = grants[0]["key"] if grants else "enabled"
            raise SettingsError(403, "trust_required", (
                f"{key}: granting a plugin a capability or enabling it takes pairing's rights: "
                f"set an admin password (App settings) to do it from another machine, or do it "
                f"from this one"), key=key)
        if enabling:
            p["warnings"].append(f"enabled: the plugin host may start {target} (its own code, "
                                 f"with the capabilities granted to it)")
        for g in grants:
            p["warnings"].append(f"{g['key']}: {target} gets {g['capability']}" + (
                ": apply asks a second time" if g["confirm"] else ""))
        p["grant_keys"] = sorted(g["key"] for g in grants if g["confirm"])
        p["grants"] = {g["key"]: g for g in grants}
        p["state"] = digest([p["state"], sorted(grants, key=lambda g: g["key"])])
        return p

    def apply(self, target, plan, ctx):
        self._confirmed = {g["capability"] for g in (plan.get("grants") or {}).values()
                           if g["confirm"] and ctx.get("grant_ok")}
        try:
            return super().apply(target, plan, ctx)
        finally:
            self._confirmed = set()

    def write(self, target, sets, unsets, who):
        kw = {"settings_set": {}, "settings_unset": [], "secrets_set": {}, "secrets_unset": [],
              "grant": [], "revoke": []}
        enabled = None
        for k, v in sets.items():
            if k == "enabled":
                enabled = bool(v)
            elif k.startswith("settings."):
                if v is None:
                    kw["settings_unset"].append(k.split(".", 1)[1])
                else:
                    kw["settings_set"][k.split(".", 1)[1]] = v
            elif k.startswith("secrets."):
                if v is None:
                    kw["secrets_unset"].append(k.split(".", 1)[1])
                else:
                    kw["secrets_set"][k.split(".", 1)[1]] = v
            elif k.startswith("capabilities."):
                (kw["grant"] if v else kw["revoke"]).append(k.split(".", 1)[1])
        for k in unsets:
            if k == "enabled":
                enabled = False
            elif k.startswith("settings."):
                kw["settings_unset"].append(k.split(".", 1)[1])
            elif k.startswith("secrets."):
                kw["secrets_unset"].append(k.split(".", 1)[1])
            elif k.startswith("capabilities."):
                kw["revoke"].append(k.split(".", 1)[1])
        try:
            self.store.write(target, enabled=enabled, confirmed=self._confirmed, who=who, **kw)
        except PluginError as exc:
            status = exc.status if exc.status in (404, 409) else 422
            raise SettingsError(status, "store_refused", exc.message,
                                key=exc.field) from None


def _confirm(cap: str) -> bool:
    try:
        return PM.needs_confirm(cap)
    except Exception:                                              # noqa: BLE001
        return True
