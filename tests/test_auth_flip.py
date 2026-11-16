"""SEC-28: the accounts are the only sign-in, and a state of the release before (0.15,
`pihooks-int5`) comes through the flip and goes back to that release.

A state of the release before is written as it writes one (tests/int5_schema.py
state_like_int5: its schema, its `users` rows, its own chatd's row and engine.key, admin.json),
never by this tree's code. "The release before starts on it" runs that release's start and
sign-in rules as frozen there: its schema migration, its admin token and admin.json sign-in, its
`qse_dash` cookie and its key rule on the OpenAI routes. tests/test_auth_int5_db.py holds the
schema itself; the rollout kit repeats the same steps with the release's own code.

  Gherkin "The flip":
  1  An upgraded install with a password and keys: the old password signs in (and the old
     form's), the three keys answer with their labels, chatd's row is its service identity and
     a chat completion runs with the secret chatd gets at its start
                                                                   test_upgraded_install
  2  Nothing of the old way opens anything: a `qse_dash` cookie the release before made is 401,
     loopback with no credential is 401, the admin password is no bearer
                                                                   test_nothing_of_the_old_way
  3  Back to the release before: on the same state its start passes, the password signs in and
     the three keys answer; a request without a key is refused (never open)
                                                                   test_upgraded_install
  4  An install that was open: bound to 0.0.0.0 with no password, no token and no keys, the real
     start serves and every route but health, the page shell, sign-in and setup answers 401;
     the log says how to get the setup code, and so does the health finding; once a token
     exists the release before fails closed                       test_an_install_that_was_open

  Acceptance criteria:
  a  nothing reads the admin token but the import (proxy/authlegacy.py, proxy/tokens.py), and
     the old route kinds' class, cookie and switches are gone from the code
                                                                   test_no_reader_of_the_old_way
  b  no value of TANDEM_AUTH brings the old answers back; any value but `accounts` is a finding
     (it replaces the switch-off golden, tests/test_auth_switch_off.py)
                                                                   test_no_switch
  c  the upgrade and the rollback run twice on each shape (password and keys; open; admin token
     only)                                   test_upgraded_install, test_an_install_that_was_open,
                                                                   test_token_only_install
  d  an admin token only (what the installer wrote beyond loopback): no local mode, the token
     once as the setup code, then a management token for 30 days that never signs in; the
     release before signs in with it and with the new password    test_token_only_install
  e  the old flags and variables never stop the start           test_leftovers_never_stop

    python tests/test_auth_flip.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import int5_schema  # noqa: E402
from proxy import authz  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

OLD_PW = "the controller password of before"
NEW_PW = "the password after the upgrade"
ADMIN_TOKEN = "an-admin-token-of-forty-characters-00000"
KEYS = {"laptop": "tdm-the-laptop-key-of-before-000001",
        "phone": "tdm-the-phone-key-of-before-0000002",
        "ci": "tdm-the-ci-key-of-the-release-before3"}
CHATD_KEY = "tdm-chatd-key-of-the-release-before-0"
CHAT = {"model": "small-1", "messages": [{"role": "user", "content": "hi"}]}
FILES = ("config.sqlite3", "config.sqlite3-wal", "config.sqlite3-shm", "admin.json",
         "secrets.key")
CODE_RE = re.compile(r"Setup code: ([A-Z0-9]{20})")


def accounts():
    return proxy_app.STATE["accounts"]


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def signed(res) -> dict:
    st, h, out = res
    assert st in (200, 201), (st, out)
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def env_for(port, **more) -> dict:
    return {"TANDEM_BACKENDS": "engine", "TANDEM_BACKEND_ENGINE_KIND": "tandemllm",
            "TANDEM_BACKEND_ENGINE_URL": f"http://127.0.0.1:{port}/v1",
            "TANDEM_DEFAULT_BACKEND": "engine", "TANDEM_LOCAL_NODE": "off", **more}


class on:
    """The new build, started on the state in `folder` (copied into its own dir) as serve()
    starts it: listening on loopback, which also mints the CLI's token at each start."""

    def __init__(self, folder: str, env: dict, local: bool = True):
        self.px = Proxy(env=env, local=local)
        for name in FILES:
            p = os.path.join(folder, name)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(self.px.dir, name))
        if os.path.isdir(os.path.join(folder, "chat")):
            shutil.copytree(os.path.join(folder, "chat"), os.path.join(self.px.dir, "chat"))

    def __enter__(self) -> Proxy:
        px = self.px.__enter__()
        accounts().listen("127.0.0.1" if self.px.local else "0.0.0.0", px.port,
                          say=lambda m: None)
        return px

    def __exit__(self, *exc):
        return self.px.__exit__(*exc)


