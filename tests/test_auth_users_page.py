"""SRV-875: the routes the page's Users screen needs, with TANDEM_AUTH=accounts.

  1  an account made without a password: the answer holds a one-time code, once; the person
     signs in with it, must set a password, and the code then stops     test_add_with_a_code
  2  POST /v1/auth/users/{id}/reset: a one-time code, the old password stops, the account's
     sessions end; only an owner resets an owner; never one's own account, never from local
     mode, never with a token                                            test_reset
  3  GET /v1/auth/audit: the rows of sign-ins (with the address), accounts, sessions, tokens and
     the auth and api settings, for the permission users only; never to a viewer, an operator
     or a token; no row holds a password or a code                       test_the_audit
  4  GET /v1/config/audit leaves those rows out for a caller without users  test_the_audit
  5  both routes have a permission; with the switch off they do not exist  test_routes
  6  the page's writes without a body (a DELETE: revoke, end a session, sign out everywhere,
     remove) pass the gate with the session's header and this server's Origin, and are refused
     without either                                          test_the_pages_writes_without_a_body

    python tests/test_auth_users_page.py
"""

from __future__ import annotations

import functools
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import app as proxy_app  # noqa: E402
from proxy import authapi, authz  # noqa: E402
from proxy_util import Proxy as _Proxy, run  # noqa: E402

# SEC-28: these tests run as before the flip made local mode the default of a bare
# process: a controller that is not listening yet, so this machine is trusted for nothing
Proxy = functools.partial(_Proxy, local=False)

ON = {"TANDEM_AUTH": "accounts"}
PW = {n: f"the password of {n} here" for n in ("own", "own2", "adm", "ops", "view", "ann")}
NEW_PW = "a new password of her own"


def accounts():
    return proxy_app.STATE["accounts"]


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def make(name: str, role: str) -> dict:
    return accounts().store.create(name, role, PW[name], actor="test")


def sign_in(px, name: str, password: str | None = None) -> dict:
    st, h, out = px.post("/v1/auth/session", {"username": name, "password": password or PW[name]})
    assert st == 200, (st, out)
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def test_add_with_a_code():
    with Proxy(env=ON) as px:
        make("own", "owner")
        own = sign_in(px, "own")
        st, h, out = px.post("/v1/auth/users", {"username": "ann", "role": "viewer"}, own)
        doc = json.loads(out)
        assert st == 201 and h.get("Cache-Control") == "no-store", (st, h, doc)
        code = doc["code"]
        assert isinstance(code, str) and len(code) >= 16 and doc["code_valid_s"] == 86400, doc
        assert doc["user"]["username"] == "ann" and doc["user"]["must_change"] is True
        assert doc["user"]["has_password"] is True
        # never again: not in the list, not in the audit
        st, users = j(px.get("/v1/auth/users", own))
        assert code not in json.dumps(users)
        assert code not in json.dumps(px.svc.cfg.audit(limit=500))
        # the code signs in once the person has it, and only a new password goes on
        ann = sign_in(px, "ann", code)
        st, me = j(px.get("/v1/auth/session", ann))
        assert me["account"]["must_change"] is True, me
        assert px.get("/v1/settings", ann)[0] == 403
        st, h, out = px.request("PUT", "/v1/auth/password", {"current": code, "password": NEW_PW},
                                ann)
        assert st == 200, (st, out)
        st, h, out = px.post("/v1/auth/session", {"username": "ann", "password": code})
        assert st == 401, "the code stopped with the new password"
        ann = sign_in(px, "ann", NEW_PW)
        assert j(px.get("/v1/auth/session", ann))[1]["account"]["must_change"] is False
        # with a password in the body it is as before: no code
        st, out = j(px.post("/v1/auth/users", {"username": "ops", "role": "operator",
                                               "password": PW["ops"]}, own))
        assert st == 201 and "code" not in out, out
        # the same refusals as with a password
        for body, want in (({"username": "ann", "role": "viewer"}, 409),
                           ({"username": "x", "role": "root"}, 400),
                           ({"username": "Bad Name!", "role": "viewer"}, 400),
                           ({"username": "x", "role": "viewer", "more": 1}, 400)):
            assert j(px.post("/v1/auth/users", body, own))[0] == want, body
        assert [u["username"] for u in j(px.get("/v1/auth/users", own))[1]["users"]].count("x") == 0


