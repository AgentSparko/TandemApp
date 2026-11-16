"""Config as code, the export's version 2 (SRV-847, design section 7): what a TandemApp is beyond
the fleet's registry, as sections of the configuration file, and how an import plans and applies
them. proxy/appsettings.py keeps version 1's sections (nodes, deployments, rules, layouts, `app.*`
settings) and calls this module for the rest.

THE SECTIONS (none holds a secret: a secret's name only, as `secrets` or `keys`):

    hooks               every hook of the registry (proxy/hookstore.py): its fields, switches and
                        payload level; an http hook's secret by name (`secrets: ["secret"]`)
    decision_providers  every provider (proxy/providerstore.py): its fields, switches and question
                        mapping; its keys by scope (`keys: ["*", "classify"]`)
    plugins             every installed plugin (proxy/pluginstore.py): name, version, source,
                        sha256, enabled, its set settings, its grants (capabilities, trust) and its
                        secrets by name; never a file of the plugin
    engines             per paired engine host (the settings interface's engine scope): the keys
                        its managed overlay sets, a secret key by name only
    conductor           this controller's conductor documents that are stored (not built-in)

THE IMPORT plans each item beside version 1's (`add`, `change`, `keep`, `conflict` with why):

  - a hook or a provider that is not here is created through its store, as its API would (the
    full payload's and a provider's text's grant included: outside the LAN the import's
    `confirm_grant`); one that is here with other fields (what it calls, its kind, stages,
    role) is a conflict, changed on its page; its switches go into the change set;
  - a plugin that is not installed here is a conflict naming what to install (a hook of kind
    plugin naming it too); one that is installed gets the file's settings, grants and switch
    through the change set, the keys its installed version has (the others listed);
  - an engine host that is not paired here is a conflict; one that is gets the file's overlay
    keys it knows through the change set;
  - a conductor document goes into the change set; one this conductor does not know is a
    conflict.

ONE CHANGE SET of the settings interface (proxy/settings.py) carries every switch, setting, overlay
key and document, in its dependency order, stopping at the first failure; the creates follow it
(hooks, then providers), stopping at the first that fails. The plan holds the change set's plan;
the import's confirm binds it, so a setting changed since the plan is 409. A change set that
restarts an engine and changes its disk cache's fingerprint needs the import's
`confirm_fingerprint`; a grant outside the LAN its `confirm_grant`.

A SECRET IN THE FILE by mistake (a value where only a name goes) is stripped and listed in the
plan's `stripped`; nothing of it is answered or written. `mode: replace` removes nothing of these
sections: a hook, provider or plugin the file lacks is listed as kept.
"""

from __future__ import annotations

import contextlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

VERSION = 2
SECTIONS = ("hooks", "decision_providers", "plugins", "engines", "conductor")
LIMITS = {"hooks": 256, "decision_providers": 64, "plugins": 128, "engines": 64, "conductor": 64}
HOOK_SWITCHES = ("enabled", "mode", "priority", "scope", "policy", "timeout_ms", "payload")
HOOK_SHAPE = ("name", "kind", "stages", "role", "endpoint", "target", "options")
PROVIDER_SWITCHES = ("enabled", "mode", "ab_percent", "sticky", "questions")
PROVIDER_SHAPE = ("name", "kind", "endpoint", "hook_id", "target", "model", "timeout_ms")
SECRETISH = re.compile(r"(secret|token|password|passwd|api_?key|private|credential)", re.I)
EXCLUDED = [
    "every hook's signing secret (its name only)",
    "every decision provider's API keys (their scopes only)",
    "every plugin's secrets (their names only) and its files",
    "every engine host's secret keys (their names only)",
]


class Unread(Exception):
    """A section that could not be read (an engine host that does not answer)."""


def _svc(fleet):
    return getattr(fleet, "svc", None)


def _store(fleet, name):
    svc = _svc(fleet)
    obj = getattr(svc, name, None) if svc is not None else None
    if name in ("hooks", "providers") and obj is not None:
        obj = getattr(obj, "store", None)
    if obj is None:
        return None
    try:
        return obj if obj.ready() else None
    except Exception:                                                # noqa: BLE001
        return None