def keep(px: Proxy, folder: str) -> None:
    """The state the new build leaves, back into `folder` (for the release before)."""
    px.svc.cfg._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for name in FILES:
        p = os.path.join(px.dir, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(folder, name))


def back_to_int5(folder: str, keys: dict, *, password: str | None = None,
                 admin_token: str | None = None) -> None:
    """The release before starts on `folder`: its migration, its sign-in, its key rule."""
    c = sqlite3.connect(os.path.join(folder, "config.sqlite3"))
    try:
        assert int5_schema.migrate_like_int5(c) == int5_schema.SCHEMA_VERSION
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for label, key in keys.items():
            assert int5_schema.key_rule_like_int5(c, key) == (200, label), label
        # never open: a request without a key, or with a key nobody made, is refused
        assert int5_schema.key_rule_like_int5(c, None) == (401, None)
        assert int5_schema.key_rule_like_int5(c, "tdm-no-such-key-0000000000000") == (401, None)
    finally:
        c.close()
    if password is not None:
        assert int5_schema.sign_in_like_int5(folder, password, admin_token)
    if admin_token is not None:
        assert int5_schema.sign_in_like_int5(folder, admin_token, admin_token)


# ---------------------------------------------------------------------------- Gherkin 1 + 3

def test_upgraded_install():
    srv, port = fake(models=["small-1"], cache=False)
    folder = tempfile.mkdtemp(prefix="int5-state-")
    int5_schema.state_like_int5(folder, password=OLD_PW, keys=KEYS, chatd_key=CHATD_KEY)
    try:
        for round_ in (1, 2):
            with on(folder, env_for(port)) as px:
                px.sync()
                a = accounts()
                # the old password signs in, as the account `owner` and through the old form
                h = signed(px.post("/v1/auth/session", {"username": "owner",
                                                        "password": OLD_PW}))
                assert px.get("/v1/settings", h)[0] == 200
                st, hh, _ = px.post("/v1/fleet/session", {"token": OLD_PW})
                assert st == 204 and "tandem_session=" in hh["Set-Cookie"], (st, hh)
                # the three keys answer, each with its label upstream and in the ledger
                for label, key in KEYS.items():
                    st, _, out = px.post("/v1/chat/completions", CHAT, bearer(key))
                    assert st == 200, (round_, label, st, out[:200])
                users = {r["user"] for r in px.rows() if r.get("user")}
                assert set(KEYS) <= users, users
                # chatd's row is its service identity, not a person's key under review
                rows = {r["label"]: r for r in a.tokens.all()}
                assert "chatd" not in rows and rows[tokens_mod.CHATD_SERVICE]["kind"] == \
                    "service", sorted(rows)
                assert all(rows[k]["needs_review"] for k in KEYS), rows
                # a chat runs: chatd's secret of its start answers; its old engine.key stops
                secret = a.tokens.mint_service(tokens_mod.CHATD_SERVICE,
                                               tokens_mod.CHATD_SCOPES,
                                               memory_user=tokens_mod.CHATD_LABEL)
                st, _, out = px.post("/v1/chat/completions", CHAT, bearer(secret))
                assert st == 200, (st, out[:200])
                assert px.post("/v1/chat/completions", CHAT, bearer(CHATD_KEY))[0] == 401
                # the keys existed: the OpenAI API is open to other devices (decision 7)
                assert a.exposure.default("openai") == "network"
                keep(px, folder)
            # Gherkin 3: the release before on the same state (chatd's row keeps its key there)
            back_to_int5(folder, {**KEYS, "chatd": CHATD_KEY}, password=OLD_PW)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 2

def test_nothing_of_the_old_way():
    folder = tempfile.mkdtemp(prefix="int5-state-")
    int5_schema.state_like_int5(folder, password=OLD_PW, keys=KEYS)
    cookie = int5_schema.cookie_like_int5(folder)
    with on(folder, {}) as px:
        a = accounts()
        assert not a.local.on(), "an owner with a password: no local mode"
        for path in ("/v1/settings", "/v1/dashboard/summary", "/v1/fleet/nodes",
                     "/v1/config/backends", "/v1/auth/users", "/metrics"):
            # the release before's cookie, as its own sign-in made it
            assert px.get(path, {"Cookie": f"qse_dash={cookie}"})[0] == 401, path
            # this machine, no credential
            assert px.get(path)[0] == 401, path
            # the old password is no bearer
            assert px.get(path, bearer(OLD_PW))[0] == 401, path
        assert j(px.get("/health")) == (200, {"status": "ok"})
        # nothing of the old way is set as a cookie by the old routes either
        st, h, _ = px.post("/v1/fleet/session", {"token": OLD_PW})
        assert st == 204 and "qse_dash" not in h["Set-Cookie"]


