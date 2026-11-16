"""SEC-22: accounts, roles, one permission table and the local identity provider
(proxy/authz.py, proxy/identity.py, proxy/accounts.py, proxy/authapi.py), with TANDEM_AUTH=accounts.

  1  the role matrix: each of the four roles holds exactly its permissions, each role those of
     the roles below it; a viewer chats only with `auth.chat_min_role` viewer and makes inference
     tokens only with `api.tokens.viewers_may_create` on (both states)       test_role_matrix
  2  every route of the dispatch code has a permission: every path the handlers compare against,
     every route kind passed to `_allowed`/`decide` or returned for one; a made-up route and a
     made-up kind are found                                      test_every_route_has_a_permission
  3  Gherkin "A role opens what it should": an operator's settings write passes, pairing a node
     is 403 naming "trust"; a viewer's settings write is 403 naming "settings:write"
                                                                  test_operator_and_viewer
  4  Gherkin "A route without an entry": 403 for every account, logged once  test_unmapped_route
  5  Gherkin "The last owner": an admin's disable, demotion and removal of the one enabled owner
     are 409 and change nothing; an admin cannot touch an owner that is not the last (403); only
     an owner makes an owner; a second owner makes the first one removable  test_last_owner
  6  Gherkin "A wrong password and an unknown user cost the same": both 401 with the same body,
     each one hash; a disabled account the same                      test_wrong_and_unknown_cost_same
  7  a PBKDF2 hash (a Python without scrypt) verifies on one with scrypt, over the provider and
     over the sign-in                                                 test_pbkdf2_hash_verifies
  8  no password, hash or salt in any answer, log line, audit row or file but the database's
     own hash column                                                  test_no_secret_anywhere
  9  the provider interface: the local provider's capabilities, the base answers nothing, the
     legacy secrets (the admin token, admin.json, trusted-local) open nothing   test_provider_and_legacy
 10  every existing route runs over the accounts: an owner's session gets what an open controller
     of the old rule answers, but for the routes the accounts change on purpose; nothing drops
     or answers 5xx                                                   test_old_routes_over_accounts

    python tests/test_auth_accounts.py
"""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import accounts as acc_mod  # noqa: E402
from proxy import adminpw, authz  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import identity as identity_mod  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

ON = {"TANDEM_AUTH": "accounts"}
PW = {"own": "the first long password", "adm": "an admin's long password",
      "ops": "an operator's long password", "view": "a viewer's long password",
      "own2": "the second long password"}


def accounts():
    return proxy_app.STATE["accounts"]


def make(name: str, role: str) -> dict:
    return accounts().store.create(name, role, PW[name], actor="test")


def sign_in(px, name: str, password: str | None = None, headers=None) -> dict:
    st, h, out = px.post("/v1/auth/session", {"username": name,
                                              "password": password or PW[name]}, headers)
    assert st == 200, (st, out)
    cookie = h["Set-Cookie"].split(";", 1)[0]
    return {"Cookie": cookie, "X-Tandem-CSRF": json.loads(out)["csrf"]}


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


# ---------------------------------------------------------------------------- 1