@contextlib.contextmanager
def trusted():
    """The import is planned and applied with pairing's rights (its apply route takes them)."""
    from proxy import settings as settings_mod
    prev = getattr(settings_mod._CALLER, "trust", False)
    settings_mod._CALLER.trust = True
    try:
        yield
    finally:
        settings_mod._CALLER.trust = prev


def _clean_url(u):
    """A git URL without a user or password in it."""
    if not isinstance(u, str):
        return u
    try:
        p = urlsplit(u)
    except ValueError:
        return u
    if p.username is None and p.password is None:
        return u
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, host, p.path, p.query, p.fragment))


# ---------------------------------------------------------------------------------- the export

def export(fleet, settings) -> tuple[dict, list[str]]:
    """(the version 2 sections, notes on what could not be read)."""
    notes: list[str] = []
    out: dict = {k: [] for k in ("hooks", "decision_providers", "plugins")}
    out.update(engines={}, conductor={})
    hs = _store(fleet, "hooks")
    for h in hs.list() if hs is not None else []:
        row = {"id": h["id"], **{k: h[k] for k in HOOK_SHAPE + HOOK_SWITCHES}}
        if h["kind"] == "http":
            row["secrets"] = ["secret"] if hs.secret_hint(h["id"], h["secret_ref"])["set"] else []
        out["hooks"].append(row)
    ps = _store(fleet, "providers")
    for p in ps.list() if ps is not None else []:
        row = {"id": p["id"], **{k: p.get(k) for k in PROVIDER_SHAPE + PROVIDER_SWITCHES}}
        row["keys"] = sorted(ps.key_hints(p["id"]))
        out["decision_providers"].append(row)
    pl = _store(fleet, "plugins")
    for p in pl.list() if pl is not None else []:
        src = dict(p["source"] or {})
        for k in ("url", "repo"):
            if k in src:
                src[k] = _clean_url(src[k])
        out["plugins"].append({
            "id": p["id"], "name": p["name"], "version": p["version"], "source": src,
            "sha256": p["sha256"], "enabled": p["enabled"], "settings": dict(p["settings"]),
            "grants": {"capabilities": sorted(p["granted"]),
                       "trust": p.get("trust_granted") or "sandboxed"},
            "secrets": sorted(n for n, h in pl.secret_hints(p["id"]).items() if h["set"])})
    if settings is not None:
        for t in _targets(settings, "engine"):
            if not t.get("writable") or t.get("transport") == "http":
                continue
            try:
                vals = settings.get("engine", t["id"])["values"]
            except Exception as exc:                                 # noqa: BLE001
                notes.append(f"the engine host {t['id']}: its overlay could not be read "
                             f"({getattr(exc, 'message', type(exc).__name__)})")
                continue
            sec = _secret_keys(settings, "engine", t["id"])
            out["engines"][t["id"]] = {
                "label": t.get("label") or t["id"],
                "values": {r["key"]: r["value"] for r in vals
                           if r["set"] and r["source"] == "managed" and r["key"] not in sec
                           and r["value"] is not None},
                "secrets": sorted(r["key"] for r in vals if r["key"] in sec and r["set"])}
        try:
            for r in settings.get("conductor", None)["values"]:
                if r["set"]:
                    out["conductor"][r["key"]] = r["value"]
        except Exception as exc:                                     # noqa: BLE001
            msg = getattr(exc, "message", type(exc).__name__)
            if "no conductor" not in str(msg):
                notes.append(f"the conductor's documents could not be read ({msg})")
    return out, notes


def _targets(settings, scope) -> list[dict]:
    try:
        return settings.scope(scope).targets()
    except Exception:                                                # noqa: BLE001
        return []


def _schema(settings, scope, target) -> dict:
    return {k["key"]: k for k in settings.schema(scope, target)["keys"]}


