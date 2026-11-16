"""SRV-865: the owner from admin.json, the admin token as the setup code, the way back to the
release before, and the legacy variables as findings (proxy/authlegacy.py), with
TANDEM_AUTH=accounts.

  Gherkin "Migration":
  1  A password install: admin.json and no token: an owner account exists and the old password
     signs in                                                         test_password_install
  2  A token-only install: no account, the page asks for the setup code, and the admin token is
     accepted once as that code                                       test_token_only_install
  3  Going back: the owner changed the password after the upgrade; the release before starts on
     the state (its schema migration, its admin.json reader) and the new password signs in
                                                                       test_going_back
  4  A malformed variable: TANDEM_ADMIN_TOKEN of 10 characters and TANDEM_API_KEYS without a
     colon: it serves, and the health page shows legacy_env_invalid for both
                                                                       test_malformed_variables
  5  An old page: GET /v1/dashboard/session says password_set true and authenticated false, and
     its sign-in form works with the owner's password                 test_old_page

  Acceptance criteria:
  a  the migration run twice changes nothing the second time, a restart on the state included
                                                                       test_migration_twice
  b  admin.json follows the first owner's password in the old format: the page's change, the
     setup, a reset's code, `tandem-app password set`                 test_admin_json_follows
  c  each legacy variable that is set is a finding with its file; a malformed one is ignored
     (valid TANDEM_API_KEYS entries stay); an unreadable TANDEM_API_USERS_FROM
                                                                       test_variables_and_files
  d  the upgrade then the rollback, on the frozen schema of the release before (tests/
     int5_schema.py) with its admin.json: a copy of a real int5 state is the rollout kit's
                                                                       test_going_back
  --dashboard-login and --trust-loopback are accepted and ignored   test_old_flags_ignored

    python tests/test_auth_migrate.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import int5_schema  # noqa: E402
from proxy import adminpw, authlegacy  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

ON = {"TANDEM_AUTH": "accounts"}
OLD_PW = "the old controller password"
NEW_PW = "the password after the upgrade"
TOKEN = "an-admin-token-of-forty-characters-00000"


def accounts():
    return proxy_app.STATE["accounts"]


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def signed(res) -> dict:
    st, h, out = res
    assert st in (200, 201), (st, out)
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def with_admin_json(px: Proxy, password: str = OLD_PW) -> Proxy:
    """admin.json as the release before wrote it, before the new build starts."""
    adminpw.Password(os.path.join(px.dir, "admin.json")).set(password)
    return px


def copy_state(src: Proxy, dst: Proxy) -> None:
    for name in ("config.sqlite3", "config.sqlite3-wal", "config.sqlite3-shm", "admin.json",
                 "secrets.key"):
        p = os.path.join(src.dir, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dst.dir, name))


def controller_findings(px, h) -> dict:
    st, out = j(px.get("/v1/fleet/health/controller", h))
    assert st == 200, (st, out)
    return {(f["code"], f["facts"].get("variable")): f for f in out["findings"]}


# ---------------------------------------------------------------------------- Gherkin 1

def test_password_install():
    with with_admin_json(Proxy(env=ON)) as px:
        st = accounts().store
        row = st.by_username("owner")
        assert row is not None and row["role"] == "owner" and row["enabled"]
        assert st.first_owner()["id"] == row["id"]
        with open(os.path.join(px.dir, "admin.json")) as f:
            doc = json.load(f)
        assert {k: row["pw"][k] for k in ("salt", "hash", "kdf")} == \
            {k: doc[k] for k in ("salt", "hash", "kdf")}, "the same hash and parameters"
        h = signed(px.post("/v1/auth/session", {"username": "owner", "password": OLD_PW}))
        assert px.get("/v1/settings", h)[0] == 200
        assert not st.setup_required()
        assert px.post("/v1/auth/session", {"username": "owner", "password": NEW_PW})[0] == 401
        audit = [r for r in px.svc.cfg.audit(limit=500) if r.get("object") == "account"]
        assert any((r.get("detail") or {}).get("from") == "admin.json" for r in audit), audit


# ---------------------------------------------------------------------------- Gherkin 2

def test_token_only_install():
    with Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": TOKEN}) as px:
        a = accounts()
        assert a.store.all() == []
        # the page asks for the setup code: never local mode with an old admin token
        a.local.exists = lambda p: False
        a.listen("127.0.0.1", 8080, say=lambda m: None)
        st, out = j(px.get("/v1/auth/session"))
        assert (out["authenticated"], out["setup_required"], out["local_mode"]) == \
            (False, True, False)
        # the token is no bearer and no sign-in
        assert px.get("/v1/settings", {"Authorization": f"Bearer {TOKEN}"})[0] == 401
        h = signed(px.post("/v1/auth/setup", {"code": TOKEN, "username": "kim",
                                              "password": NEW_PW}))
        assert px.get("/v1/settings", h)[0] == 200
        assert a.store.first_owner()["username"] == "kim"
        assert px.post("/v1/auth/setup", {"code": TOKEN, "username": "lee",
                                          "password": NEW_PW})[0] == 404
        # once: a later start does not bring it back
        rows = px.svc.cfg._conn.execute("SELECT source, spent_at FROM setup_codes").fetchall()
        assert ("admin_token",) in [(r[0],) for r in rows] and all(r[1] for r in rows)
        authlegacy.migrate(a, px.dir)
        assert a.codes.live() == []


# ---------------------------------------------------------------------------- Gherkin 3 / AC d

def test_going_back():
    # the release before's state: its schema, its admin.json (a Proxy's dir, before it starts)
    new = Proxy(env=ON)
    c = sqlite3.connect(os.path.join(new.dir, "config.sqlite3"))
    int5_schema.migrate_like_int5(c)
    c.close()
    with_admin_json(new)
    back = Proxy(env={}, login="on")
    with new:
        h = signed(new.post("/v1/auth/session", {"username": "owner", "password": OLD_PW}))
        st, out = j(new.request("PUT", "/v1/auth/password", {"current": OLD_PW,
                                                              "password": NEW_PW}, h))
        assert st == 200, out
        copy_state(new, back)
    # back: the release before's migration runs on it, its tables are whole
    c = sqlite3.connect(os.path.join(back.dir, "config.sqlite3"))
    assert int5_schema.migrate_like_int5(c) == int5_schema.SCHEMA_VERSION
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.close()
    again = Proxy(env=ON)
    with back:
        # the old sign-in reads admin.json: the new password signs in, the old one does not
        assert back.post("/v1/dashboard/session", {"token": OLD_PW})[0] == 401
        st, h, _ = back.post("/v1/dashboard/session", {"token": NEW_PW})
        assert st == 204 and h["Set-Cookie"].startswith("qse_dash="), (st, h)
        copy_state(back, again)
    # and forward again: the same owner, the new password
    with again:
        assert len(accounts().store.all()) == 1
        signed(again.post("/v1/auth/session", {"username": "owner", "password": NEW_PW}))


# ---------------------------------------------------------------------------- Gherkin 4

def test_malformed_variables():
    env = {**ON, "TANDEM_ADMIN_TOKEN": "0123456789", "TANDEM_API_KEYS": "laptop-without-colon",
           "QSE_METRICS_TOKEN": "short"}
    with Proxy(env=env) as px:
        a = accounts()
        a.store.create("own", "owner", NEW_PW, actor="test")
        h = signed(px.post("/v1/auth/session", {"username": "own", "password": NEW_PW}))
        assert px.get("/v1/settings", h)[0] == 200
        got = controller_findings(px, h)
        for var in ("TANDEM_ADMIN_TOKEN", "TANDEM_API_KEYS", "QSE_METRICS_TOKEN"):
            f = got[("legacy_env_invalid", var)]
            assert f["severity"] == "error" and f["facts"]["file"] == "the environment", f
            assert var in f["sentence"] and "ignored" in f["sentence"]
        # ignored: a 10-character token is no setup code and guards nothing
        assert not a.local.legacy_guard
        assert a.codes.live() == []
    # the same with the switch off still stops the start, as it did (nothing changes there)
    try:
        with Proxy(env={"TANDEM_ADMIN_TOKEN": "0123456789"}):
            raise AssertionError("the old rule started with a short token")
    except SystemExit:
        pass


# ---------------------------------------------------------------------------- Gherkin 5

def test_old_page():
    with with_admin_json(Proxy(env=ON)) as px:
        st, out = j(px.get("/v1/dashboard/session"))
        assert st == 200 and out["password_set"] is True and out["authenticated"] is False, out
        assert out["login"] is True
        # its form posts {token}
        assert px.post("/v1/dashboard/session", {"token": "not the password"})[0] == 401
        st, h, _ = px.post("/v1/dashboard/session", {"token": OLD_PW})
        assert st == 204 and h["Set-Cookie"].startswith("tandem_session="), (st, h)
        cookie = {"Cookie": h["Set-Cookie"].split(";", 1)[0]}
        st, out = j(px.get("/v1/dashboard/session", cookie))
        assert st == 200 and out["authenticated"] is True
        assert px.get("/v1/dashboard/summary", cookie)[0] == 200
        # the fleet's form too, and from another origin never
        st, h, _ = px.post("/v1/fleet/session", {"token": OLD_PW})
        assert st == 204, st
        assert px.post("/v1/dashboard/session", {"token": OLD_PW},
                       {"Origin": "http://evil.example"})[0] == 403
        # an account other than the first owner never signs in through it
        accounts().store.create("ops", "operator", NEW_PW, actor="test")
        assert px.post("/v1/dashboard/session", {"token": NEW_PW})[0] == 401


# ---------------------------------------------------------------------------- AC a

def test_migration_twice():
    with with_admin_json(Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": TOKEN})) as px:
        a = accounts()

        def snapshot():
            c = px.svc.cfg._conn
            return (c.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
                    c.execute("SELECT * FROM auth_state ORDER BY key").fetchall(),
                    c.execute("SELECT * FROM setup_codes ORDER BY hash").fetchall(),
                    c.execute("SELECT COUNT(*) FROM audit").fetchone(),
                    open(os.path.join(px.dir, "admin.json")).read())
        before = snapshot()
        assert len(before[0]) == 1 and before[2] == [], "an owner: the token is no setup code"
        did = authlegacy.migrate(a, px.dir)
        assert did == {"owner": None, "token_code": False}
        assert snapshot() == before
        # a restart on the same state
        again = Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": TOKEN})
        copy_state(px, again)
    with again:
        c = again.svc.cfg._conn
        assert c.execute("SELECT * FROM accounts ORDER BY id").fetchall() == before[0]
        assert c.execute("SELECT * FROM setup_codes").fetchall() == []


# ---------------------------------------------------------------------------- AC b

def test_admin_json_follows():
    path_of = lambda px: os.path.join(px.dir, "admin.json")  # noqa: E731

    def old_format(px, password):
        with open(path_of(px)) as f:
            doc = json.load(f)
        assert set(doc) <= set(authlegacy.OLD_FIELDS) and doc["v"] == 1, doc
        assert "expires_at" not in doc and "removed" not in doc
        assert adminpw.Password(path_of(px)).verify(password)
    with with_admin_json(Proxy(env=ON)) as px:
        h = signed(px.post("/v1/auth/session", {"username": "owner", "password": OLD_PW}))
        st, out = j(px.request("PUT", "/v1/auth/password", {"current": OLD_PW,
                                                             "password": NEW_PW}, h))
        assert st == 200, out
        old_format(px, NEW_PW)
        assert not adminpw.Password(path_of(px)).verify(OLD_PW)
        # another owner's password never goes there
        a = accounts()
        two = a.store.create("two", "owner", "the second owner's password", actor="test")
        a.store.set_password(two["id"], "another long password!", actor="test")
        old_format(px, NEW_PW)
        # a reset's code, then the password set after it
        code = a.store.set_code(a.store.by_username("owner")["id"], actor="test")
        old_format(px, code)
        a.store.set_password(a.store.by_username("owner")["id"], OLD_PW, actor="test")
        old_format(px, OLD_PW)
    # the setup's owner is the first owner too
    with Proxy(env=ON) as px:
        a = accounts()
        a.local.exists = lambda p: False
        said: list[str] = []
        a.listen("0.0.0.0", 8080, say=said.append)
        code = next(s for s in said if "Setup code" in s).split("Setup code: ", 1)[1][:20]
        signed(px.post("/v1/auth/setup", {"code": code, "username": "ann", "password": NEW_PW}))
        old_format(px, NEW_PW)
    # an owner the CLI made on a new install is the first owner too: admin.json holds its code
    with Proxy(env=ON) as px:
        import contextlib
        import io
        from proxy import userscli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert userscli.main(["add", "boss", "--role", "owner", "--config-db",
                                  px.svc.cfg.path]) == 0
        code = out.getvalue().split("One-time code: ", 1)[1][:20]
        assert accounts().store.first_owner()["username"] == "boss"
        old_format(px, code)


# ---------------------------------------------------------------------------- AC c

def test_variables_and_files():
    d = tempfile.mkdtemp(prefix="legacy-")
    envf = os.path.join(d, "app.env")
    users_db = os.path.join(d, "engine.sqlite3")
    sqlite3.connect(users_db).close()
    with open(envf, "w") as f:
        f.write(f"TANDEM_ADMIN_TOKEN={TOKEN}\nexport TANDEM_DASHBOARD_LOGIN=maybe\n"
                f"TANDEM_API_KEYS=laptop:0123456789abcdef0123,broken\n")
    env = {"TANDEM_ADMIN_TOKEN": TOKEN, "TANDEM_DASHBOARD_LOGIN": "maybe",
           "TANDEM_API_KEYS": "laptop:0123456789abcdef0123,broken",
           "QSE_METRICS_TOKEN": "m" * 40, "TANDEM_API_USERS_FROM": users_db,
           "QSE_DASHBOARD_LOGIN": "on", "UNRELATED": "x"}
    files = {v: envf for v in authlegacy.env_file_vars(envf)}
    clean, found = authlegacy.scan(env, files)
    got = {f["variable"]: (f["code"], f["file"]) for f in found}
    assert got == {
        "TANDEM_ADMIN_TOKEN": ("legacy_env_set", envf),
        "QSE_METRICS_TOKEN": ("legacy_env_set", authlegacy.ENVIRONMENT),
        "TANDEM_API_KEYS": ("legacy_env_invalid", envf),
        "TANDEM_API_USERS_FROM": ("legacy_env_set", authlegacy.ENVIRONMENT),
        "TANDEM_DASHBOARD_LOGIN": ("legacy_env_invalid", envf),
        "QSE_DASHBOARD_LOGIN": ("legacy_env_set", authlegacy.ENVIRONMENT),
    }, got
    # the malformed parts are ignored; the valid key stays
    assert clean["TANDEM_API_KEYS"] == "laptop:0123456789abcdef0123"
    assert "TANDEM_DASHBOARD_LOGIN" not in clean and clean["UNRELATED"] == "x"
    # an unreadable users file
    _, found = authlegacy.scan({"TANDEM_API_USERS_FROM": os.path.join(d, "missing.sqlite3")})
    assert [f["code"] for f in found] == ["legacy_users_from_unreadable"]
    # nothing set: nothing found
    assert authlegacy.scan({"PATH": "/bin"})[1] == []
    # in the server: the start serves with every one of them, each a finding with its file
    px = Proxy(env={**ON, **env})
    with px:
        a = accounts()
        a.store.create("own", "owner", NEW_PW, actor="test")
        h = signed(px.post("/v1/auth/session", {"username": "own", "password": NEW_PW}))
        f = controller_findings(px, h)
        assert ("legacy_env_invalid", "TANDEM_API_KEYS") in f
        assert ("legacy_env_set", "TANDEM_ADMIN_TOKEN") in f
        assert f[("legacy_env_set", "TANDEM_ADMIN_TOKEN")]["severity"] == "warning"
        # the valid key still reads (the users' keys stay until the API tokens replace them)
        assert px.svc.cfg.apply_env()["users"] == [{"label": "laptop",
                                                    "key": "0123456789abcdef0123"}]
        from onboard_rig import ok as schema_ok
        for one in f.values():
            schema_ok(one, "health-finding.schema.json")
        dump = json.dumps(list(f.values()))
        assert TOKEN not in dump and "0123456789abcdef0123" not in dump
    # the switch off: the old rule, unchanged (a malformed key list still stops it)
    from proxy import config as config_mod
    try:
        with Proxy(env={"TANDEM_API_KEYS": "broken"}):
            raise AssertionError("the old rule read a broken TANDEM_API_KEYS")
    except config_mod.ConfigError:
        pass


def test_old_flags_ignored():
    from proxy import __main__ as main_mod
    args = main_mod.parser().parse_args(["--dashboard-login", "on", "--trust-loopback", "off"])
    assert args.dashboard_login == "on" and args.trust_loopback == "off"
    with with_admin_json(Proxy(env=ON, login="on")) as px:
        # the accounts decide; the old login flag opens nothing and closes nothing more
        assert px.get("/v1/dashboard/summary")[0] == 401
        assert accounts().trust_loopback is False


if __name__ == "__main__":
    run(globals())
