"""SRV-847 (D3, QA SRV-848): the configuration as code, version 2: the export carries the hooks, the
decision providers, the plugins (name, version, source, sha256, settings, grants), each paired
engine host's overlay and the conductor's documents, never a secret; the import plans first and
applies through one change set of the settings interface. Every answer is held to
contracts/fleet-v1.

  Gherkin (SRV-847) and the acceptance criteria:
  1  Round trip: Given a TandemApp with hooks, providers, a plugin with settings, a secret and a
     grant, an engine overlay and a conductor document, When it is exported and imported into a
     clean TandemApp (the plugin installed there, the engine host paired), Then the clean one
     exports the same sections (its own new secrets, no provider key: those are named only)
                                                                         test_round_trip
  2  No secrets: Given a hook's secret, provider keys, a plugin's secret and a backend's key, When
     the export is written, Then the file holds their names only; a file that holds secrets by
     mistake has them stripped and listed, and no plan or result answers them
                                                                         test_no_secrets
  3  Import plan: Given a v2 file, When it is imported, Then a plan is shown and nothing changes
     until the confirm (428 without it, 409 with another); a plugin that is not installed here is
     a conflict naming what to install, and the rest still applies
                                                                         test_plan_then_confirm
  4  A version 1 file still imports; a file of a later version is refused with the version it
     needs; a version 1 file with version 2's sections is refused
                                                                         test_versions

CPU only. Run: python tests/test_config_v2.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_tandem_remote as ftr  # noqa: E402
import plugin_util as pu  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

from proxy import app as proxy_app  # noqa: E402
from proxy import enginectl  # noqa: E402
from tools import contract_check  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "contracts", "fleet-v1")
PKEY = "sk-" + "Pv2" * 12
QKEY = "sk-" + "Qv2" * 12
PLUGIN_SECRET = "sk-" + "Gv2" * 12
BACKEND_KEY = "sk-" + "Bv2" * 12
PLANTED = "hks_" + "planted0" * 5


def ok(doc, name):
    errs = contract_check.validate(doc, contract_check.load(f"{name}.schema.json", FLEET),
                                   root=FLEET)
    assert not errs, (name, errs[:5], json.dumps(doc, default=str)[:500])
    return doc


def req(px, method, path, body=None):
    st, _, out = px.request(method, path, body)
    return st, (json.loads(out) if out else None)


def until(fn, s: float = 20.0):
    end = time.time() + s
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    return None


def change_set(px, changes):
    st, p = req(px, "POST", "/v1/settings/plan", {"changes": changes})
    assert st == 200, (st, p)
    body = {"changes": changes, "confirm": p["confirm"]}
    for k in ("confirm_fingerprint", "confirm_grant"):
        if p.get(k):
            body[k] = p[k]
    st, res = req(px, "POST", "/v1/settings/apply", body)
    assert st == 200 and res["state"] == "applied", (st, res)
    return res


def engine(px) -> str:
    """A paired engine host `h1` on the fake `tandem remote`: its restarts run to the end."""
    d = ftr.install(tempfile.mkdtemp(prefix="cfg2-eng-"))
    ftr.set_state(d, sim=True, sim_hold=False, sim_steps=["waiting", "acting", "done"])
    row = {"id": "h1", "alias": "box"}
    proxy_app.STATE["settings"] = proxy_app.settings_of(
        px.svc.cfg, px.svc, proxy_app.STATE["parts"], px.dir,
        ctl_for=lambda r: enginectl.LocalControl(d, label="h1"), rows=lambda: [row],
        backends_of=lambda h: [], warm=lambda b, m: (True, None), poll_s=0.05)
    return d


def wait_restarts(px) -> None:
    def done():
        st, lst = req(px, "GET", "/v1/settings/restarts")
        return st == 200 and all(r["state"] in ("done", "failed", "timeout", "cancelled")
                                 for r in lst["restarts"])
    assert until(done), req(px, "GET", "/v1/settings/restarts")


def plugin(px):
    p, _ = px.svc.plugins.install({"type": "folder", "path": pu.folder("classify-provider")},
                                  who="admin")
    return p


def seed(px) -> dict:
    """Controller A: hooks, providers, a plugin, an engine overlay key, a conductor document."""
    out = {}
    st, got = req(px, "POST", "/v1/fleet/hooks", {
        "name": "Audit", "endpoint": "http://127.0.0.1:9/audit", "role": "observer",
        "stages": ["request.finished"], "priority": 50, "mode": "shadow"})
    assert st == 201, got
    out["hook_secret"] = got["secret"]
    st, got = req(px, "POST", "/v1/fleet/hooks", {
        "name": "No big model", "kind": "builtin", "target": "deny", "role": "guard",
        "stages": ["request.received"], "options": {"reason": "ask the admin"},
        "scope": {"models": ["big-*"]}, "policy": "closed", "priority": 10})
    assert st == 201, got
    st, got = req(px, "POST", "/v1/fleet/hooks", {
        "name": "Full here", "endpoint": "http://127.0.0.1:9/full", "role": "observer",
        "stages": ["request.finished"], "payload": "full"})
    assert st == 201, got
    st, got = req(px, "POST", "/v1/fleet/decision-providers", {
        "name": "Jev", "endpoint": "http://127.0.0.1:9", "api_key": PKEY, "mode": "shadow",
        "questions": {"classify": {"model": "c2", "api_key": QKEY}, "judge": {}}})
    assert st == 201, got
    st, got = req(px, "POST", "/v1/fleet/decision-providers", {
        "name": "Own", "kind": "builtin", "mode": "on", "questions": {"memory_gate": {}}})
    assert st == 201, got
    plugin(px)
    change_set(px, [{"scope": "plugins", "target": "classify-provider", "set": {
        "settings.fallback": "chat", "secrets.API_KEY": PLUGIN_SECRET,
        "capabilities.prompts:redacted": True}}])
    change_set(px, [{"scope": "engine", "target": "h1", "set": {"MAX_QUEUE": 64}}])
    wait_restarts(px)
    change_set(px, [{"scope": "app", "set": {"conductor": "on"}}])
    st, vals = req(px, "GET", "/v1/settings/conductor")
    doc = next(v for v in vals["values"] if v["key"] == "settings")["value"]
    doc["journal"]["retention_days"] = 11
    change_set(px, [{"scope": "conductor", "set": {"settings": doc}}])
    out["retention"] = 11
    return out


def sections(doc: dict) -> dict:
    """The v2 sections as two controllers can share them: a provider's keys and a plugin's
    source (a path on each machine) aside."""
    out = {k: doc.get(k) for k in ("hooks", "decision_providers", "plugins", "engines",
                                   "conductor")}
    out["decision_providers"] = [{k: v for k, v in p.items() if k != "keys"}
                                 for p in out["decision_providers"]]
    out["plugins"] = [{k: v for k, v in p.items() if k not in ("source", "secrets")}
                      for p in out["plugins"]]
    return out


def export_a() -> tuple[dict, dict]:
    with Proxy({"TANDEM_BACKENDS": "ds", "TANDEM_BACKEND_DS_KIND": "deepseek",
                "TANDEM_BACKEND_DS_KEY": BACKEND_KEY}) as px:
        engine(px)
        planted = seed(px)
        st, doc = req(px, "GET", "/v1/fleet/app/export")
        assert st == 200, doc
        ok(doc, "fleet-config")
        st, app = req(px, "GET", "/v1/fleet/app")
        assert st == 200 and app["export"]["version"] == 2, app["export"]
        return doc, planted


# ---------------------------------------------------------------------------- 1

def test_round_trip():
    doc, planted = export_a()
    assert doc["version"] == 2
    hooks = {h["id"]: h for h in doc["hooks"]}
    assert set(hooks) == {"audit", "no-big-model", "full-here"}
    assert hooks["audit"]["secrets"] == ["secret"] and hooks["audit"]["mode"] == "shadow"
    assert hooks["no-big-model"]["scope"] == {"models": ["big-*"]}
    provs = {p["id"]: p for p in doc["decision_providers"]}
    assert provs["jev"]["keys"] == ["*", "classify"] and \
        provs["jev"]["questions"]["classify"] == {"model": "c2"}
    pl = doc["plugins"][0]
    assert (pl["id"], pl["version"], pl["settings"], pl["grants"]["capabilities"],
            pl["secrets"]) == ("classify-provider", "0.1.0", {"fallback": "chat"},
                               ["prompts:redacted"], ["API_KEY"]), pl
    assert pl["sha256"] and pl["source"]["type"] == "folder"
    assert doc["engines"]["h1"]["values"].get("MAX_QUEUE") in (64, "64"), doc["engines"]
    assert doc["conductor"]["settings"]["journal"]["retention_days"] == 11
    with Proxy() as px:
        engine(px)
        plugin(px)
        change_set(px, [{"scope": "app", "set": {"conductor": "on"}}])
        st, p = req(px, "POST", "/v1/fleet/app/import", {"config": doc})
        assert st == 200, p
        ok(p, "import-plan")
        assert p["allowed"], p["blocked"]
        kinds = {(i["kind"], i["id"]): i for i in p["items"]}
        for k in (("hook", "audit"), ("hook", "full-here"), ("provider", "jev"),
                  ("provider", "own")):
            assert kinds[k]["op"] == "add", kinds[k]
        assert kinds[("plugin", "classify-provider")]["op"] == "change"
        assert kinds[("engine", "h1")]["op"] == "change"
        assert kinds[("conductor", "settings")]["op"] in ("change", "add")
        assert "set them on its page" in kinds[("provider", "jev")]["detail"]
        scopes = {(x["scope"], x["target"]) for x in p["settings"]["parts"]}
        assert {("engine", "h1"), ("plugins", "classify-provider"),
                ("conductor", "controller")} <= scopes, scopes
        body = {"config": doc, "confirm": p["confirm"]}
        for k in ("confirm_fingerprint", "confirm_grant"):
            if p.get(k):
                body[k] = p[k]
        st, res = req(px, "POST", "/v1/fleet/app/import/apply", body)
        assert st == 200, res
        ok(res, "import-result")
        assert res["failed"] is None and res["settings"]["state"] == "applied", res
        assert sorted(res["created"]) == ["hook/audit", "hook/full-here", "hook/no-big-model",
                                          "provider/jev", "provider/own"], res
        wait_restarts(px)
        st, again = req(px, "GET", "/v1/fleet/app/export")
        assert st == 200
        a, b = sections(doc), sections(again)
        for k in a:
            assert a[k] == b[k], (k, a[k], b[k])
        # what is named only stays unset: the provider's keys, the plugin's secret
        assert {p["id"]: p["keys"] for p in again["decision_providers"]}["jev"] == []
        assert again["plugins"][0]["secrets"] == []
        # the grant of the full payload is the new controller's own
        full = px.svc.hooks.store.get("full-here")
        assert full["grant"]["lan"] and full["grant"]["by"]
    return "hooks, providers, a plugin, an engine overlay and a conductor document round trip"


# ---------------------------------------------------------------------------- 2

def test_no_secrets():
    doc, planted = export_a()
    text = json.dumps(doc)
    for s in (planted["hook_secret"], PKEY, QKEY, PLUGIN_SECRET, BACKEND_KEY):
        assert s not in text and s[4:20] not in text, s[:8]
    assert doc["secrets"] == "excluded" and any("signing secret" in x for x in doc["excluded"])
    # a file with secrets by mistake: stripped and listed, never answered
    bad = json.loads(text)
    bad["hooks"][0]["secret"] = PLANTED
    bad["decision_providers"][0]["api_key"] = PKEY
    bad["decision_providers"][0]["questions"]["classify"]["api_key"] = QKEY
    bad["plugins"][0]["secrets"] = {"API_KEY": PLUGIN_SECRET}
    bad["engines"]["h1"]["values"]["HF_TOKEN"] = "hf_" + "x" * 30
    with Proxy() as px:
        engine(px)
        plugin(px)
        change_set(px, [{"scope": "app", "set": {"conductor": "on"}}])
        st, p = req(px, "POST", "/v1/fleet/app/import", {"config": bad})
        assert st == 200, p
        ok(p, "import-plan")
        for where in ("hooks[0].secret", "decision_providers[0].api_key",
                      "decision_providers[0].questions.classify.api_key", "plugins[0].secrets",
                      "engines.h1.values.HF_TOKEN"):
            assert where in p["stripped"], (where, p["stripped"])
        assert p["allowed"], p["blocked"]
        body = {"config": bad, "confirm": p["confirm"]}
        for k in ("confirm_fingerprint", "confirm_grant"):
            if p.get(k):
                body[k] = p[k]
        st, res = req(px, "POST", "/v1/fleet/app/import/apply", body)
        assert st == 200, res
        wait_restarts(px)
        answers = json.dumps([p, res, req(px, "GET", "/v1/fleet/app/export")[1]])
        for s in (PLANTED, PKEY, QKEY, PLUGIN_SECRET, "hf_" + "x" * 30):
            assert s not in answers, s[:8]
        assert px.svc.providers.store.key_for("jev", "*") is None
        assert px.svc.plugins.secret_for("classify-provider", "API_KEY") is None
        for f in px.files():
            with open(f, "rb") as fh:
                raw = fh.read()
            for s in (PLANTED, PKEY, QKEY, PLUGIN_SECRET):
                assert s.encode() not in raw, (f, s[:8])
    return "names only; secrets in a file stripped and listed"


# ---------------------------------------------------------------------------- 3

def test_plan_then_confirm():
    doc, _ = export_a()
    with Proxy() as px:
        engine(px)
        change_set(px, [{"scope": "app", "set": {"conductor": "on"}}])
        # no plugin here yet: a conflict naming what to install; the rest is planned
        st, p = req(px, "POST", "/v1/fleet/app/import", {"config": doc})
        assert st == 200, p
        ok(p, "import-plan")
        it = {(i["kind"], i["id"]): i for i in p["items"]}
        c = it[("plugin", "classify-provider")]
        assert c["op"] == "conflict" and "install classify-provider 0.1.0" in c["detail"], c
        assert it[("hook", "audit")]["op"] == "add" and p["allowed"], p["blocked"]
        # nothing changes until the confirm
        assert px.svc.hooks.store.list() == [] and px.svc.providers.store.list() == []
        st, err = req(px, "POST", "/v1/fleet/app/import/apply", {"config": doc})
        assert st == 428, err
        st, err = req(px, "POST", "/v1/fleet/app/import/apply",
                      {"config": doc, "confirm": "nope"})
        assert st == 409 and err["error"]["type"] == "confirm_invalid", err
        assert px.svc.hooks.store.list() == []
        # a setting changed since the plan makes it stale
        change_set(px, [{"scope": "engine", "target": "h1", "set": {"MAX_QUEUE": 32}}])
        wait_restarts(px)
        body = {"config": doc, "confirm": p["confirm"]}
        for k in ("confirm_fingerprint", "confirm_grant"):
            if p.get(k):
                body[k] = p[k]
        st, err = req(px, "POST", "/v1/fleet/app/import/apply", body)
        assert st == 409 and err["error"]["type"] == "confirm_invalid", err
        st, p = req(px, "POST", "/v1/fleet/app/import", {"config": doc})
        if p.get("confirm_fingerprint"):
            st, err = req(px, "POST", "/v1/fleet/app/import/apply",
                          {"config": doc, "confirm": p["confirm"]})
            assert st == 428 and err["error"]["type"] == "fingerprint_confirm_required", err
        body = {"config": doc, "confirm": p["confirm"]}
        for k in ("confirm_fingerprint", "confirm_grant"):
            if p.get(k):
                body[k] = p[k]
        st, res = req(px, "POST", "/v1/fleet/app/import/apply", body)
        assert st == 200 and res["failed"] is None, res
        assert {h["id"] for h in px.svc.hooks.store.list()} == {"audit", "no-big-model",
                                                                 "full-here"}
        assert px.svc.plugins.list() == []
        wait_restarts(px)
    return "plan first; 428, 409; stale after a change; a missing plugin is a conflict"


# ---------------------------------------------------------------------------- 4

def test_versions():
    doc, _ = export_a()
    v1 = {k: v for k, v in doc.items() if k not in ("hooks", "decision_providers", "plugins",
                                                     "engines", "conductor", "notes")}
    v1["version"] = 1
    with Proxy() as px:
        st, p = req(px, "POST", "/v1/fleet/app/import", {"config": v1})
        assert st == 200, p
        ok(p, "import-plan")
        assert not any(i["kind"] in ("hook", "provider", "plugin", "engine", "conductor")
                       for i in p["items"])
        assert p["settings"] is None and p["stripped"] == []
        later = dict(doc, version=3)
        st, err = req(px, "POST", "/v1/fleet/app/import", {"config": later})
        assert st == 400 and "reads versions 1 and 2" in err["error"]["message"] and \
            "version 3" in err["error"]["message"], err
        mixed = dict(v1, hooks=doc["hooks"])
        st, err = req(px, "POST", "/v1/fleet/app/import", {"config": mixed})
        assert st == 400 and "set version 2" in err["error"]["message"], err
    return "v1 imports; v3 refused with its version; v1 with v2's sections refused"


if __name__ == "__main__":
    run(globals())