def _secret_keys(settings, scope, target) -> set:
    try:
        return {k for k, s in _schema(settings, scope, target).items() if s.get("secret")}
    except Exception:                                                # noqa: BLE001
        return set()


# ---------------------------------------------------------------------------------- the file

def check(doc: dict, bad) -> None:
    """The version 2 sections held to their shape (`bad(message)` raises)."""
    for k in ("hooks", "decision_providers", "plugins"):
        v = doc.get(k, [])
        if not isinstance(v, list) or len(v) > LIMITS[k]:
            bad(f"{k} is a list of at most {LIMITS[k]}")
        for i, x in enumerate(v):
            if not isinstance(x, dict) or not isinstance(x.get("id"), str) or \
                    not 1 <= len(x["id"]) <= 80:
                bad(f"{k}[{i}]: an object with its id")
    for k in ("engines", "conductor"):
        v = doc.get(k, {})
        if not isinstance(v, dict) or len(v) > LIMITS[k]:
            bad(f"{k} is an object of at most {LIMITS[k]}")
    for t, e in (doc.get("engines") or {}).items():
        if not isinstance(e, dict) or not isinstance(e.get("values", {}), dict):
            bad(f"engines.{t}: {{label, values}}")
    for key, d in (doc.get("conductor") or {}).items():
        if not isinstance(d, dict):
            bad(f"conductor.{key}: a document (an object)")


# ---------------------------------------------------------------------------------- the plan

class Plan:
    """A v2 import's plan: items (appended to version 1's), the change set's parts, the creates,
    the grants outside the LAN, what was stripped."""

    def __init__(self):
        self.items: list[dict] = []
        self.parts: dict = {}            # (scope, target) -> {"set": {}, "unset": []}
        self.creates: list[tuple] = []   # ("hook" | "provider", id, doc, confirm_of)
        self.grants: list[str] = []      # what a create grants outside the LAN, in words
        self.stripped: list[str] = []
        self.view: dict | None = None    # the change set's plan (proxy/settings.py)
        self.refused: str | None = None

    def item(self, kind, iid, label, op, detail=None, fields=None):
        self.items.append({"kind": kind, "id": str(iid), "label": str(label), "op": op,
                           "detail": detail, "fields": list(fields or []), "_apply": None})

    def put(self, scope, target, key, value=None, unset=False):
        p = self.parts.setdefault((scope, target), {"set": {}, "unset": []})
        if unset:
            p["unset"].append(key)
        else:
            p["set"][key] = value

    def changes(self) -> list[dict]:
        out = []
        for (scope, target), p in self.parts.items():
            c = {"scope": scope, "target": target}
            if p["set"]:
                c["set"] = p["set"]
            if p["unset"]:
                c["unset"] = sorted(p["unset"])
            if p["set"] or p["unset"]:
                out.append(c)
        return out


def _strip(row: dict, allowed, where: str, plan: Plan) -> dict:
    out = {}
    for k, v in row.items():
        if k in allowed:
            out[k] = v
        elif SECRETISH.search(k) and k not in ("secrets", "keys"):
            plan.stripped.append(f"{where}.{k}")
    return out


def plan(fleet, settings, doc: dict, mode: str) -> Plan:
    pl = Plan()
    _plan_plugins(fleet, settings, doc, pl)
    _plan_hooks(fleet, doc, mode, pl)
    _plan_providers(fleet, doc, mode, pl)
    if settings is not None:
        _plan_engines(settings, doc, pl)
        _plan_conductor(settings, doc, pl)
    elif doc.get("engines") or doc.get("conductor"):
        pl.item("setting", "settings", "the settings interface", "conflict",
                "this controller has no settings interface: the engines' and the conductor's "
                "sections are skipped")
    ch = pl.changes()
    if ch:
        from proxy.settings import SettingsError
        try:
            with trusted():
                pl.view = settings.plan({"changes": ch})
        except SettingsError as exc:
            where = "/".join(x for x in (exc.extra.get("scope"), exc.extra.get("target"),
                                        exc.extra.get("key")) if x)
            pl.refused = f"the settings change set is refused at {where or 'a part'}: " \
                         f"{exc.message}"
    return pl


