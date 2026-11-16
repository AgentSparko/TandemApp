"""SRV-845, SRV-846 (D1): the settings interface end to end through a running controller
(proxy/app.py in process, tests/proxy_util.py): its routes and rights, the gateway while an engine
restarts for a settings change, the app scope's switch acting live, and the export. The engine host
is tests/fake_tandem_remote.py with its overlay model, reached through the local adapter. Every
answer is held to contracts/settings-v1.

  1  /v1/settings/*: reads under the dashboard's rule, plans and writes under the configuration's
     (401 from another machine without the admin, 200 with it, 200 from this machine), a write
     from another site 403, a body that is not JSON 415; a backend's key set through a change set
     reaches the relay's key store and is in no answer, no audit row and not in the fleet's
     export; the configuration's audit carries the change-set id
                                                        test_routes_rights_and_the_key
  2  Restart scheduling at the gateway: while an engine change waits for idle, the backend on that
     engine host answers 503 node_draining with Retry-After 30 and names the host, another backend
     keeps serving; the restart's countdown and cancel over HTTP; after the cancel the backend
     serves again, and the same change applied again is a new apply (not a replay of the
     cancelled one) that runs through to its warm-up   test_gateway_while_an_engine_restarts
  3  The app scope switches the conductor on live (its /v1/conductor/* answers at once), the
     conductor scope then edits the running conductor's documents, which it reads at the next
     request; off again through the app scope           test_app_and_conductor_live
  4  SRV-845 x SEC-16: the hooks scope is the registry's: a hook made on /v1/fleet/hooks has its
     keys in /v1/settings/hooks; with no admin password a change from another machine applies,
     but the full payload from there is 403 trust_required; from this machine it is planned with
     its warning and granted, and the runner sees the change. SRV-851: a decision provider made
     on /v1/fleet/decision-providers has its keys there too, and a change reaches the runtime
                                                        test_hooks_scope_and_pairing_rights

CPU only. Run: python tests/test_settings_http.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_backend  # noqa: E402
import fake_tandem_remote as ftr  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import enginectl  # noqa: E402
from proxy_util import Proxy, owner  # noqa: E402
from tools import contract_check  # noqa: E402

CON = os.path.join(ROOT, "contracts", "settings-v1")
LAN = {"X-Real-IP": "192.0.2.77"}
SECRET = "sk-" + "Hh5" * 12


def ok(doc, name: str):
    errs = contract_check.validate(doc, contract_check.load(name, CON), root=CON)
    assert not errs, (name, errs[:5], json.dumps(doc, default=str)[:600])
    return doc


def call(px, method: str, path: str, body=None, headers=None):
    st, h, raw = px.request(method, path, body, headers)
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        doc = raw
    return st, doc


def until(fn, s: float = 15.0):
    end = time.time() + s
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    return None


def apply(px, changes, headers=None, **kw):
    st, p = call(px, "POST", "/v1/settings/plan", {"changes": changes}, headers)
    assert st == 200, (st, p)
    ok(p, "plan")
    body = {"changes": changes, "confirm": p["confirm"], **kw}
    st, res = call(px, "POST", "/v1/settings/apply", body, headers)
    assert st == 200, (st, res)
    return p, ok(res, "result")


# ---------------------------------------------------------------------------- 1 routes, rights

def test_routes_rights_and_the_key():
    srv, port = fake_backend.start(models=["fake-a"])
    env = {"TANDEM_BACKENDS": "eng",
           "TANDEM_BACKEND_ENG_KIND": "generic",
           "TANDEM_BACKEND_ENG_URL": f"http://127.0.0.1:{port}/v1"}
    try:
        with Proxy(env=env) as px:
            BEARER = owner(px)              # SEC-28: an owner's session, not a token
            px.svc.cfg.create_backend({"id": "cloud", "kind": "openai",
                                       "url": f"http://127.0.0.1:{port}/v1"})
            # reads: the dashboard's rule
            st, err = call(px, "GET", "/v1/settings", headers=LAN)
            assert st == 401, (st, err)
            st, ov = call(px, "GET", "/v1/settings", headers={**LAN, **BEARER})
            assert st == 200 and ok(ov, "overview")["order"] == ["app", "auth", "api",
                                                                  "backends", "conductor",
                                                                  "engine", "hooks",
                                                                  "plugins"], ov
            assert next(s for s in ov["scopes"] if s["scope"] == "engine")["targets"] == []
            # writes: the configuration's
            ch = [{"scope": "backends", "set": {"backends.cloud.key": SECRET}}]
            st, err = call(px, "POST", "/v1/settings/plan", {"changes": ch}, LAN)
            assert st == 401, (st, err)
            st, err = call(px, "POST", "/v1/settings/plan", {"changes": ch},
                           {**BEARER, "Sec-Fetch-Site": "cross-site"})
            # the sign-in gate refuses it first (its code; type forbidden)
            assert st == 403 and err["error"].get("code") == "cross_site", err
            st, err = call(px, "POST", "/v1/settings/plan", {"changes": ch},
                           {**BEARER, "Content-Type": "text/plain"})
            assert st == 415, (st, err)
            p, res = apply(px, ch, {**LAN, **BEARER})
            assert res["state"] == "applied" and px.svc.cfg.key_for("cloud") == SECRET
            # reads take a sign-in on this machine too, once an owner has a password
            assert call(px, "GET", "/v1/settings/backends")[0] == 401
            st, vals = call(px, "GET", "/v1/settings/backends", headers=BEARER)
            assert st == 200 and ok(vals, "values")
            k = next(v for v in vals["values"] if v["key"] == "backends.cloud.key")
            assert k["value"] is None and k["set"], k
            # the env's fields are not writable here; the schema says which variable
            st, sch = call(px, "GET", "/v1/settings/backends/schema", headers=BEARER)
            url = next(x for x in ok(sch, "schema")["keys"] if x["key"] == "backends.eng.url")
            assert url["variable"] == "TANDEM_BACKEND_ENG_URL" and not url["writable"]
            # the audit, here and in the configuration's audit; the export
            st, aud = call(px, "GET", f"/v1/settings/audit?change_set={p['change_set']}",
                           headers=BEARER)
            assert st == 200 and ok(aud, "audit")["rows"], aud
            st, caud = call(px, "GET", "/v1/config/audit", headers=BEARER)
            assert any(r.get("change_set") == p["change_set"] for r in caud["audit"]), caud
            st, exp = call(px, "GET", "/v1/fleet/app/export", headers=BEARER)
            assert st == 200, (st, exp)
            for doc in (ov, vals, sch, aud, caud, exp, p, res):
                assert SECRET not in json.dumps(doc)
            assert SECRET not in "\n".join(px.logs)
            # unknown routes
            assert call(px, "GET", "/v1/settings/nope", headers=BEARER)[0] == 404
            assert call(px, "DELETE", "/v1/settings/backends", headers=BEARER)[0] == 404
    finally:
        srv.shutdown()
    return "reads as the dashboard, writes as the configuration; the key reaches the store only"


# ---------------------------------------------------------------------------- 2 the gateway

def _engine(px, sim: dict) -> str:
    d = ftr.install(tempfile.mkdtemp(prefix="settings-http-eng-"))
    ftr.set_state(d, sim=True, **sim)
    row = {"id": "h1", "alias": "box"}
    proxy_app.STATE["settings"] = proxy_app.settings_of(
        px.svc.cfg, px.svc, proxy_app.STATE["parts"], px.dir,
        ctl_for=lambda r: enginectl.LocalControl(d, label="h1"), rows=lambda: [row],
        backends_of=lambda h: ["eng"], warm=lambda b, m: (True, None), poll_s=0.05)
    return d


def _chat(px, model: str):
    st, h, raw = px.post("/v1/chat/completions", {"model": model, "stream": False, "messages": [
        {"role": "user", "content": "hello"}]})
    return st, h, json.loads(raw)


def test_gateway_while_an_engine_restarts():
    a, pa = fake_backend.start(models=["fake-a"])
    b, pb = fake_backend.start(models=["fake-b"])
    env = {"TANDEM_BACKENDS": "eng,other",
           "TANDEM_BACKEND_ENG_KIND": "generic",
           "TANDEM_BACKEND_ENG_URL": f"http://127.0.0.1:{pa}/v1",
           "TANDEM_BACKEND_OTHER_KIND": "generic",
           "TANDEM_BACKEND_OTHER_URL": f"http://127.0.0.1:{pb}/v1"}
    try:
        with Proxy(env=env) as px:
            px.sync()
            d = _engine(px, {"sim_hold": True})
            assert _chat(px, "fake-a")[0] == 200
            ch = [{"scope": "engine", "target": "h1", "set": {"MAX_QUEUE": 64}}]
            st, sch = call(px, "GET", "/v1/settings/engine/schema?target=h1")
            assert st == 200 and ok(sch, "schema")["keys"], sch
            p, res = apply(px, ch, deadline_s=300)
            rs = res["parts"][0]["restart"]
            assert rs["state"] == "draining" and rs["remaining_s"] <= 300, rs
            st, h, body = _chat(px, "fake-a")
            assert st == 503 and h.get("Retry-After") == "30", (st, h, body)
            assert body["error"]["code"] == "node_draining" and "box" in body["error"]["message"]
            assert _chat(px, "fake-b")[0] == 200, "another backend keeps serving"
            st, v = call(px, "GET", f"/v1/settings/restarts/{rs['id']}")
            assert st == 200 and ok(v, "restart")["can_cancel"], v
            st, lst = call(px, "GET", "/v1/settings/restarts")
            assert st == 200 and ok(lst, "restarts")["restarts"][0]["id"] == rs["id"]
            st, cv = call(px, "POST", f"/v1/settings/restarts/{rs['id']}/cancel", {})
            assert st == 200 and ok(cv, "restart")["state"] == "cancelled", cv
            assert _chat(px, "fake-a")[0] == 200, "back after the cancel"
            assert ftr._sim(ftr.state(d))["current"] == 0, "the engine kept its settings"
            # the restart through to the end: drained while it waits, served after the warm-up
            ftr.set_state(d, sim_hold=False, sim_steps=["waiting", "waiting", "acting", "done"])
            p, res = apply(px, ch)
            rid = res["parts"][0]["restart"]["id"]
            v = until(lambda: (lambda x: x if x["state"] in ("done", "failed", "timeout",
                                                             "cancelled") else None)(
                call(px, "GET", f"/v1/settings/restarts/{rid}")[1]))
            assert v and v["state"] == "done" and v["warm"]["ok"], \
                (v, call(px, "GET", f"/v1/settings/restarts/{rid}")[1], ftr.state(d))
            assert _chat(px, "fake-a")[0] == 200
            assert ftr._sim(ftr.state(d))["current"] == 1
            st, vers = call(px, "GET", "/v1/settings/engine/versions?target=h1")
            assert st == 200 and ok(vers, "versions")["current"] == "1", vers
            st, aud = call(px, "GET", "/v1/settings/audit")
            acts = {(r["action"], r["result"]) for r in ok(aud, "audit")["rows"]}
            assert ("settings.cancel", "cancelled") in acts and \
                ("settings.restart", "done") in acts, acts
    finally:
        a.shutdown()
        b.shutdown()
    return "503 with Retry-After for the engine's backend, the other serves; cancel; back"


# ---------------------------------------------------------------------------- 3 app, conductor

def test_app_and_conductor_live():
    with Proxy() as px:
        assert call(px, "GET", "/v1/conductor/profiles")[0] == 404
        p, res = apply(px, [{"scope": "app", "set": {"conductor": "on"}}])
        assert res["state"] == "applied", res
        st, prof = call(px, "GET", "/v1/conductor/profiles")
        assert st == 200, (st, prof)
        st, vals = call(px, "GET", "/v1/settings/conductor")
        assert st == 200 and ok(vals, "values")["why"] is None, vals
        doc = next(v for v in vals["values"] if v["key"] == "settings")["value"]
        doc["journal"]["retention_days"] = 11
        p, res = apply(px, [{"scope": "conductor", "set": {"settings": doc}}])
        assert res["parts"][0]["version_after"].startswith("settings@"), res
        cond = proxy_app.STATE["conductor"]
        assert cond.store.peek("settings")[1]["journal"]["retention_days"] == 11, \
            "the running conductor reads the new version"
        p, res = apply(px, [{"scope": "app", "set": {"conductor": "off"}}])
        assert call(px, "GET", "/v1/conductor/profiles")[0] == 404
        st, vals = call(px, "GET", "/v1/settings/conductor")
        assert st == 200 and vals["why"], "the documents stay, the scope says it is off"
        assert any(v["value"]["journal"]["retention_days"] == 11 for v in vals["values"]
                   if v["key"] == "settings")
    return "conductor on through the app scope, its document changed live, off again"



# ---------------------------------------------------------------------------- 4 the hooks scope

def test_hooks_scope_and_pairing_rights():
    """SEC-28: an operator from another device changes a hook's switches; its full payload
    needs pairing's rights (an admin's)."""
    with Proxy({}) as px:
        away = {"X-Forwarded-For": "203.0.113.50", **owner(px, "ops", "operator")}
        adm = {"X-Forwarded-For": "203.0.113.50", **owner(px, "adm", "admin")}
        st, out = call(px, "POST", "/v1/fleet/hooks", {
            "name": "Near", "id": "near", "endpoint": "http://192.168.1.20:9300/",
            "role": "observer", "stages": ["request.finished"]}, adm)
        assert st == 201, (st, out)
        st, sch = call(px, "GET", "/v1/settings/hooks/schema", headers=away)
        assert st == 200 and "hooks.near.payload" in {k["key"] for k in ok(sch, "schema")["keys"]}
        _, res = apply(px, [{"scope": "hooks", "set": {"hooks.near.mode": "shadow"}}], away)
        assert res["state"] == "applied" and px.svc.hooks.store.get("near")["mode"] == "shadow"
        assert px.svc.hooks.hooks()[0]["mode"] == "shadow"
        full = [{"scope": "hooks", "set": {"hooks.near.payload": "full"}}]
        st, err = call(px, "POST", "/v1/settings/plan", {"changes": full}, away)
        assert st == 403 and ok(err, "error")["error"]["type"] == "trust_required", (st, err)
        p, res = apply(px, full, adm)                              # an admin
        assert any("full payload" in w for w in p["parts"][0]["warnings"]), p
        g = px.svc.hooks.store.get("near")["grant"]
        assert res["state"] == "applied" and g and g["lan"], (res, g)
        st, out = call(px, "POST", "/v1/fleet/decision-providers", {
            "name": "Classes", "id": "s1", "endpoint": "http://192.168.1.30:8720",
            "questions": {"classify": {}}}, adm)
        assert st == 201, (st, out)
        st, vals = call(px, "GET", "/v1/settings/hooks", headers=away)
        assert {v["key"]: v["value"] for v in ok(vals, "values")["values"]}[
            "providers.s1.mode"] == "shadow", vals
        _, res = apply(px, [{"scope": "hooks", "set": {"providers.s1.mode": "on"}}], away)
        assert res["state"] == "applied" and [p["id"] for p in px.svc.providers.rows()] == ["s1"]
        assert px.svc.providers.rows()[0]["mode"] == "on"
    return "the registry's keys and the providers'; the full payload only with pairing's rights"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