def test_role_matrix():
    want = {
        "viewer": {"read", "metrics:read", "fleet:read", "inference", "self"},
        "operator": {"read", "metrics:read", "fleet:read", "inference", "self", "settings:write",
                     "fleet:write", "chat", "tokens:inference", "tokens:management"},
    }
    want["admin"] = want["operator"] | {"trust", "users"}
    want["owner"] = want["admin"] | {"owner"}
    off = authz.Policy(values={})
    for role, perms in want.items():
        assert authz.role_permissions(role, off) == perms, role
        p = acc_mod.Principal(kind="account", id="x", name="x", role=role)
        for perm in authz.PERMISSIONS:
            assert authz.allow(p, perm, off) == (perm in perms), (role, perm)
        assert authz.allow(p, authz.OPEN, off)
        # no role holds a service's permission
        assert not perms & authz.SERVICE
    assert authz.role_permissions("nobody", off) == frozenset()
    assert not authz.allow(None, "read", off) and authz.allow(None, authz.OPEN, off)
    # each role holds the ones below it
    for lo, hi in zip(authz.ROLES, authz.ROLES[1:]):
        assert want[lo] < want[hi]
    # SEC-22: a viewer's inference tokens follow the admin switch, off by default
    v = acc_mod.Principal(kind="account", id="v", name="v", role="viewer")
    o = acc_mod.Principal(kind="account", id="o", name="o", role="operator")
    for state, viewer_may in ((None, False), (False, False), ("off", False), (True, True),
                              ("on", True)):
        pol = authz.Policy(values={} if state is None else
                           {"api.tokens.viewers_may_create": state})
        assert authz.allow(v, "tokens:inference", pol) == viewer_may, state
        assert authz.allow(o, "tokens:inference", pol), state
        assert not authz.allow(v, "tokens:management", pol)
    # who may chat: the operator by default, the viewer when the setting says so
    assert not authz.allow(v, "chat", off) and authz.allow(o, "chat", off)
    assert authz.allow(v, "chat", authz.Policy(values={"auth.chat_min_role": "viewer"}))
    # the settings live in the config database's settings table
    with Proxy(env=ON) as px:
        cfg = px.svc.cfg
        pol = accounts().policy
        assert not pol.viewers_may_create and pol.idle_days == 30 and pol.absolute_days == 180
        cfg.set_setting("api.tokens.viewers_may_create", True, who="test")
        cfg.set_setting("auth.session.idle_days", 9999, who="test")
        assert pol.viewers_may_create and pol.idle_days == 400
        assert authz.allow(v, "tokens:inference", pol)
    # a session address opens its own routes only; a token holds its scopes only
    s = acc_mod.Principal(kind=authz.SESSION_ADDRESS, id="k", name="k", via="address")
    assert authz.allow(s, authz.SESSION_ADDRESS) and not authz.allow(s, "read")
    t = acc_mod.Principal(kind="token", id="t", name="t", via="token",
                          scopes=frozenset({"inference"}))
    assert authz.allow(t, "inference") and not authz.allow(t, "read")


# ---------------------------------------------------------------------------- 2

PATH_NAMES = {"path", "raw", "p"}
KIND_FUNCS = {"_route_rights", "_keys_rights", "route_for"}