def _plan_hooks(fleet, doc, mode, pl: Plan) -> None:
    from proxy import hookstore
    from proxy.hookstore import HookError
    rows = doc.get("hooks") or []
    st = _store(fleet, "hooks")
    if st is None:
        for h in rows:
            pl.item("hook", h["id"], h.get("name") or h["id"], "conflict",
                    "this controller has no hook registry")
        return
    here = {h["id"]: h for h in st.list()}
    plugins = {p["id"] for p in (_store(fleet, "plugins").list()
                                 if _store(fleet, "plugins") is not None else [])}
    seen = set()
    for i, raw in enumerate(rows):
        h = _strip(raw, ("id", "secrets") + HOOK_SHAPE + HOOK_SWITCHES, f"hooks[{i}]", pl)
        hid, label = h["id"], h.get("name") or h["id"]
        if hid in seen:
            pl.item("hook", hid, label, "conflict", "listed twice in the file")
            continue
        seen.add(hid)
        if h.get("kind") == "plugin" and str(h.get("target") or "").partition("/")[0] \
                not in plugins:
            pl.item("hook", hid, label, "conflict", f"its plugin "
                    f"{str(h.get('target')).partition('/')[0]} is not installed here: install "
                    f"it, then import again")
            continue
        old = here.get(hid)
        if old is not None:
            diff = [k for k in HOOK_SHAPE if k in h and h[k] != old[k]]
            if diff:
                pl.item("hook", hid, old["name"], "conflict",
                        f"here it differs in {', '.join(diff)}: change that on its page")
                continue
            ch = [k for k in HOOK_SWITCHES if k in h and h[k] != old[k] and
                  not (k == "scope" and not h[k] and not old[k])]
            for k in ch:
                key = f"hooks.{hid}.{k}"
                if h[k] is None or (k == "scope" and not h[k]):
                    pl.put("hooks", "controller", key, unset=True)
                else:
                    pl.put("hooks", "controller", key, h[k])
            pl.item("hook", hid, old["name"], "change" if ch else "keep", None, ch)
            continue
        d = {k: h[k] for k in HOOK_SHAPE + HOOK_SWITCHES if k in h}
        full = {"name": None, "kind": "http", "stages": None, "role": None, "priority": 100,
                "mode": "on", "scope": {}, "policy": None, "timeout_ms": None,
                "payload": "meta", "endpoint": None, "target": None, "options": {},
                "enabled": True}
        full.update(d)
        try:
            st._check(full)
        except HookError as exc:
            pl.item("hook", hid, label, "conflict", f"not a hook here: {exc.message}")
            continue
        if not hookstore.ID_RE.match(hid) or hid in hookstore.RESERVED:
            pl.item("hook", hid, label, "conflict", "its id is not a hook id")
            continue
        note = []
        if full["kind"] == "http":
            note.append("a new signing secret is made: rotate it on its page to read it")
        if full["payload"] == "full":
            dest = st.destination(dict(full, id=hid))
            if not dest["lan"]:
                pl.grants.append(f"hooks/{hid}:payload")
                note.append(f"the full payload goes to {dest['host']}, outside the LAN")
        pl.creates.append(("hook", hid, dict(d, id=hid), None))
        pl.item("hook", hid, label, "add", "; ".join(note) or None, sorted(d))
    if mode == "replace":
        for hid, old in here.items():
            if hid not in seen:
                pl.item("hook", hid, old["name"], "keep", "not in the file: an import removes "
                        "no hook (remove it on its page)")