# ---------------------------------------------------------------------------- Gherkin 4

def _serve(folder: str, host: str, extra: dict):
    """`python -m proxy serve` on `host`: (process, port, its lines)."""
    import ports
    port = ports.free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TANDEM_", "QSE_"))}
    env.update({"TANDEM_NODE_AGENT": "off", "TANDEM_LOCAL_NODE": "off", "HOME": folder,
                "PYTHONUNBUFFERED": "1", **extra})
    proc = subprocess.Popen(
        [sys.executable, "-m", "proxy", "serve", "--host", host, "--port", str(port),
         "--config-db", os.path.join(folder, "config.sqlite3"), "--usage-ledger", "off",
         "--conductor-config", "off", "--fleet-store", "off", "--chat", "off", "--no-keychain"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")),
                     daemon=True).start()
    end = time.monotonic() + 60
    while time.monotonic() < end and not any(f"{host}:{port}" in x for x in lines):
        assert proc.poll() is None, "the controller stopped at its start:\n" + "".join(lines)
        time.sleep(0.1)
    assert any(f"{host}:{port}" in x for x in lines), "the controller did not start"
    return proc, port, lines


def _ask(port: int, method: str, path: str, body=None, headers=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=None if body is None else json.dumps(body),
              headers={"Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, dict(r.getheaders()), out


def _walk_closed(port: int) -> int:
    """Every route of the table, each method, without a credential: the gate's 401, except the
    open ones (health, the page, the sign-in, the setup), which answer for themselves."""
    open_ok = 0
    for methods, how, route, perm in authz.ROUTES:
        path = route if route != "/s" else "/s/abcdefabcdef/v1/library"
        for method in methods:
            st, _, out = _ask(port, method, path, {} if method != "GET" else None)
            try:
                err = json.loads(out).get("error") or {}
                code = err.get("code")
                if tokens_mod.anthropic(path):
                    code = "sign_in_required" if err.get("type") == \
                        "authentication_error" else None
            except (ValueError, AttributeError):
                code = None
            gate = st == 401 and code == "sign_in_required"
            if perm == authz.OPEN:
                assert not gate, (method, path, st)
                open_ok += 1
            else:
                assert gate, (method, path, st, code)
    return open_ok


def test_an_install_that_was_open():
    for round_ in (1, 2):
        folder = tempfile.mkdtemp(prefix="int5-open-")
        int5_schema.state_like_int5(folder)
        proc, port, lines = _serve(folder, "0.0.0.0", {})
        try:
            # it serves; nothing but health, the page, the sign-in and the setup answers
            assert _walk_closed(port) >= 10
            assert json.loads(_ask(port, "GET", "/health")[2]) == {"status": "ok"}
            assert _ask(port, "GET", "/dashboard/")[0] != 401
            st, _, out = _ask(port, "GET", "/v1/auth/session")
            doc = json.loads(out)
            assert st == 200 and doc["setup_required"] and not doc["local_mode"], doc
            # the log says how to get the setup code
            text = "".join(lines)
            code = CODE_RE.search(text)
            assert code and "tandem-app users setup-code" in text, text[-2000:]
            # the owner from the code, then a token: the release before then fails closed
            st, h, out = _ask(port, "POST", "/v1/auth/setup",
                              {"code": code.group(1), "username": "kim", "password": NEW_PW})
            assert st == 201, out[:300]
            who = {"Cookie": h["Set-Cookie"].split(";", 1)[0],
                   "X-Tandem-CSRF": json.loads(out)["csrf"]}
            st, _, out = _ask(port, "POST", "/v1/auth/tokens",
                              {"label": "notebook", "scopes": ["inference:openai"]}, who)
            assert st == 201, out[:300]
        finally:
            proc.terminate()
            proc.wait(20)
        back_to_int5(folder, {}, password=NEW_PW)
        c = sqlite3.connect(os.path.join(folder, "config.sqlite3"))
        assert c.execute("SELECT enabled FROM users WHERE label = ?",
                         (tokens_mod.GUARD_LABEL,)).fetchone() == (1,), round_
        c.close()
    # the health page says it too (its finding's text names the command)
    from proxy import enginecodes
    with Proxy(local=False) as px:
        accounts().listen("0.0.0.0", px.port, say=lambda m: None)
        codes = {f["code"] for f in accounts().findings()}
        assert "auth_setup_pending" in codes, codes
        assert "tandem-app users setup-code" in enginecodes.CATALOG["auth_setup_pending"][2]


# ---------------------------------------------------------------------------- AC d

def test_token_only_install():
    srv, port = fake(models=["small-1"], cache=False)
    folder = tempfile.mkdtemp(prefix="int5-token-")
    int5_schema.state_like_int5(folder)
    # what the installer of before wrote beyond loopback, in app.env
    env = env_for(port, TANDEM_ADMIN_TOKEN=ADMIN_TOKEN, TANDEM_DASHBOARD_LOGIN="on")
    try:
        with on(folder, env) as px:
            a = accounts()
            assert not a.local.on(), "an admin token of before: never local mode"
            assert px.get("/v1/settings", bearer(ADMIN_TOKEN))[0] == 401
            st, out = j(px.get("/v1/auth/session"))
            assert out["setup_required"] and not out["local_mode"], out
            h = signed(px.post("/v1/auth/setup", {"code": ADMIN_TOKEN, "username": "kim",
                                                  "password": NEW_PW}))
            assert px.get("/v1/settings", h)[0] == 200
            assert px.post("/v1/auth/setup", {"code": ADMIN_TOKEN, "username": "lee",
                                              "password": NEW_PW})[0] == 404
            keep(px, folder)
        back_to_int5(folder, {}, password=NEW_PW, admin_token=ADMIN_TOKEN)
        for round_ in (1, 2):
            with on(folder, env) as px:
                a = accounts()
                # the next start: a management token of the first owner, 30 days, under review
                row = next(r for r in a.tokens.all() if r["label"] == tokens_mod.ADMIN_LABEL)
                assert set(row["scopes"]) == set(tokens_mod.ADMIN_TOKEN_SCOPES), row
                assert "trust" not in row["scopes"] and row["needs_review"]
                days = (row["expires_at"] - row["created_at"]) / 86_400_000
                assert abs(days - tokens_mod.ADMIN_TOKEN_DAYS) < 0.01, days
                assert px.get("/v1/settings", bearer(ADMIN_TOKEN))[0] == 200
                assert px.get("/v1/auth/users", bearer(ADMIN_TOKEN))[0] == 403
                # it never signs in to the page
                assert px.post("/v1/auth/session", {"username": "kim",
                                                    "password": ADMIN_TOKEN})[0] == 401
                assert px.post("/v1/fleet/session", {"token": ADMIN_TOKEN})[0] == 401
                codes = {(f["code"], f["facts"].get("variable")) for f in a.findings()}
                assert ("legacy_admin_token", None) in codes, codes
                assert ("legacy_env_set", "TANDEM_ADMIN_TOKEN") in codes, codes
                assert ("legacy_env_set", "TANDEM_DASHBOARD_LOGIN") in codes, codes
                keep(px, folder)
            back_to_int5(folder, {}, password=NEW_PW, admin_token=ADMIN_TOKEN)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC a

#: the only files that read the variables of the old sign-in: the import
IMPORT = {os.path.join("proxy", "authlegacy.py"), os.path.join("proxy", "tokens.py")}
READ = re.compile(r"""(?:get\(|\[)\s*["'](?:TANDEM_ADMIN_TOKEN|QSE_ADMIN_TOKEN|QSE_METRICS_TOKEN|"""
                  r"""TANDEM_DASHBOARD_LOGIN|QSE_DASHBOARD_LOGIN|TANDEM_AUTH)["']""")
#: the two key lists: the config's overlay parses them for the import (and the Users list of
#: Backends shows them); nothing lets a request in by them
KEY_LISTS = re.compile(r"""(?:get\(|\[)\s*["'](?:TANDEM_API_KEYS|TANDEM_API_USERS_FROM)["']""")
KEY_PARSE = IMPORT | {os.path.join("proxy", "config.py")}
GONE = re.compile(r"\b(admin_bearer|metrics_bearer|admin_any|password_set|has_password|"
                  r"make_cookie|trust_loopback|dashboard_login)\b|\bauth_mod\.(Auth|COOKIE)\b|"
                  r"(?<!authlocal)\.trusted_local\(")


def _sources():
    for top in ("proxy", "tandemapp", "chatd", "packaging"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    yield os.path.relpath(path, ROOT), open(path, encoding="utf-8").read()


def test_no_reader_of_the_old_way():
    readers, gone = [], []
    for rel, src in _sources():
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        if rel not in IMPORT and READ.search(code):
            readers.append(rel)
        if rel not in KEY_PARSE and KEY_LISTS.search(code):
            readers.append(rel)
        for m in GONE.finditer(code):
            line = code[:m.start()].count("\n") + 1
            text = code.splitlines()[line - 1]
            # chatd's contract keeps the word password_set (it means: a sign-in is needed)
            if m.group(0) == "password_set" and (rel.startswith("chatd") or '"password_set"' in
                                                 text or "password_set=" in text or
                                                 "password_set = " in text or
                                                 "X-Tandem-Password-Set" in text or
                                                 "password_set:" in text):
                continue
            if m.group(0) == "has_password" and '"has_password"' in text:
                continue                    # an account's field in /v1/auth/users
            if m.group(0) == "dashboard_login" and "dashboard-login" in text:
                continue
            gone.append(f"{rel}:{line}: {text.strip()[:100]}")
    assert readers == [], readers
    assert gone == [], gone
    # the old class and its cookie are gone; the shared pieces stay
    from tandemapp import auth as auth_mod
    assert not hasattr(auth_mod, "Auth") and not hasattr(auth_mod, "COOKIE")
    assert {"TrustedProxies", "LoginLimit", "same_origin", "same_origin_json",
            "loopback_host", "MIN_TOKEN"} <= set(dir(auth_mod))
    # `grep trusted_local`: the one this-machine test, with its two callers (SRV-869)
    callers = []
    for rel, src in _sources():
        for m in re.finditer(r"\btrusted_local\(", src):
            line = src[:m.start()].count("\n") + 1
            if not src.splitlines()[line - 1].lstrip().startswith("def "):
                callers.append(rel)
    assert sorted(callers) == ["proxy/authlocal.py", "proxy/exposure.py"], callers


# ---------------------------------------------------------------------------- AC b

def test_no_switch():
    """Each value TANDEM_AUTH had: the same accounts, the same answers; a finding unless it says
    what happens anyway."""
    folder = tempfile.mkdtemp(prefix="int5-state-")
    int5_schema.state_like_int5(folder, password=OLD_PW, keys=KEYS)
    cookie = int5_schema.cookie_like_int5(folder)
    seen = []
    for value in (None, "accounts", "legacy", "off", "no-such-mode"):
        env = {} if value is None else {"TANDEM_AUTH": value}
        with on(folder, env) as px:
            a = accounts()
            assert type(a).__name__ == "AccountsAuth"
            got = (px.get("/v1/settings")[0],
                   px.get("/v1/settings", {"Cookie": f"qse_dash={cookie}"})[0],
                   px.get("/v1/auth/session")[0], j(px.get("/health")))
            seen.append(got)
            codes = {(f["code"], f["facts"].get("variable")) for f in a.findings()}
            want = value not in (None, "accounts")
            assert (("legacy_env_set", "TANDEM_AUTH") in codes) == want, (value, codes)
    assert all(x == seen[0] for x in seen), seen
    assert seen[0][:3] == (401, 401, 200), seen[0]


# ---------------------------------------------------------------------------- local mode

def test_local_mode_and_a_made_up_key():
    """Local mode as SEC-26 built it: this machine with no credential is the owner `local`;
    a request that carries a key is that key and nothing else, so a made-up one is 401 (an
    OpenAI client on this machine sends none, or an API token)."""
    srv, port = fake(models=["small-1"], cache=False)
    try:
        with Proxy(env=env_for(port)) as px:
            px.sync()
            assert accounts().local.on()
            assert px.post("/v1/chat/completions", CHAT)[0] == 200
            st, _, out = px.post("/v1/chat/completions", CHAT, bearer("sk-anything-at-all"))
            assert st == 401 and b"the token is not known" in out, out
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC e

def test_leftovers_never_stop():
    folder = tempfile.mkdtemp(prefix="int5-left-")
    int5_schema.state_like_int5(folder)
    left = {"TANDEM_AUTH": "legacy", "TANDEM_DASHBOARD_LOGIN": "maybe",
            "QSE_ADMIN_TOKEN": "short", "TANDEM_API_KEYS": "no-colon-here",
            "QSE_METRICS_TOKEN": "x" * 8}
    proc, port, lines = _serve(folder, "127.0.0.1", {**left, "TANDEM_LOCAL_MODE": "off"})
    try:
        assert json.loads(_ask(port, "GET", "/health")[2]) == {"status": "ok"}
        text = "".join(lines)
        for var in left:
            assert f"[auth] {var} (" in text, (var, text[-1500:])
    finally:
        proc.terminate()
        proc.wait(20)
    # the old flags: accepted, whatever their value, and ignored
    from proxy import __main__ as main_mod
    for v in ("on", "off", "maybe"):
        args = main_mod.parser().parse_args(["--dashboard-login", v, "--trust-loopback", v])
        assert args.dashboard_login == v and args.trust_loopback == v


if __name__ == "__main__":
    run(globals())