def _consts(node) -> list[str]:
    """The strings an expression can evaluate to: a constant, or a branch of a conditional."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _consts(node.body) + _consts(node.orelse)
    return []


def dispatch_paths(source: str, names: dict) -> dict:
    """{method: {probe path}} of the routes the Handler's do_GET, do_POST and _write compare
    a path against (==, in, startswith), with a named constant read from `names`."""
    tree = ast.parse(source)
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    which = {"do_GET": ("GET",), "do_POST": ("POST",), "_write": ("PUT", "PATCH", "DELETE")}
    out: dict = {m: set() for ms in which.values() for m in ms}

    def values(node) -> list:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [v for e in node.elts for v in values(e)]
        if isinstance(node, ast.Name) and node.id in names:
            got = names[node.id]
            return [got] if isinstance(got, str) else list(got)
        if isinstance(node, ast.Attribute) and node.attr in names:
            got = names[node.attr]
            return [got] if isinstance(got, str) else list(got)
        return []

    def only(fn) -> dict:
        """{id(node): methods} for the path tests under `method == "X" and ...`."""
        got = {}
        for b in ast.walk(fn):
            if not (isinstance(b, ast.BoolOp) and isinstance(b.op, ast.And)):
                continue
            ms = {c.comparators[0].value for c in b.values if isinstance(c, ast.Compare)
                  and isinstance(c.left, ast.Name) and c.left.id == "method"
                  and isinstance(c.ops[0], ast.Eq) and isinstance(c.comparators[0], ast.Constant)}
            if ms:
                for v in b.values:
                    for n in ast.walk(v):
                        got[id(n)] = ms
        return got

    for fn in handler.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in which:
            continue
        restrict = only(fn)
        for n in ast.walk(fn):
            found = []
            if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and \
                    n.left.id in PATH_NAMES:
                for op, right in zip(n.ops, n.comparators):
                    if isinstance(op, (ast.Eq, ast.In)):
                        found += [(v, False) for v in values(right)]
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                    n.func.attr == "startswith" and isinstance(n.func.value, ast.Name) and \
                    n.func.value.id in PATH_NAMES and n.args:
                found += [(v, True) for v in values(n.args[0])]
            for v, prefix in found:
                if not v.startswith("/"):
                    continue
                probe = (v.rstrip("/") + "/x") if prefix else v
                if v.startswith("/s/"):
                    probe = "/s/abcdefabcdef/v1/library"
                for m in which[fn.name]:
                    if m in restrict.get(id(n), {m}):
                        out[m].add(probe)
    return out


def route_kinds(paths: list[str]) -> dict:
    """{kind: where} of every route kind the code passes to `_allowed`/`decide`, assigns to a
    `route` it passes on, or returns from a rights function."""
    out = {}
    for path in paths:
        tree = ast.parse(open(path).read())
        rel = os.path.relpath(path, ROOT)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                    n.func.attr in ("_allowed", "decide") and n.args:
                for v in _consts(n.args[0]):
                    out.setdefault(v, f"{rel}:{n.lineno}")
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "route"
                                                 for t in n.targets):
                for v in _consts(n.value):
                    out.setdefault(v, f"{rel}:{n.lineno}")
            if isinstance(n, ast.FunctionDef) and n.name in KIND_FUNCS:
                for r in ast.walk(n):
                    if isinstance(r, ast.Return) and r.value is not None:
                        for v in _consts(r.value):
                            out.setdefault(v, f"{rel}:{r.lineno}")
    return out


def test_every_route_has_a_permission():
    from proxy import chatengine, deployments, pluginweb, service
    from tandemapp import chat_forward
    names = {"GATEWAY": proxy_app.GATEWAY, "PROXY_PATHS": service.PROXY_PATHS,
             "CACHE_PATHS": deployments.CACHE_PATHS, "PANELS": pluginweb.PANELS,
             "BACKENDS": pluginweb.BACKENDS}
    got = dispatch_paths(open(os.path.join(ROOT, "proxy", "app.py")).read(), names)
    assert sum(len(v) for v in got.values()) >= 40, got
    # the sub-dispatchers in front of them: the Chat tab, the chat engine's prefix, the session
    # address, the plugins' pages
    for m in got:
        got[m] |= {chat_forward.PREFIX + "/x", chatengine.PREFIX + "/v1/chat/completions"}
    got["GET"] |= {chatengine.PREFIX + "/health", chatengine.PREFIX + "/v1/dashboard/system",
                   chatengine.PREFIX + "/v1/dashboard/live", chatengine.PREFIX + "/v1/models",
                   "/v1/auth/session", "/v1/auth/sessions", "/v1/auth/users",
                   "/v1/auth/tokens", "/v1/auth/tokens/x", "/v1/auth/audit"}
    got["POST"] |= {"/v1/auth/session", "/v1/auth/users", "/v1/auth/tokens",
                    "/v1/auth/tokens/x/rotate", "/v1/auth/users/x/reset"}
    for m in ("PATCH", "DELETE"):
        got.setdefault(m, set()).add("/v1/auth/tokens/x")         # SRV-867
    missing = sorted(f"{m} {p}" for m, ps in got.items() for p in ps
                     if authz.permission_for(m, p) is None)
    assert not missing, f"routes without a permission (proxy/authz.py ROUTES): {missing}"
    # every route kind maps to a permission, and every permission exists
    files = [f for d in ("proxy", "tandemapp") for f in
             glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True)]
    kinds = route_kinds(files)
    from proxy import fleethealth
    kinds.update({a[3]: "proxy/fleethealth.py ACTIONS" for a in fleethealth.ACTIONS.values()})
    assert {"dashboard", "conductor_write", "fleet_trust", "setup"} <= set(kinds), kinds
    unknown = {k: w for k, w in kinds.items() if k not in authz.KIND_PERMISSION}
    assert not unknown, f"route kinds without a permission: {unknown}"
    assert set(authz.KIND_PERMISSION.values()) <= authz.PERMISSIONS
    assert {r[3] for r in authz.ROUTES} <= authz.PERMISSIONS | {authz.OPEN}
    # the checks find a made-up route and a made-up kind
    planted = dispatch_paths("class Handler:\n def do_GET(self):\n  path = self.path\n"
                             "  if path == '/v1/brand-new':\n   pass\n"
                             "  if path.startswith('/v1/other-new/'):\n   pass\n", {})
    assert planted["GET"] == {"/v1/brand-new", "/v1/other-new/x"}
    assert all(authz.permission_for("GET", p) is None for p in planted["GET"])
    tmp = os.path.join(tempfile.mkdtemp(prefix="kinds-"), "planted.py")
    try:
        with open(tmp, "w") as f:
            f.write("def f(h):\n    return h._allowed('brand_new_kind')\n")
        assert "brand_new_kind" in route_kinds([tmp])
    finally:
        os.remove(tmp)
    return f"{sum(len(v) for v in got.values())} routes, {len(kinds)} kinds"


# ---------------------------------------------------------------------------- 3

def test_operator_and_viewer():
    with Proxy(env=ON) as px:
        make("own", "owner")
        make("ops", "operator")
        make("view", "viewer")
        ops = sign_in(px, "ops")
        view = sign_in(px, "view")
        plan = {"changes": [{"scope": "app", "target": "controller", "set": {"chat": "off"}}]}
        st, out = j(px.post("/v1/settings/plan", plan, ops))
        assert st not in (401, 403), (st, out)
        st, out = j(px.post("/v1/fleet/pair", {"address": "192.0.2.9"}, ops))
        assert st == 403 and out["error"]["permission"] == "trust", out
        st, out = j(px.post("/v1/settings/plan", plan, view))
        assert st == 403 and out["error"]["permission"] == "settings:write", out
        assert px.get("/v1/settings", view)[0] == 200
        assert px.get("/v1/dashboard/summary", view)[0] == 200
        # the Chat tab is the operator's by default
        st, out = j(px.get("/v1/chat-app/chats", view))
        assert st == 403 and out["error"]["permission"] == "chat", out
        assert px.get("/v1/chat-app/chats", ops)[0] not in (401, 403)
        # no credential: 401 on everything but the open routes
        for path in ("/v1/settings", "/v1/fleet/nodes", "/v1/dashboard/summary", "/metrics",
                     "/v1/models", "/v1/auth/sessions"):
            st, h, _ = px.get(path)
            assert st == 401 and "Bearer" in h.get("WWW-Authenticate", ""), (path, st)
        assert j(px.get("/health")) == (200, {"status": "ok"})
        assert "backends" in j(px.get("/health", view))[1]
        assert j(px.get("/v1/auth/session"))[1]["authenticated"] is False
        # the audit names the account that wrote
        st, out = j(px.request("PUT", "/v1/fleet/layouts/fleet",
                               {"device": "desk", "doc": {"a": 1}}, ops))
        assert st in (200, 400), (st, out)
        assert px.get("/v1/auth/users", ops)[0] == 403
        assert accounts().who_of(type("H", (), {"headers": ops, "_principal": None})()) == "ops"


# ---------------------------------------------------------------------------- 4

def test_unmapped_route():
    with Proxy(env=ON) as px:
        make("own", "owner")
        own = sign_in(px, "own")
        for _ in range(3):
            st, out = j(px.get("/v1/brand-new-route", own))
            assert st == 403 and out["error"]["code"] == "unmapped_route", out
        st, out = j(px.request("DELETE", "/v1/brand-new-route", None, own))
        assert st == 403 and out["error"]["code"] == "unmapped_route"
        assert j(px.get("/v1/brand-new-route"))[0] == 403
        lines = [x for x in px.logs if "/v1/brand-new-route" in x]
        assert len(lines) == 2, lines             # once per method
        assert "GET /v1/brand-new-route" in lines[0]


# ---------------------------------------------------------------------------- 5

def test_last_owner():
    with Proxy(env=ON) as px:
        own = make("own", "owner")
        make("adm", "admin")
        adm = sign_in(px, "adm")
        owner = sign_in(px, "own")
        url = f"/v1/auth/users/{own['id']}"
        before = j(px.get("/v1/auth/users", adm))[1]["users"]
        for change in ({"enabled": False}, {"role": "admin"}, {"role": "viewer",
                                                               "enabled": False}):
            st, out = j(px.request("PATCH", url, change, adm))
            assert st == 409 and out["error"]["code"] == "last_owner", (change, out)
        st, out = j(px.request("DELETE", url, None, adm))
        assert st == 409 and out["error"]["code"] == "last_owner"
        # the owner on itself: the same
        assert j(px.request("PATCH", url, {"role": "admin"}, owner))[0] == 409
        assert j(px.request("DELETE", url, None, owner))[0] == 409
        assert j(px.get("/v1/auth/users", adm))[1]["users"] == before, "nothing changed"
        assert px.get("/v1/settings", owner)[0] == 200, "its sessions live on"
        # only an owner makes an owner
        st, out = j(px.post("/v1/auth/users", {"username": "own2", "role": "owner",
                                               "password": PW["own2"]}, adm))
        assert st == 403 and out["error"]["permission"] == "owner"
        st, out = j(px.post("/v1/auth/users", {"username": "own2", "role": "owner",
                                               "password": PW["own2"]}, owner))
        assert st == 201 and out["user"]["role"] == "owner" and "pw" not in out["user"]
        own2 = out["user"]["id"]
        # not the last now: an admin still cannot touch an owner
        assert j(px.request("PATCH", url, {"enabled": False}, adm))[1]["error"]["permission"] \
            == "owner"
        assert j(px.request("DELETE", f"/v1/auth/users/{own2}", None, adm))[0] == 403
        # an owner can: the second one removes the first, whose sessions end
        own2h = sign_in(px, "own2")
        assert j(px.request("DELETE", url, None, own2h))[0] == 204
        assert px.get("/v1/settings", owner)[0] == 401
        assert j(px.request("DELETE", f"/v1/auth/users/{own2}", None, own2h))[0] == 409
        # a duplicate name, a bad role, a short password
        for body, code in (({"username": "own2", "role": "viewer", "password": PW["view"]}, 409),
                           ({"username": "x", "role": "root", "password": PW["view"]}, 400),
                           ({"username": "x", "role": "viewer", "password": "short"}, 400),
                           ({"username": "Bad Name!", "role": "viewer",
                             "password": PW["view"]}, 400)):
            assert j(px.post("/v1/auth/users", body, own2h))[0] == code, body
        # a lower role or a disable ends the account's sessions
        st, out = j(px.post("/v1/auth/users", {"username": "ops", "role": "operator",
                                               "password": PW["ops"]}, own2h))
        ops_id = out["user"]["id"]
        ops = sign_in(px, "ops")
        st, out = j(px.request("PATCH", f"/v1/auth/users/{ops_id}", {"role": "viewer"}, own2h))
        assert st == 200 and out["sessions_ended"] and px.get("/v1/settings", ops)[0] == 401
        ops = sign_in(px, "ops")
        st, out = j(px.request("PATCH", f"/v1/auth/users/{ops_id}", {"role": "admin"}, own2h))
        assert st == 200 and not out["sessions_ended"] and px.get("/v1/settings", ops)[0] == 200
        st, out = j(px.request("PATCH", f"/v1/auth/users/{ops_id}", {"enabled": False}, own2h))
        assert st == 200 and px.get("/v1/settings", ops)[0] == 401
        assert px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]})[0] == 401


# ---------------------------------------------------------------------------- 6

def test_wrong_and_unknown_cost_same():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        prov = accounts().provider
        answers = []
        for i, name in enumerate(("ops", "bob")):
            n = prov.derives
            st, h, out = px.post("/v1/auth/session", {"username": name,
                                                      "password": "not the right one"},
                                 {"X-Real-IP": f"192.0.2.{i + 1}"})
            assert prov.derives == n + 1, name
            answers.append((st, out))
            assert "Set-Cookie" not in h
        assert answers[0] == answers[1] and answers[0][0] == 401, answers
        # a disabled account: the same answer, the same one hash
        rid = accounts().store.by_username("ops")["id"]
        accounts().store.update(rid, {"enabled": False}, actor="test")
        n = prov.derives
        st, _, out = px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]},
                             {"X-Real-IP": "192.0.2.3"})
        assert (st, out) == answers[0] and prov.derives == n + 1
        # the provider itself
        assert isinstance(prov.authenticate("bob", "x"), identity_mod.Denied)
        assert isinstance(prov.authenticate(None, None), identity_mod.Denied)


# ---------------------------------------------------------------------------- 7

def test_pbkdf2_hash_verifies():
    import hashlib as hl
    saved = getattr(hl, "scrypt", None)
    try:
        if saved is not None:
            del hl.scrypt                          # a Python built without scrypt
        doc = adminpw.make_doc(PW["ops"], 0)
    finally:
        if saved is not None:
            hl.scrypt = saved
    assert doc["kdf"] == "pbkdf2-sha256" and hasattr(hl, "scrypt") == (saved is not None)
    assert adminpw.verify_doc(PW["ops"], doc) and not adminpw.verify_doc(PW["view"], doc)
    with Proxy(env=ON) as px:
        st = accounts().store
        row = st.create("ops", "operator", None, actor="test")
        cfg = px.svc.cfg
        with cfg._lock:
            cfg._conn.execute("UPDATE accounts SET pw = ? WHERE id = ?", (json.dumps(doc),
                                                                           row["id"]))
            cfg._conn.commit()
        got = accounts().provider.authenticate("ops", PW["ops"])
        assert isinstance(got, identity_mod.Identity) and got.username == "ops"
        assert px.get("/v1/settings", sign_in(px, "ops"))[0] == 200
        # an account without a password never signs in
        st.create("nopw", "viewer", None, actor="test")
        assert isinstance(accounts().provider.authenticate("nopw", ""), identity_mod.Denied)


# ---------------------------------------------------------------------------- 8

def test_no_secret_anywhere():
    answers = []
    with Proxy(env=ON) as px:
        make("own", "owner")
        own = sign_in(px, "own")
        for path in ("/v1/auth/users", "/v1/auth/session", "/v1/auth/sessions",
                     "/v1/settings/audit"):
            answers.append(px.get(path, own)[2])
        answers.append(px.post("/v1/auth/users", {"username": "view", "role": "viewer",
                                                  "password": PW["view"]}, own)[2])
        answers.append(px.post("/v1/auth/session", {"username": "view",
                                                    "password": "wrong one wrong one"})[2])
        answers.append(px.request("PUT", "/v1/auth/password", {"current": PW["own"],
                                                               "password": PW["own2"]}, own)[2])
        con = sqlite3.connect(px.svc.cfg.path)
        docs = [json.loads(r[0]) for r in con.execute("SELECT pw FROM accounts")]
        audit = "\n".join(repr(r) for r in con.execute("SELECT * FROM audit"))
        con.close()
        secrets_ = [PW["own"], PW["own2"], PW["view"], "wrong one wrong one"]
        for d in docs:
            secrets_ += [d["hash"], d["salt"]]
        logs = "\n".join(px.logs)
        files = px.files()
        blob = b"\n".join(answers)
        for s in secrets_:
            assert s not in logs and s not in audit and s.encode() not in blob, s[:4]
        for f in files:
            if os.path.basename(f).startswith("config.sqlite3"):
                continue                  # the database holds the hashes, never a password
            data = open(f, "rb").read()
            for s in secrets_:
                assert s.encode() not in data, (f, s[:4])
        for f in files:
            if "config.sqlite3" in f:
                data = open(f, "rb").read()
                for s in secrets_[:4]:
                    assert s.encode() not in data, (f, "a password")
        assert "sign_in" in audit and "create" in audit and "password" in audit


# ---------------------------------------------------------------------------- 9

def test_provider_and_legacy():
    base = identity_mod.IdentityProvider()
    assert isinstance(base.authenticate("a", "b"), identity_mod.Denied)
    assert base.from_request({}, "127.0.0.1") is None and base.health() == []
    assert not any(base.capabilities().values())
    token = "legacy-admin-token-0123456789abcdef0123"
    with Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": token}, login="on") as px:
        cap = accounts().provider.capabilities()
        assert cap["password"] and cap["manage_users"] and not cap["redirect"]
        adminpw.Password(os.path.join(px.dir, "admin.json")).set("a legacy password here")
        # the admin token as a bearer and as a password, admin.json's password, this machine
        # without a credential: nothing opens
        assert px.get("/v1/settings", {"Authorization": f"Bearer {token}"})[0] == 401
        assert px.get("/v1/settings")[0] == 401
        assert px.post("/v1/fleet/session", {"token": token})[0] == 404
        assert px.post("/v1/dashboard/session", {"token": "a legacy password here"})[0] == 404
        assert px.request("PUT", "/v1/fleet/app/password", {"password": "x" * 20})[0] == 401
        from tandemapp import auth as auth_mod
        legacy = auth_mod.Auth(token)
        value, _ = legacy.make_cookie()
        assert px.get("/v1/settings", {"Cookie": f"{auth_mod.COOKIE}={value}"})[0] == 401
        assert j(px.get("/v1/fleet/session"))[1]["authenticated"] is False
        assert px.get("/v1/dashboard/session")[0] == 401
        assert "auth: accounts" in accounts().describe()



# ---------------------------------------------------------------------------- 10

SWEEP = [("GET", p, None) for p in (
    "/v1/fleet/health", "/v1/dashboard/system", "/v1/dashboard/usage", "/v1/dashboard/requests",
    "/v1/fleet/ledger/summary", "/v1/config", "/v1/fleet/plugins", "/v1/fleet/hooks",
    "/v1/fleet/decision-providers", "/v1/fleet/plugin-trust", "/v1/fleet/engines",
    "/v1/fleet/deployments", "/v1/fleet/chat", "/v1/fleet/app/parts", "/v1/settings/app",
    "/v1/fleet/discovery", "/v1/fleet/app/export", "/v1/dashboard/logs?follow=0",
    "/v1/fleet/layouts/fleet", "/v1/settings/backends", "/v1/settings/hooks",
    "/v1/settings/conductor", "/v1/settings/engine")] + [
    ("POST", "/v1/fleet/placement", {}), ("POST", "/v1/fleet/app/parts/chat", {"on": False}),
    ("POST", "/v1/fleet/gateway/chat/completions", {"model": "m", "messages": []})]
# what the accounts change on purpose: their own routes, a route without an entry, an unknown
# session address, and the old password's writes
CHANGED = {("GET", "/v1/auth/session"), ("GET", "/v1/auth/sessions"), ("GET", "/v1/auth/users"),
           ("POST", "/v1/auth/session"), ("GET", "/v1/no-such-route"),
           ("GET", "/s/abcdefabcdef/v1/library")}


def test_old_routes_over_accounts():
    import test_auth_switch_off as off
    routes = [r for r in list(off.ROUTES) + SWEEP if (r[0], r[1]) not in CHANGED]

    def sweep(env: dict, owner: bool) -> dict:
        out = {}
        with Proxy(env=env) as px:
            h = {}
            if owner:
                make("own", "owner")
                h = sign_in(px, "own")
            for method, path, body in routes:
                try:
                    out[(method, path)] = px.request(method, path, body, h)[0]
                except Exception as exc:                           # noqa: BLE001
                    out[(method, path)] = f"dropped: {type(exc).__name__}"
        return out
    old, new = sweep({}, False), sweep(ON, True)
    diff = {k: (old[k], new[k]) for k in old if old[k] != new[k]}
    assert not diff, diff
    assert not any(isinstance(v, str) or v >= 500 and v != old[k] for k, v in new.items())
    return f"{len(routes)} routes"


if __name__ == "__main__":
    run(globals())