def _plan_providers(fleet, doc, mode, pl: Plan) -> None:
    from proxy import providerstore
    from proxy.providerstore import ProviderError
    rows = doc.get("decision_providers") or []
    st = _store(fleet, "providers")
    if st is None:
        for p in rows:
            pl.item("provider", p["id"], p.get("name") or p["id"], "conflict",
                    "this controller has no decision providers")
        return
    here = {p["id"]: p for p in st.list()}
    new_hooks = {c[1] for c in pl.creates if c[0] == "hook"}
    seen = set()
    for i, raw in enumerate(rows):
        p = _strip(raw, ("id", "keys") + PROVIDER_SHAPE + PROVIDER_SWITCHES,
                   f"decision_providers[{i}]", pl)
        qs = p.get("questions")
        if isinstance(qs, dict):
            clean = {}
            for q, spec in qs.items():
                if isinstance(spec, dict) and "api_key" in spec:
                    pl.stripped.append(f"decision_providers[{i}].questions.{q}.api_key")
                    spec = {k: v for k, v in spec.items() if k != "api_key"}
                clean[q] = spec
            p["questions"] = clean
        pid, label = p["id"], p.get("name") or p["id"]
        if pid in seen:
            pl.item("provider", pid, label, "conflict", "listed twice in the file")
            continue
        seen.add(pid)
        keys = [k for k in p.get("keys") or [] if isinstance(k, str)]
        old = here.get(pid)
        if old is not None:
            diff = [k for k in PROVIDER_SHAPE if k in p and p[k] != old.get(k)]
            if diff:
                pl.item("provider", pid, old["name"], "conflict",
                        f"here it differs in {', '.join(diff)}: change that on its page")
                continue
            ch = [k for k in PROVIDER_SWITCHES if k in p and p[k] != old[k]]
            for k in ch:
                pl.put("hooks", "controller", f"providers.{pid}.{k}", p[k])
            pl.item("provider", pid, old["name"], "change" if ch else "keep", None, ch)
            continue
        d = {k: p[k] for k in PROVIDER_SHAPE + PROVIDER_SWITCHES if k in p}
        full = {"name": None, "kind": "systemone-http", "endpoint": None, "hook_id": None,
                "target": None, "model": "auto", "timeout_ms": 400, "questions": None,
                "mode": "shadow", "ab_percent": 0, "sticky": "session", "enabled": True,
                "api_key": None}
        full.update(d)
        try:
            if full["kind"] == "hook" and full["hook_id"] in new_hooks:
                # its hook arrives with this import: checked as the hooks were
                st2 = dict(full, kind="builtin", hook_id=None)
                st._check(st2)
            else:
                st._check(full)
            if not providerstore.ID_RE.match(pid):
                raise ProviderError(422, "invalid", "its id is not a provider id")
            st._taken(dict(full, questions=full["questions"]), pid)
        except ProviderError as exc:
            pl.item("provider", pid, label, "conflict", f"not a provider here: {exc.message}")
            continue
        note = []
        away = st.away(None, dict(full, id=pid))
        if away:
            pl.grants.append(f"providers/{pid}:endpoint")
            note.append(f"the questions' text goes to {', '.join(away)}, outside the LAN")
        if keys:
            note.append(f"its keys ({', '.join(keys)}) are not in the file: set them on its "
                        f"page")
        pl.creates.append(("provider", pid, dict(d, id=pid), away))
        pl.item("provider", pid, label, "add", "; ".join(note) or None, sorted(d))
    if mode == "replace":
        for pid, old in here.items():
            if pid not in seen:
                pl.item("provider", pid, old["name"], "keep", "not in the file: an import "
                        "removes no provider (remove it on its page)")