def test_reset():
    with Proxy(env=ON) as px:
        own_id = make("own", "owner")["id"]
        adm_id = make("adm", "admin")["id"]
        view_id = make("view", "viewer")["id"]
        own, adm, view = sign_in(px, "own"), sign_in(px, "adm"), sign_in(px, "view")
        url = f"/v1/auth/users/{view_id}/reset"
        # a viewer reaches no users route; an unknown account is 404
        assert px.post(url, None, view)[0] == 403
        assert px.post("/v1/auth/users/nobody/reset", None, adm)[0] == 404
        assert px.request("GET", url, None, adm)[0] == 404
        st, h, out = px.post(url, None, adm)
        doc = json.loads(out)
        assert st == 200 and h.get("Cache-Control") == "no-store", (st, doc)
        code = doc["code"]
        assert doc["user"]["id"] == view_id and doc["user"]["must_change"] is True
        assert code not in json.dumps(px.svc.cfg.audit(limit=500))
        # the old password stopped and the account's sessions ended
        assert px.get("/v1/auth/session", view)[1].get("Set-Cookie") is None
        assert j(px.get("/v1/auth/session", view))[1]["authenticated"] is False
        assert px.post("/v1/auth/session", {"username": "view", "password": PW["view"]})[0] == 401
        sign_in(px, "view", code)
        # only an owner resets an owner; nobody resets their own account here
        st, out = j(px.post(f"/v1/auth/users/{own_id}/reset", None, adm))
        assert st == 403 and out["error"]["permission"] == "owner", out
        st, out = j(px.post(f"/v1/auth/users/{adm_id}/reset", None, adm))
        assert st == 409 and out["error"]["code"] == "own_account", out
        assert j(px.get("/v1/auth/session", adm))[1]["authenticated"] is True
        own2_id = make("own2", "owner")["id"]
        st, out = j(px.post(f"/v1/auth/users/{own2_id}/reset", None, own))
        assert st == 200 and out["code"], out
        # a token never resets a password
        st, h, out = px.post("/v1/auth/tokens", {"label": "all", "scopes": [
            "settings:read", "settings:write", "fleet:read", "fleet:write"]}, own)
        token = json.loads(out)["token"]
        assert px.post(url, None, {"Authorization": f"Bearer {token}"})[0] == 403
    # local mode: the owner's password first
    with Proxy(env=ON) as px:
        a = accounts()
        row = a.store.create("ann", "viewer", None, actor="test")
        a.local.bind = "127.0.0.1"
        a.local.exists = lambda p: False
        st, out = j(px.post(f"/v1/auth/users/{row['id']}/reset", None))
        assert st == 409 and out["error"]["code"] == "password_first", (st, out)
        assert a.store.get(row["id"])["pw"] is None


def test_the_audit():
    with Proxy(env=ON) as px:
        make("own", "owner")
        make("adm", "admin")
        make("ops", "operator")
        make("view", "viewer")
        px.post("/v1/auth/session", {"username": "view", "password": "not her password"})
        own, adm, ops, view = (sign_in(px, n) for n in ("own", "adm", "ops", "view"))
        st, h, out = px.post("/v1/auth/users", {"username": "ann", "role": "viewer"}, adm)
        code = json.loads(out)["code"]
        st, h, out = px.post("/v1/auth/tokens", {"label": "mgmt", "scopes": [
            "settings:read", "settings:write", "fleet:read", "usage:read", "metrics:read"]}, own)
        token = json.loads(out)["token"]
        st, plan = j(px.post("/v1/settings/plan", {"changes": [{"scope": "api", "set": {
            "tokens.viewers_may_create": "on"}}]}, adm))
        assert st == 200, plan
        st, done = j(px.post("/v1/settings/apply", {"changes": [{"scope": "api", "set": {
            "tokens.viewers_may_create": "on"}}], "confirm": plan["confirm"]}, adm))
        assert st == 200, done
        for who in (own, adm):
            st, h, out = px.get("/v1/auth/audit", who)
            doc = json.loads(out)
            assert st == 200 and h.get("Cache-Control") == "no-store", (st, doc)
            rows = doc["rows"]
            acts = {(r["object"], r["action"]) for r in rows}
            assert {("session", "sign_in"), ("session", "sign_in_failed"), ("account", "create"),
                    ("api_token", "create"), ("setting", "set")} <= acts, acts
            failed = next(r for r in rows if r["action"] == "sign_in_failed")
            assert failed["who"] == "view" and failed["detail"]["address"], failed
            assert all(set(r) == {"at", "who", "action", "object", "object_id", "fields",
                                  "detail"} for r in rows), rows[0]
            assert rows == sorted(rows, key=lambda r: r["at"], reverse=True)
            text = json.dumps(doc)
            for secret in (code, token, *PW.values(), "not her password"):
                assert secret not in text
        assert len(j(px.get("/v1/auth/audit?limit=2", own))[1]["rows"]) == 2
        for bad in ("0", "501", "many"):
            assert px.get(f"/v1/auth/audit?limit={bad}", own)[0] == 400, bad
        # never to a viewer, an operator or a token
        for who in (ops, view, {"Authorization": f"Bearer {token}"}, {}):
            st, out = j(px.get("/v1/auth/audit", who))
            assert st in (401, 403), (who, st, out)
        # the configuration's audit leaves the rows about people out for them
        for who, sees in ((own, True), (adm, True), (ops, False), (view, False),
                          ({"Authorization": f"Bearer {token}"}, False)):
            st, out = j(px.get("/v1/config/audit", who))
            assert st == 200, (who, st, out)
            got = [r for r in out["audit"] if authapi.private_audit(r)]
            assert bool(got) is sees, (who, got[:2])
            assert "not her password" not in json.dumps(out)


