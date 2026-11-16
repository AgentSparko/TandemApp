"""SEC-26: the new default (local mode), the first-run setup code, the `tandem-app users` CLI and
the installer's flags (proxy/authlocal.py, proxy/accounts.py, proxy/authapi.py, proxy/userscli.py,
packaging/tandemapp_install.py), with TANDEM_AUTH=accounts.

  Gherkin "First run":
  1  Bare local use: bound to 127.0.0.1, no accounts: this machine's browser works without a
     prompt as the owner `local`                                      test_bare_local_use
  2  The same install seen through a proxy: X-Forwarded-For, X-Real-IP, Forwarded or a Host that
     is not loopback gets 401 and raises local_mode_proxy_seen on the health page
                                                                       test_proxy_seen
  3  Another site's page in the local browser: a cross-origin form post and a text/plain post to
     the settings routes are 403 and nothing changed; this origin's JSON passes
                                                                       test_other_site_in_local_browser
  4  A network-bound install: 401 on every route but /health, the page shell, the sign-in and
     the setup; the setup code makes the owner, signed in; then the setup route answers 404
                                                                       test_network_bound_install
  5  The first screen: GET /v1/auth/session says what the page shows (the setup screen, then the
     signed-in owner); the page's side is dashboard/tests/auth.test.ts  test_first_screen_answers
  6  A wrong setup code: 5 wrong codes spend it; a new one must be printed
                                                                       test_wrong_setup_code
  7  Kubernetes never trusts loopback                                  test_kubernetes_loopback
  8  Lost password: `tandem-app users reset-password owner` prints a one-time code, the old
     password stops and every session ends; the code signs in only to set a password
                                                                       test_lost_password
  The network-bound start: no owner, a setup code in the log, 401 everywhere but /health, the
  page shell and the setup and sign-in routes (every entry of the route table)
                                                                       test_network_start_closed

  Acceptance criteria:
  a  the matrix bind (loopback, other) x deploy (bare, container, kubernetes) x owner password
     (no, yes), cell by cell                                          test_matrix
  b  adding a user from local mode asks for the owner's password first (409 password_first)
                                                                       test_local_add_user_needs_password
  c  every local-mode write passes the same-origin rule (a form post and a text/plain post from
     another origin refused)                                           test_other_site_in_local_browser
  d  the setup code is in the log only while no owner exists, and the line says single use
                                                                       test_setup_code_log_line
  e  `kubectl port-forward` arrives as loopback: on the cluster it is the chart test's (OPS-298);
     here a loopback request on Kubernetes gets 401                  test_kubernetes_loopback
  f  the installer writes TANDEM_LOCAL_MODE=off for --listen beyond loopback and --behind-proxy;
     with the accounts it writes no admin token                       test_installer_flags
  the CLI: list, add (a one-time code), disable, enable, role, remove, setup-code, the last owner,
  the password alias                                                  test_users_cli
  the Secret's file on Kubernetes, and bootstrap_secret_left         test_secret_file
  the real start (`python -m proxy serve`): local mode on loopback; with TANDEM_LOCAL_MODE=off a
  setup code in the log and 401; the installer's CLI line, its setup_code() and its health check
  without a credential                                                test_real_start

    python tests/test_auth_setup.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packaging"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import accounts as acc_mod  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import authlocal, authz, userscli  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

ON = {"TANDEM_AUTH": "accounts"}
PW = "the owner's long password"
NEW_PW = "a brand new long password"
CODE_RE = re.compile(r"Setup code: ([A-Z0-9]{20}) \(single use")
SAID_RE = re.compile(r"(?:setup code|One-time code): ([A-Z0-9]{20})")


def accounts():
    return proxy_app.STATE["accounts"]


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def bare(a, bind: str = "127.0.0.1", deploy: str | None = None) -> list[str]:
    """Listen at `bind` as a bare process (or `deploy`), whatever the test machine is; the log."""
    a.local.exists = lambda p: False
    a.local.env = {"TANDEM_DEPLOY": deploy} if deploy else {}
    a.local.seen = None
    said: list[str] = []
    a.listen(bind, 8080, say=said.append)
    return said


def code_of(lines) -> str:
    for line in lines:
        m = CODE_RE.search(line) or SAID_RE.search(line)
        if m:
            return m.group(1)
    raise AssertionError("no code in the lines")


def setup(px, code, name="owner", password=PW, headers=None):
    body = {"username": name, "password": password}
    if code is not None:
        body["code"] = code
    return px.post("/v1/auth/setup", body, headers)


def signed(res) -> dict:
    st, h, out = res
    assert st in (200, 201), (st, out)
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def sign_in(px, name, password) -> dict:
    return signed(px.post("/v1/auth/session", {"username": name, "password": password}))


def cli(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = userscli.main(list(argv))
    return rc, out.getvalue()


PLAN = {"changes": [{"scope": "app", "target": "controller", "set": {"chat": "off"}}]}


# ---------------------------------------------------------------------------- Gherkin 1

def test_bare_local_use():
    with Proxy(env=ON) as px:
        said = bare(accounts())
        assert any("local mode" in s for s in said) and not any("Setup code" in s for s in said)
        st, out = j(px.get("/v1/auth/session"))
        assert st == 200 and out["authenticated"] and out["local_mode"], out
        assert out["principal"] == {"kind": "local", "name": "local"} and out["csrf"] is None
        assert "owner" in out["permissions"] and out["setup_required"]
        for path in ("/v1/settings", "/v1/fleet/nodes", "/v1/dashboard/summary", "/metrics",
                     "/v1/auth/users"):
            assert px.get(path)[0] == 200, path
        st, out = j(px.post("/v1/settings/plan", PLAN))
        assert st not in (401, 403, 415), (st, out)
        assert "backends" in j(px.get("/health"))[1]
        # the owner `local` writes the audit
        assert accounts().who_of(type("H", (), {"headers": {}, "_principal": accounts().resolve(
            {"Host": "127.0.0.1"}, "127.0.0.1")})()) == "local:local"
        # setting the password ends local mode at once, with no restart
        h = signed(setup(px, None, "own", PW))
        assert px.get("/v1/settings")[0] == 401
        assert px.get("/v1/settings", h)[0] == 200
        assert j(px.get("/v1/auth/session"))[1]["local_mode"] is False
        assert setup(px, None, "own2", PW)[0] == 404


# ---------------------------------------------------------------------------- Gherkin 2

def test_proxy_seen():
    with Proxy(env=ON) as px:
        a = accounts()
        bare(a)
        assert a.local.seen is None
        for extra in ({"X-Forwarded-For": "192.0.2.7"}, {"X-Real-IP": "192.0.2.7"},
                      {"Forwarded": "for=192.0.2.7"}, {"Host": "tandem.example"},
                      {"Host": "192.0.2.10:8080"}):
            st, h, _ = px.get("/v1/settings", extra)
            assert st == 401 and "Bearer" in h.get("WWW-Authenticate", ""), (extra, st)
        assert a.local.seen is not None
        codes = {f["code"] for f in a.findings()}
        assert {"local_mode_proxy_seen", "auth_owner_no_password"} <= codes, codes
        # on the health page, read from this machine
        st, out = j(px.get("/v1/fleet/health/controller"))
        assert st == 200, (st, out)
        got = {f["code"]: f for f in out["findings"]}
        assert got["local_mode_proxy_seen"]["severity"] == "error"
        assert "proxy" in got["local_mode_proxy_seen"]["sentence"]
        # this machine's own request still works
        assert px.get("/v1/settings")[0] == 200


# ---------------------------------------------------------------------------- Gherkin 3 / AC c

def test_other_site_in_local_browser():
    with Proxy(env=ON) as px:
        bare(accounts())
        cfg = px.svc.cfg
        before = cfg.audit(limit=500)
        settings_before = j(px.get("/v1/settings"))[1]
        evil = "http://evil.example"
        cases = [
            # a form post, as a browser sends it from another site
            ({"Origin": evil, "Sec-Fetch-Site": "cross-site",
              "Content-Type": "application/x-www-form-urlencoded"}, b"changes=1"),
            # text/plain, no preflight
            ({"Origin": evil, "Content-Type": "text/plain"}, json.dumps(PLAN).encode()),
            # JSON from another origin
            ({"Origin": evil}, PLAN),
            # a page that hides its Origin but not the site
            ({"Sec-Fetch-Site": "cross-site"}, PLAN),
        ]
        for path in ("/v1/settings/plan", "/v1/settings/apply", "/v1/fleet/layouts/fleet",
                     "/v1/auth/setup"):
            for headers, body in cases:
                method = "PUT" if "layouts" in path else "POST"
                st, out = j(px.request(method, path, body, headers))
                assert st == 403, (path, headers, st, out)
        # a WebSocket from another origin, and one with no Origin
        for origin in (evil, None):
            h = {"Upgrade": "websocket", "Connection": "Upgrade"}
            if origin:
                h["Origin"] = origin
            assert px.get("/v1/fleet/events", h)[0] == 403
        # a tool on this machine without a browser: no Origin, but JSON only
        st, out = j(px.request("POST", "/v1/settings/plan", json.dumps(PLAN).encode(),
                               {"Content-Type": "text/plain"}))
        assert st == 415 and out["error"]["code"] == "json_only", out
        assert cfg.audit(limit=500) == before, "nothing changed"
        assert j(px.get("/v1/settings"))[1] == settings_before
        st, out = j(px.post("/v1/settings/plan", PLAN, {"Origin": f"http://127.0.0.1:{px.port}"}))
        assert st not in (401, 403, 415), (st, out)


# ---------------------------------------------------------------------------- Gherkin 4

def test_network_bound_install():
    with Proxy(env=ON) as px:
        said = bare(accounts(), "0.0.0.0")
        code = code_of(said)
        for path in ("/v1/settings", "/v1/fleet/nodes", "/v1/dashboard/summary", "/metrics",
                     "/v1/models", "/v1/auth/users", "/v1/fleet/health"):
            assert px.get(path)[0] == 401, path
        assert j(px.get("/health")) == (200, {"status": "ok"})
        assert px.get("/health")[1].get("X-Tandem-Auth") == "accounts"
        assert px.get("/dashboard/")[0] != 401
        st, out = j(px.get("/v1/auth/session"))
        assert st == 200 and not out["authenticated"] and out["setup_required"]
        assert not out["local_mode"]
        h = signed(setup(px, code))
        st, out = j(px.get("/v1/auth/session", h))
        assert out["account"]["username"] == "owner" and out["account"]["role"] == "owner"
        assert px.get("/v1/settings", h)[0] == 200
        assert accounts().store.first_owner()["username"] == "owner"
        # from then on: 404, whatever the code
        assert setup(px, code, "second")[0] == 404
        assert setup(px, "AAAAABBBBBCCCCCDDDDD", "second")[0] == 404
        assert j(px.get("/v1/auth/session"))[1]["setup_required"] is False


def test_network_start_closed():
    """Every route of the table, each of its methods, without a credential."""
    with Proxy(env=ON) as px:
        said = bare(accounts(), "0.0.0.0")
        assert CODE_RE.search("\n".join(said)), "a setup code is in the log"
        open_ok = 0
        for methods, how, route, perm in authz.ROUTES:
            path = route if route != "/s" else "/s/abcdefabcdef/v1/library"
            for method in methods:
                st, h, out = px.request(method, path, {} if method != "GET" else None)
                try:
                    err = json.loads(out).get("error") or {}
                    code = err.get("code")
                    if tokens_mod.anthropic(path):
                        # the Messages API's routes refuse in its own shape
                        code = "sign_in_required" if err.get("type") == \
                            "authentication_error" else None
                except (ValueError, AttributeError):
                    code = None
                gate = st == 401 and code == "sign_in_required"
                if perm == authz.OPEN:
                    # an open route answers for itself (a wrong sign-in is its own 401)
                    assert not gate, (method, path, st)
                    open_ok += 1
                else:
                    assert gate, (method, path, st, code)
        assert open_ok >= 10
        # the open ones: the status, the shell, the sign-in, the setup
        assert j(px.get("/health"))[1] == {"status": "ok"}
        assert px.get("/")[0] == 302
        assert j(px.post("/v1/auth/session", {"username": "x", "password": "y" * 12}))[0] == 401
        assert j(setup(px, "wrong-code"))[0] == 401


# ---------------------------------------------------------------------------- Gherkin 5

def test_first_screen_answers():
    with Proxy(env=ON) as px:
        code = code_of(bare(accounts(), "0.0.0.0"))
        st, out = j(px.get("/v1/auth/session"))
        # the page: not signed in and setup required -> the setup screen
        assert (out["authenticated"], out["setup_required"], out["local_mode"]) == \
            (False, True, False)
        assert px.get("/health")[1].get("X-Tandem-Auth") == "accounts"
        h = signed(setup(px, code))
        st, out = j(px.get("/v1/auth/session", h))
        assert out["authenticated"] and out["csrf"] == h["X-Tandem-CSRF"] and \
            not out["setup_required"]
        # signed out: the sign-in screen (no setup any more)
        assert px.request("DELETE", "/v1/auth/session", None, h)[0] == 204
        st, out = j(px.get("/v1/auth/session"))
        assert (out["authenticated"], out["setup_required"]) == (False, False)


# ---------------------------------------------------------------------------- Gherkin 6

def test_wrong_setup_code():
    with Proxy(env=ON) as px:
        a = accounts()
        code = code_of(bare(a, "0.0.0.0"))
        lefts = []
        for i in range(authlocal.TRIES):
            st, out = j(setup(px, "WRONGWRONGWRONGWRONG", headers={"X-Real-IP": f"192.0.2.{i}"}))
            assert st == 401 and out["error"]["code"] == "wrong_setup_code", out
            lefts.append(out["error"]["tries_left"])
        assert lefts == [4, 3, 2, 1, 0], lefts
        st, out = j(setup(px, code, headers={"X-Real-IP": "192.0.2.99"}))
        assert st == 410 and out["error"]["code"] == "setup_code_spent", out
        assert a.store.all() == []
        # a new one must be printed (the CLI, or the next start)
        rc, said = cli("setup-code", "--config-db", px.svc.cfg.path)
        assert rc == 0 and "single use" in said
        fresh = code_of(said.splitlines())
        assert fresh != code
        signed(setup(px, fresh.lower()[:10] + "-" + fresh.lower()[10:],
                     headers={"X-Real-IP": "192.0.2.100"}))
        # each code in the table is a hash, never the code
        with px.svc.cfg._lock:
            rows = px.svc.cfg._conn.execute("SELECT hash FROM setup_codes").fetchall()
        assert rows and all(len(r[0]) == 64 and code not in r[0] for r in rows)
        dump = "\n".join(str(r) for r in px.svc.cfg._conn.iterdump())
        assert code not in dump and fresh not in dump


# ---------------------------------------------------------------------------- Gherkin 7 / AC e

def test_kubernetes_loopback():
    for env in ({"TANDEM_DEPLOY": "kubernetes"}, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
        with Proxy(env={**ON, **env}) as px:
            a = accounts()
            a.local.exists = lambda p: False
            said: list[str] = []
            a.listen("127.0.0.1", 8080, say=said.append)
            assert a.local.why_not() == "runs on Kubernetes"
            assert a.store.all() == []
            for path in ("/v1/settings", "/v1/dashboard/summary", "/v1/fleet/nodes"):
                assert px.get(path)[0] == 401, (env, path)
            assert j(px.get("/v1/auth/session"))[1]["local_mode"] is False
            assert CODE_RE.search("\n".join(said))
            assert "auth_setup_pending" in {f["code"] for f in a.findings()}


# ---------------------------------------------------------------------------- Gherkin 8

def test_lost_password():
    with Proxy(env=ON) as px:
        code = code_of(bare(accounts(), "0.0.0.0"))
        own = signed(setup(px, code))
        other = sign_in(px, "owner", PW)
        rc, said = cli("reset-password", "owner", "--config-db", px.svc.cfg.path)
        assert rc == 0 and "every session ended" in said
        once = code_of(said.splitlines())
        # the old password stops at once, every session ends
        assert px.post("/v1/auth/session", {"username": "owner", "password": PW})[0] == 401
        assert px.get("/v1/settings", own)[0] == 401 and px.get("/v1/settings", other)[0] == 401
        # the code signs in, and only to set a password
        h = sign_in(px, "owner", once)
        st, out = j(px.get("/v1/auth/session", h))
        assert out["account"]["must_change"] is True
        st, out = j(px.get("/v1/settings", h))
        assert st == 403 and out["error"]["code"] == "password_change_required", out
        st, out = j(px.request("PUT", "/v1/auth/password", {"current": once, "password": NEW_PW},
                               h))
        assert st == 200, out
        h2 = {"Cookie": h["Cookie"], "X-Tandem-CSRF": out["csrf"]}
        st, rh, _ = px.request("PUT", "/v1/auth/password", {"current": once, "password": NEW_PW},
                               h)
        assert st == 401
        h = sign_in(px, "owner", NEW_PW)
        assert px.get("/v1/settings", h)[0] == 200
        assert px.post("/v1/auth/session", {"username": "owner", "password": once})[0] == 401
        del h2
        # a code ends after its day
        rc, said = cli("reset-password", "owner", "--config-db", px.svc.cfg.path)
        once = code_of(said.splitlines())
        a = accounts()
        real = a.provider.clock
        a.provider.clock = lambda: real() + 86_400 + 5
        try:
            assert px.post("/v1/auth/session", {"username": "owner", "password": once})[0] == 401
        finally:
            a.provider.clock = real
        assert px.post("/v1/auth/session", {"username": "owner", "password": once})[0] == 200


# ---------------------------------------------------------------------------- AC a

def test_matrix():
    cells = {}
    for with_pw in (False, True):
        with Proxy(env=ON) as px:
            a = accounts()
            h = None
            if with_pw:
                a.store.create("own", "owner", PW, actor="test")
                h = sign_in(px, "own", PW)
            for bind in ("127.0.0.1", "0.0.0.0"):
                for deploy in ("bare", "container", "kubernetes"):
                    bare(a, bind, None if deploy == "bare" else deploy)
                    anon = px.get("/v1/settings")[0]
                    local = j(px.get("/v1/auth/session"))[1]["local_mode"]
                    cells[(bind, deploy, with_pw)] = (anon, local)
                    if h is not None:
                        assert px.get("/v1/settings", h)[0] == 200
    want = {k: ((200, True) if k == ("127.0.0.1", "bare", False) else (401, False))
            for k in cells}
    assert len(cells) == 12 and cells == want, cells
    # the other ways local mode is off
    with Proxy(env=ON) as px:
        a = accounts()
        bare(a)
        assert a.local.on()
        px.svc.cfg.set_setting("auth.local_mode", "off", who="test")
        assert a.local.why_not() == "auth.local_mode is off" and px.get("/v1/settings")[0] == 401
        px.svc.cfg.set_setting("auth.local_mode", "auto", who="test")
        a.local.env = {authlocal.LOCAL_ENV: "off"}
        assert px.get("/v1/settings")[0] == 401
        a.local.env = {}
        a.local.exists = lambda p: p == "/.dockerenv"
        assert a.local.why_not() == "runs in a container"
        a.local.exists = lambda p: False
        assert px.get("/v1/settings")[0] == 200
    with Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": "x" * 40}) as px:
        a = accounts()
        a.local.exists = lambda p: False
        a.listen("127.0.0.1", 8080, say=lambda m: None)
        assert a.local.why_not() == "an admin token is set" and px.get("/v1/settings")[0] == 401
    for bind, loop in (("127.0.0.1", True), ("127.1.2.3", True), ("::1", True),
                       ("localhost", True), ("[::1]", True), ("0.0.0.0", False), ("::", False),
                       ("192.0.2.5", False), ("", False)):
        assert authlocal.loopback_bind(bind) is loop, bind


# ---------------------------------------------------------------------------- AC b

def test_local_add_user_needs_password():
    with Proxy(env=ON) as px:
        bare(accounts())
        st, out = j(px.post("/v1/auth/users", {"username": "ann", "role": "viewer",
                                               "password": NEW_PW}))
        assert st == 409 and out["error"]["code"] == "password_first", out
        assert accounts().store.all() == []
        p = accounts().resolve({"Host": "127.0.0.1"}, "127.0.0.1")
        assert authlocal.needs_password(p) and not authlocal.needs_password(None)
        h = signed(setup(px, None, "own", PW))
        st, out = j(px.post("/v1/auth/users", {"username": "ann", "role": "viewer",
                                               "password": NEW_PW}, h))
        assert st == 201, out


# ---------------------------------------------------------------------------- AC d

def test_setup_code_log_line():
    with Proxy(env=ON) as px:
        a = accounts()
        said = bare(a, "0.0.0.0")
        line = next(s for s in said if "Setup code" in s)
        assert "single use" in line and "only the first-run setup" in line
        assert "tandem-app users setup-code" in line
        first = code_of(said)
        # a start's code ends the one of the start before
        second = code_of(bare(a, "0.0.0.0"))
        assert first != second
        assert j(setup(px, first))[0] == 401
        signed(setup(px, second))
        # an owner exists: no code in the log any more
        said = bare(a, "0.0.0.0")
        assert not any("Setup code" in s or "setup code" in s for s in said), said
        assert a.codes.live() == []
        # the CLI refuses too
        rc, out = cli("setup-code", "--config-db", px.svc.cfg.path)
        assert rc == 1 and "owner exists" in out


# ---------------------------------------------------------------------------- AC f

def _install_args(**kw):
    a = argparse.Namespace(listen=None, port=None, chat=None, behind_proxy=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_installer_flags():
    import tandemapp_install as inst
    old = os.environ.pop("TANDEM_AUTH", None)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            # --listen beyond loopback, the old sign-in: the token as before, and local mode off
            d = tempfile.mkdtemp(prefix="inst-")
            st = inst.ensure_state(d, _install_args(listen="0.0.0.0"), False)
            env = inst.read_env(os.path.join(d, "app.env"))
            assert env["TANDEM_LOCAL_MODE"] == "off" and len(env["TANDEM_ADMIN_TOKEN"]) >= 32
            assert st["lan"] and not st["accounts"]
            # --behind-proxy on loopback: local mode off, no token
            d = tempfile.mkdtemp(prefix="inst-")
            inst.ensure_state(d, _install_args(behind_proxy=True), False)
            env = inst.read_env(os.path.join(d, "app.env"))
            assert env["TANDEM_LOCAL_MODE"] == "off" and "TANDEM_ADMIN_TOKEN" not in env
            # plain loopback: neither
            d = tempfile.mkdtemp(prefix="inst-")
            inst.ensure_state(d, _install_args(), False)
            env = inst.read_env(os.path.join(d, "app.env"))
            assert "TANDEM_LOCAL_MODE" not in env and "TANDEM_ADMIN_TOKEN" not in env
            # the accounts: no token, the switch kept in app.env, local mode off beyond loopback
            os.environ["TANDEM_AUTH"] = "accounts"
            d = tempfile.mkdtemp(prefix="inst-")
            st = inst.ensure_state(d, _install_args(listen="0.0.0.0"), False)
            env = inst.read_env(os.path.join(d, "app.env"))
            assert "TANDEM_ADMIN_TOKEN" not in env and env["TANDEM_AUTH"] == "accounts"
            assert env["TANDEM_LOCAL_MODE"] == "off" and st["accounts"]
            # a dry run says so and writes nothing
            d = tempfile.mkdtemp(prefix="inst-")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                inst.ensure_state(d, _install_args(listen="0.0.0.0"), True)
            assert "TANDEM_LOCAL_MODE=off" in out.getvalue() and \
                "admin token" not in out.getvalue()
            assert not os.path.exists(os.path.join(d, "app.env"))
        assert "--behind-proxy" in inst.parser().format_help()
        # a setup code from the release's own CLI (the summary prints it)
        assert inst.accounts_on({"TANDEM_AUTH": "accounts"})
    finally:
        os.environ.pop("TANDEM_AUTH", None)
        if old is not None:
            os.environ["TANDEM_AUTH"] = old
    # the image never gets local mode
    with open(os.path.join(ROOT, "packaging", "Dockerfile.app")) as f:
        assert "TANDEM_DEPLOY=container" in f.read()


# ---------------------------------------------------------------------------- the CLI

def test_users_cli():
    with Proxy(env=ON) as px:
        db = px.svc.cfg.path
        code = code_of(bare(accounts(), "0.0.0.0"))
        signed(setup(px, code))
        rc, out = cli("add", "ann", "--role", "operator", "--config-db", db)
        assert rc == 0 and "One-time code" in out
        once = code_of(out.splitlines())
        h = sign_in(px, "ann", once)
        assert j(px.get("/v1/auth/session", h))[1]["account"]["must_change"]
        rc, out = cli("list", "--config-db", db)
        assert rc == 0 and re.search(r"ann\s+operator\s+enabled\s+one-time code", out), out
        assert once not in out
        rc, out = cli("role", "ann", "viewer", "--config-db", db)
        assert rc == 0 and "sessions ended" in out
        assert accounts().store.by_username("ann")["role"] == "viewer"
        assert px.get("/v1/auth/session", h)[0] == 200 and \
            not j(px.get("/v1/auth/session", h))[1]["authenticated"]
        rc, out = cli("disable", "ann", "--config-db", db)
        assert rc == 0 and not accounts().store.by_username("ann")["enabled"]
        assert px.post("/v1/auth/session", {"username": "ann", "password": once})[0] == 401
        rc, out = cli("enable", "ann", "--config-db", db)
        assert rc == 0 and accounts().store.by_username("ann")["enabled"]
        rc, out = cli("remove", "ann", "--config-db", db)
        assert rc == 0 and accounts().store.by_username("ann") is None
        # the last owner, a role that is none, a name that is none
        assert cli("disable", "owner", "--config-db", db)[0] == 1
        assert cli("role", "owner", "admin", "--config-db", db)[0] == 1
        assert cli("remove", "owner", "--config-db", db)[0] == 1
        assert cli("role", "owner", "boss", "--config-db", db)[0] == 2
        assert cli("disable", "nobody", "--config-db", db)[0] == 1
        assert cli("add", "bob", "--role", "chief", "--config-db", db)[0] == 2
        # the password alias: the first owner's, admin.json follows
        import getpass
        real = getpass.getpass
        getpass.getpass = lambda prompt="": NEW_PW
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = userscli.password_main(["set", "--state-dir", os.path.dirname(db)])
        finally:
            getpass.getpass = real
        assert rc == 0 and "owner: password set" in out.getvalue(), out.getvalue()
        sign_in(px, "owner", NEW_PW)
        from proxy import adminpw
        assert adminpw.Password(os.path.join(os.path.dirname(db), "admin.json")).verify(NEW_PW)
        # no accounts: the controller's admin password as before
        d = tempfile.mkdtemp(prefix="pw-")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = userscli.password_main(["status", "--state-dir", d])
        assert rc == 0 and "admin password: not set" in out.getvalue()
    # no database: a word, not a traceback
    rc, out = cli("list", "--config-db", os.path.join(tempfile.mkdtemp(), "none.sqlite3"))
    assert rc == 1 and "no config database" in out


# ---------------------------------------------------------------------------- the real start

def _serve(state: str, extra: dict):
    """`python -m proxy serve` on 127.0.0.1 with the accounts on: (process, port, its lines)."""
    import subprocess
    import threading
    import time
    import ports
    port = ports.free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TANDEM_", "QSE_"))}
    env.update({"TANDEM_AUTH": "accounts", "TANDEM_NODE_AGENT": "off", "TANDEM_LOCAL_NODE": "off",
                "HOME": state, "PYTHONUNBUFFERED": "1", **extra})
    proc = subprocess.Popen(
        [sys.executable, "-m", "proxy", "serve", "--host", "127.0.0.1", "--port", str(port),
         "--config-db", os.path.join(state, "config.sqlite3"), "--usage-ledger", "off",
         "--conductor-config", "off", "--fleet-store", "off", "--chat", "off", "--no-keychain"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")),
                     daemon=True).start()
    end = time.monotonic() + 60
    while time.monotonic() < end and not any(f"127.0.0.1:{port}" in x for x in lines):
        assert proc.poll() is None, "the controller stopped at its start"
        time.sleep(0.1)
    assert any(f"127.0.0.1:{port}" in x for x in lines), "the controller did not start"
    return proc, port, lines


def _ask(port: int, method: str, path: str, body=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=None if body is None else json.dumps(body),
              headers={"Content-Type": "application/json"})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def test_real_start():
    """serve() hands the bound address to the accounts: local mode on loopback; with
    TANDEM_LOCAL_MODE=off a setup code in the log, 401, the installer's own CLI line and its
    health check without a credential."""
    import subprocess
    import tandemapp_install as inst
    state = tempfile.mkdtemp(prefix="serve-")
    proc, port, lines = _serve(state, {})
    try:
        assert any("[auth] local mode" in x for x in lines)
        assert not any(CODE_RE.search(x) for x in lines)
        assert _ask(port, "GET", "/v1/settings")[0] == 200
    finally:
        proc.terminate()
        proc.wait(20)
    state = tempfile.mkdtemp(prefix="serve-")
    proc, port, lines = _serve(state, {authlocal.LOCAL_ENV: "off"})
    try:
        assert any(CODE_RE.search(x) for x in lines), "a setup code in the log"
        assert _ask(port, "GET", "/v1/settings")[0] == 401
        db = os.path.join(state, "config.sqlite3")
        # the command the summary names, as the machine's owner runs it
        r = subprocess.run([sys.executable, "-m", "proxy", "users", "setup-code", "--config-db",
                            db], capture_output=True, text=True, timeout=60, cwd=ROOT)
        assert r.returncode == 0, r.stderr[-500:]
        code = code_of(r.stdout.splitlines())
        # and the installer's own function over a release laid out as it lays one out
        rel = tempfile.mkdtemp(prefix="rel-")
        os.makedirs(os.path.join(rel, "venv", "bin"))
        py = os.path.join(rel, "venv", "bin", "python")
        with open(py, "w") as f:
            f.write(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
        os.chmod(py, 0o755)
        for pkg in ("proxy", "tandemapp"):
            os.symlink(os.path.join(ROOT, pkg), os.path.join(rel, pkg))
        other = inst.setup_code(rel, state)
        assert other and other != code and len(other) == 20
        # its health check: no version without a credential, the status counts
        ok, said = inst.health("127.0.0.1", port, None, "any version", None, 10, accounts=True)
        assert ok, said
        ok, _ = inst.health("127.0.0.1", port, None, "any version", None, 1, accounts=False)
        assert not ok
        st, out = _ask(port, "POST", "/v1/auth/setup", {"code": code, "username": "kim",
                                                        "password": PW})
        assert st == 201, out[:200]
        assert _ask(port, "POST", "/v1/auth/setup", {"code": other, "username": "lee",
                                                     "password": PW})[0] == 404
        assert inst.setup_code(rel, state) is None, "an owner exists: no code"
    finally:
        proc.terminate()
        proc.wait(20)


# ---------------------------------------------------------------------------- the Secret's file

def test_secret_file():
    d = tempfile.mkdtemp(prefix="secret-")
    path = os.path.join(d, "code")
    with open(path, "w") as f:
        f.write("k8s-made-setup-code-0123456789\n")
    with Proxy(env={**ON, "TANDEM_DEPLOY": "kubernetes", authlocal.SECRET_ENV: path}) as px:
        a = accounts()
        a.local.exists = lambda p: False
        said: list[str] = []
        a.listen("0.0.0.0", 8080, say=said.append)
        assert any(path in s for s in said) and not CODE_RE.search("\n".join(said))
        h = signed(setup(px, "k8s-made-setup-code-0123456789"))
        assert px.get("/v1/settings", h)[0] == 200
        assert setup(px, "k8s-made-setup-code-0123456789", "again")[0] == 404
        assert "bootstrap_secret_left" in {f["code"] for f in a.findings()}
        os.remove(path)
        assert "bootstrap_secret_left" not in {f["code"] for f in a.findings()}


if __name__ == "__main__":
    run(globals())