def _plan_plugins(fleet, settings, doc, pl: Plan) -> None:
    rows = doc.get("plugins") or []
    st = _store(fleet, "plugins")
    here = {p["id"]: p for p in st.list()} if st is not None else {}
    for i, raw in enumerate(rows):
        pid = raw["id"]
        label = raw.get("name") or pid
        sec = raw.get("secrets")
        if isinstance(sec, dict) or (isinstance(sec, list) and any(
                not isinstance(x, str) for x in sec)):
            pl.stripped.append(f"plugins[{i}].secrets")
        old = here.get(pid)
        src = raw.get("source") or {}
        if old is None:
            where = src.get("url") or src.get("path") or "its source"
            pl.item("plugin", pid, label, "conflict",
                    f"not installed here: install {raw.get('name') or pid} "
                    f"{raw.get('version') or ''} from {where} "
                    f"(sha256 {str(raw.get('sha256') or '')[:12]}), then import again")
            continue
        notes = []
        if raw.get("version") != old["version"] or raw.get("sha256") != old["sha256"]:
            notes.append(f"here it is version {old['version']} (the file has "
                         f"{raw.get('version')}): its settings go where this version has them")
        if settings is None:
            pl.item("plugin", pid, old["name"], "conflict", "no settings interface here")
            continue
        try:
            spec = _schema(settings, "plugins", pid)
            cur = {r["key"]: r for r in settings.get("plugins", pid)["values"]}
        except Exception as exc:                                     # noqa: BLE001
            pl.item("plugin", pid, old["name"], "conflict",
                    f"its settings could not be read: {getattr(exc, 'message', exc)}")
            continue
        want: dict = {}
        if isinstance(raw.get("enabled"), bool):
            want["enabled"] = raw["enabled"]
        for k, v in (raw.get("settings") or {}).items():
            if SECRETISH.search(k) and isinstance(v, str) and spec.get(f"settings.{k}") is None:
                pl.stripped.append(f"plugins[{i}].settings.{k}")
                continue
            want[f"settings.{k}"] = v
        g = raw.get("grants") or {}
        caps = set(g.get("capabilities") or [])
        for k in spec:
            if k.startswith("capabilities."):
                want[k] = k.split(".", 1)[1] in caps
        if g.get("trust") and "trust" in spec:
            want["trust"] = g["trust"]
        missing = sorted(k for k in want if k not in spec)
        if missing:
            notes.append(f"not a key of the installed version: {', '.join(missing[:6])}")
        ch = []
        for k, v in want.items():
            if k not in spec or spec[k].get("secret") or not spec[k].get("writable", True):
                continue
            r = cur.get(k) or {}
            now = r.get("value") if r.get("set") else spec[k].get("default")
            if v == now:
                continue
            if v is False and k.startswith("capabilities.") and not r.get("set"):
                continue
            pl.put("plugins", pid, k, v)
            ch.append(k)
        pl.item("plugin", pid, old["name"], "change" if ch else "keep",
                "; ".join(notes) or None, ch)


def _plan_engines(settings, doc, pl: Plan) -> None:
    targets = {t["id"]: t for t in _targets(settings, "engine")}
    for tid, e in (doc.get("engines") or {}).items():
        label = (e or {}).get("label") or tid
        t = targets.get(tid)
        if t is None or not t.get("writable"):
            pl.item("engine", tid, label, "conflict", "no engine host paired over SSH here by "
                    "that id: pair it, then import again")
            continue
        try:
            spec = _schema(settings, "engine", tid)
            cur = {r["key"]: r for r in settings.get("engine", tid)["values"]}
        except Exception as exc:                                     # noqa: BLE001
            pl.item("engine", tid, label, "conflict",
                    f"its overlay could not be read: {getattr(exc, 'message', exc)}")
            continue
        ch, skip = [], []
        for k, v in (e.get("values") or {}).items():
            s = spec.get(k)
            if s is not None and s.get("secret") or (s is None and SECRETISH.search(k)):
                pl.stripped.append(f"engines.{tid}.values.{k}")
                continue
            if s is None or not s.get("writable", True):
                skip.append(k)
                continue
            r = cur.get(k) or {}
            if r.get("set") and r.get("value") == v:
                continue
            pl.put("engine", tid, k, v)
            ch.append(k)
        pl.item("engine", tid, t.get("label") or label, "change" if ch else "keep",
                f"not set here (unknown or not managed): {', '.join(skip[:6])}" if skip else None,
                ch)