def browser(px, method: str, path: str, who: dict, body=None, origin: str | None = None,
            csrf: bool = True):
    """A write as the page's fetch sends it: the cookie, the session's header, an Origin, and a
    content type only with a body (a DELETE has none)."""
    import http.client
    h = {"Cookie": who["Cookie"], "Origin": origin or f"http://127.0.0.1:{px.port}",
         "Sec-Fetch-Site": "same-origin"}
    if csrf:
        h["X-Tandem-CSRF"] = who["X-Tandem-CSRF"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c = http.client.HTTPConnection("127.0.0.1", px.port, timeout=30)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, (json.loads(out) if out else None)


def test_the_pages_writes_without_a_body():
    """The page's DELETEs carry no body and no content type: the gate takes them with the
    session's header and this server's Origin, and refuses them without either."""
    with Proxy(env=ON) as px:
        make("own", "owner")
        view_id = make("view", "viewer")["id"]
        ops_id = make("ops", "operator")["id"]
        own, own2, ops = sign_in(px, "own"), sign_in(px, "own"), sign_in(px, "ops")
        st, tok = browser(px, "POST", "/v1/auth/tokens", own, {"label": "desk", "scopes": [
            "inference:openai"]})
        assert st == 201, tok
        url = f"/v1/auth/tokens/{tok['id']}"
        # without the session's header, or from another origin: refused, nothing changed
        st, out = browser(px, "DELETE", url, own, csrf=False)
        assert st == 403 and out["error"]["code"] == "csrf", out
        st, out = browser(px, "DELETE", url, own, origin="http://elsewhere.example")
        assert st == 403 and out["error"]["code"] == "cross_origin", out
        assert accounts().tokens.get(tok["id"])["revoked_at"] is None
        st, out = browser(px, "DELETE", url, own)
        assert st == 200 and out["state"] == "revoked", (st, out)
        # a session of another browser, every session of another account, an account
        other = next(s for s in j(px.get("/v1/auth/sessions", own))[1]["sessions"]
                     if not s["current"])
        assert browser(px, "DELETE", f"/v1/auth/sessions/{other['id']}", own)[0] == 204
        assert j(px.get("/v1/auth/session", own2))[1]["authenticated"] is False
        assert browser(px, "DELETE", f"/v1/auth/sessions?user={ops_id}", own)[0] == 204
        assert j(px.get("/v1/auth/session", ops))[1]["authenticated"] is False
        assert browser(px, "DELETE", f"/v1/auth/users/{view_id}", own)[0] == 204
        # the reset sends an empty JSON object; a POST with no body at all passes too
        st, out = browser(px, "POST", f"/v1/auth/users/{ops_id}/reset", own, {})
        assert st == 200 and out["code"], out
        st, out = browser(px, "POST", f"/v1/auth/users/{ops_id}/reset", own)
        assert st == 200 and out["code"], out
        # sign out, then sign out everywhere: this browser's cookie is cleared
        own3 = sign_in(px, "own")
        assert browser(px, "DELETE", "/v1/auth/session", own3)[0] == 204
        assert j(px.get("/v1/auth/session", own3))[1]["authenticated"] is False
        assert browser(px, "DELETE", "/v1/auth/sessions", own)[0] == 204
        assert j(px.get("/v1/auth/session", own))[1]["authenticated"] is False


def test_routes():
    assert authz.permission_for("GET", "/v1/auth/audit") == "users"
    assert authz.permission_for("POST", "/v1/auth/users/abc/reset") == "users"
    assert authz.permission_for("POST", "/v1/auth/audit") is None
    assert authapi.private_audit({"object": "setting", "object_id": "auth.local_mode"})
    assert authapi.private_audit({"object": "setting", "object_id": "api.listen"})
    assert not authapi.private_audit({"object": "setting", "object_id": "app.chat"})
    assert not authapi.private_audit({"object": "backend", "object_id": "engine"})
    # SEC-28: no switch: the routes are always there and need a sign-in; local mode (this
    # machine, no password) is the owner and reads every row
    with Proxy() as px:
        assert px.get("/v1/auth/audit")[0] == 401
        assert px.post("/v1/auth/users/x/reset", None)[0] == 401
    with _Proxy() as px:
        px.svc.cfg.log_audit("someone", "sign_in", "session", "abc", detail={"address": "x"})
        st, out = j(px.get("/v1/config/audit"))
        assert st == 200 and any(r["object"] == "session" for r in out["audit"]), out
        assert px.get("/v1/auth/audit")[0] == 200


if __name__ == "__main__":
    run(globals())
