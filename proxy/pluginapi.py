"""`/v1/fleet/plugins/*`: the plugins' admin API (SRV-857, contracts/plugin-v1; the plugin pages
are VIS-284's).

    GET    /v1/fleet/plugins                  every installed plugin, and the versions this
                                              controller reads (plugin_api, hooks_api, tandemapp)
    POST   /v1/fleet/plugins/inspect          {source}: fetch and check a source without
                                              installing it: the manifest, every check, the
                                              requested capabilities, the settings' form
    POST   /v1/fleet/plugins                  {source, upgrade?, allow_downgrade?}: install
                                              (201), upgrade or unchanged (200); refused: 422
                                              plugin_refused with every error, 409 exists,
                                              version_unchanged or downgrade
    GET    /v1/fleet/plugins/{id}             one plugin: its manifest, contributions, settings'
                                              form, capabilities requested and granted, secrets
                                              set or unset, integrity, the hooks that name it
    POST   /v1/fleet/plugins/{id}/enable, /disable
    POST   /v1/fleet/plugins/{id}/verify      check its tree against its sha256 now
    DELETE /v1/fleet/plugins/{id}             refused while a hook names it (409 referenced)

A plugin's settings, secrets and capability grants are the settings interface's plugins scope
(/v1/settings/plugins?target=<id>, proxy/pluginsettings.py), with its plan, confirm, versions and
audit.

THE RIGHTS. Reads, inspect, disable and verify take the configuration's writes (`conductor_write`).
Install, upgrade, enable and remove take pairing's (`fleet_trust`): with no admin password set only
this machine may, as pairing a node does. No answer holds a secret: a secret is set or unset with
its hint.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from proxy import hookstages
from proxy import pluginmanifest as PM
from proxy import schemaform
from proxy.pluginstore import PluginError

CONTRACT = PM.PLUGIN_API
BASE = "/v1/fleet/plugins"


def view(store, p: dict, full: bool = False) -> dict:
    m = p["manifest"]
    contributes = m.get("contributes") or {}
    out = {"id": p["id"], "name": p["name"], "version": p["version"], "kind": p["kind"],
           "title": p["title"], "description": m.get("description"), "source": p["source"],
           "sha256": p["sha256"], "signature": p["signature"], "enabled": p["enabled"],
           "trust": {"requested": p["trust"], "granted": "sandboxed"},
           "integrity": p["integrity"], "checked_at": p["checked_at"],
           "capabilities": {"requested": store.requested(p), "granted": sorted(p["granted"]),
                            "grants": p["granted"]},
           "contributes": {k: len(v) for k, v in contributes.items() if isinstance(v, list)},
           "installed_at": p["installed_at"], "installed_by": p["installed_by"],
           "updated_at": p["updated_at"], "updated_by": p["updated_by"],
           "row_version": p["row_version"]}
    if full:
        sch = store.settings_schema(p)
        out["manifest"] = m
        out["settings_form"] = schemaform.describe(sch) if sch else None
        out["settings"] = store.effective_settings(p)
        out["secrets"] = store.secret_hints(p["id"])
        out["references"] = store.references(p["id"])
        out["path"] = p["path"]
        out["manifest_dir"] = p["manifest_dir"]
    return out


def _route_rights(method: str, sub: list[str]) -> str:
    if method == "POST" and not sub:
        return "fleet_trust"                                          # install
    if method == "DELETE" and len(sub) == 1:
        return "fleet_trust"
    if method == "POST" and len(sub) == 2 and sub[1] == "enable":
        return "fleet_trust"
    return "conductor_write"


def handle(handler, svc, method: str, sub: list[str], body) -> None:
    """One /v1/fleet/plugins request; `sub` is the path after /v1/fleet/plugins."""
    store = svc.plugins
    route = _route_rights(method, sub)
    from proxy import app as app_mod
    a = app_mod.STATE.get("auth")
    if route == "fleet_trust" and a is not None and not a.password_set and \
            a.decide(route, handler._peer(), handler.headers) != "ok":
        return _send(handler, 401, {"error": {
            "type": "unauthorized", "message": "installing, enabling and removing a plugin take "
            "pairing's rights: set an admin password (App settings) to do it from another "
            "machine, or do it from this one"}})
    if not handler._allowed(route):
        return
    from proxy import config_api
    who = config_api._who(handler, svc)
    q = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query).items()}
    try:
        code, out = _route(store, method, sub, body, who, q)
    except PluginError as exc:
        code, out = exc.status, exc.body()
    return _send(handler, code, out)


def _send(handler, code: int, out) -> None:
    handler._json(code, out, extra_headers=(("Cache-Control", "no-store"),))


def _route(store, method: str, sub: list[str], body, who: str, q: dict) -> tuple[int, dict]:
    if body is not None and not isinstance(body, dict):
        raise PluginError(400, "bad_request", "the body must be a JSON object")
    body = body or {}
    if not store.ready():
        raise PluginError(503, "unavailable", "the config database is not open")
    if not sub:
        if method == "GET":
            return 200, {"contract_version": CONTRACT, "plugin_api": PM.PLUGIN_API,
                         "hooks_api": hookstages.VERSION, "tandemapp": PM.this_tandemapp(),
                         "plugins": [view(store, p) for p in store.list()]}
        if method == "POST":
            _fields(body, {"source", "upgrade", "allow_downgrade"})
            for f in ("upgrade", "allow_downgrade"):
                if f in body and not isinstance(body[f], bool):
                    raise PluginError(400, "bad_request", f"{f}: true or false", f)
            p, note = store.install(body.get("source"), who, upgrade=body.get("upgrade", False),
                                    allow_downgrade=body.get("allow_downgrade", False))
            code = 201 if note["action"] == "installed" else 200
            return code, {"contract_version": CONTRACT, "plugin": view(store, p, full=True),
                          **note}
    elif sub == ["inspect"] and method == "POST":
        _fields(body, {"source"})
        return 200, {"contract_version": CONTRACT, **store.inspect(body.get("source"))}
    elif len(sub) == 1:
        pid = sub[0]
        if method == "GET":
            return 200, {"contract_version": CONTRACT,
                         "plugin": view(store, store.need(pid), full=True)}
        if method == "DELETE":
            return 200, {"contract_version": CONTRACT, **store.remove(pid, who)}
    elif len(sub) == 2 and method == "POST":
        pid, act = sub
        _fields(body, set())
        if act in ("enable", "disable"):
            p = store.set_enabled(pid, act == "enable", who)
            return 200, {"contract_version": CONTRACT, "plugin": view(store, p, full=True)}
        if act == "verify":
            v = store.verify(pid)
            return 200, {"contract_version": CONTRACT, "id": pid, **v}
    raise PluginError(404, "not_found", f"no route {method} {BASE}/{'/'.join(sub)}".rstrip("/"))


def _fields(body: dict, allowed: set) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise PluginError(400, "bad_request", f"{extra[0]}: not a field of this request",
                          extra[0])