def _plan_conductor(settings, doc, pl: Plan) -> None:
    docs = doc.get("conductor") or {}
    if not docs:
        return
    try:
        target = settings.scope("conductor").check_target(None)
        spec = _schema(settings, "conductor", target)
        cur = {r["key"]: r for r in settings.get("conductor", target)["values"]}
    except Exception as exc:                                         # noqa: BLE001
        for k in docs:
            pl.item("conductor", k, k, "conflict",
                    f"the conductor's documents: {getattr(exc, 'message', exc)}")
        return
    for k, d in docs.items():
        s = spec.get(k)
        if s is None:
            pl.item("conductor", k, k, "conflict", "not a document of this conductor")
        elif not s.get("writable", True):
            pl.item("conductor", k, k, "conflict", s.get("why") or "read-only here")
        elif (cur.get(k) or {}).get("value") == d:
            pl.item("conductor", k, k, "keep")
        else:
            pl.put("conductor", target, k, d)
            pl.item("conductor", k, k, "change" if (cur.get(k) or {}).get("set") else "add",
                    "a new version of the document")


def plan_view(pl: Plan) -> dict | None:
    """What the import plan shows of its change set."""
    v = pl.view
    if v is None:
        return None
    return {"change_set": v["change_set"], "changed": v["changed"],
            "parts": [{"scope": p["scope"], "target": p["target"],
                       "keys": [c["key"] for c in p["changes"]],
                       "restart": p["restart"], "warnings": p["warnings"]}
                      for p in v["parts"]],
            "restart": v["restart"], "fingerprint_keys": v["fingerprint_keys"],
            "grant_keys": v["grant_keys"]}


# ---------------------------------------------------------------------------------- apply

def apply(fleet, settings, pl: Plan, who: str, *, fingerprint_ok: bool,
          grant_ok: bool) -> dict:
    """The change set, then the creates; the first failure stops what follows."""
    out = {"settings": None, "created": [], "failed": None, "not_run": []}
    if pl.view is not None and pl.view["changed"]:
        v = pl.view
        body = {"changes": pl.changes(), "confirm": v["confirm"],
                "note": "configuration import"}
        if v.get("confirm_fingerprint") and fingerprint_ok:
            body["confirm_fingerprint"] = v["confirm_fingerprint"]
        if v.get("confirm_grant") and grant_ok:
            body["confirm_grant"] = v["confirm_grant"]
        from proxy.settings import SettingsError
        try:
            with trusted():
                res = settings.apply(body, who)
        except SettingsError as exc:
            res = {"state": "failed", "error": exc.brief()}
        out["settings"] = {k: res.get(k) for k in ("change_set", "state", "applied", "failed",
                                                    "not_run", "error") if k in res}
        if res.get("state") != "applied":
            out["failed"] = "settings"
            out["not_run"] = [f"{c[0]}/{c[1]}" for c in pl.creates]
            return out
    hs, ps = _store(fleet, "hooks"), _store(fleet, "providers")
    for i, (kind, iid, d, away) in enumerate(pl.creates):
        try:
            if kind == "hook":
                conf = None
                if d.get("payload") == "full" and grant_ok:
                    dest = hs.destination(dict(d, kind=d.get("kind", "http")))
                    conf = hs.confirm_token(iid, dest["host"], 0)
                hs.create(d, who, confirm=conf)
            else:
                conf = ps.confirm_token(iid, away, 0) if away and grant_ok else None
                ps.create(d, who, confirm=conf)
            out["created"].append(f"{kind}/{iid}")
        except Exception as exc:                                     # noqa: BLE001
            out["failed"] = f"{kind}/{iid}: {getattr(exc, 'message', type(exc).__name__)}"
            out["not_run"] = [f"{c[0]}/{c[1]}" for c in pl.creates[i + 1:]]
            break
    return out


def digest_state(fleet) -> dict:
    """What a v2 import's confirm binds besides version 1's registry."""
    out = {}
    for name, key in (("hooks", "hooks"), ("providers", "providers"), ("plugins", "plugins")):
        st = _store(fleet, name)
        if st is None:
            continue
        rows = st.list()
        if name == "plugins":
            rows = [{k: r[k] for k in ("id", "version", "sha256", "enabled", "settings",
                                       "granted", "trust_granted")} for r in rows]
        out[key] = json.loads(json.dumps(rows, default=str))
    return out
