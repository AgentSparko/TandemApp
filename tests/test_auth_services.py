"""SRV-869: service identities replace the loopback exemption, with TANDEM_AUTH=accounts; and the
three owner items of the same package.

  Gherkin "Service identities":
  1  chatd through the controller: own_chat mints svc:chatd at chatd's start into its environment
     only; a chat runs through /chat-engine with it; the engine never sees it; the ledger rows say
     the client kind chat-app and the user chatd; a user skill goes to the conductor
                                                                   test_chatd_through_the_controller
  2  Loopback alone opens nothing: a process on the machine calls /chat-engine/v1/models (and the
     other service routes, the page's reads) without a token: 401  test_loopback_alone_opens_nothing
  3  The CLI: `tandem-app doctor` as the state dir's user reads the full health with
     <state>/run/cli.token; a user who cannot read the file gets the open health only
                                                                   test_the_cli
  4  A collect plugin: backend b1's collect token posts for b1 (200) and not for b2 (403)
                                                                   test_a_collect_plugin
  5  The controller move: a trust token made on the giving controller; the taker starts the move
     with it; the move runs and no password or session cookie was sent
                                                                   test_the_controller_move

  Acceptance criteria:
  a  the this-machine test has two callers, local mode and the exposure rule, and neither admits a
     request outside local mode; the old guards' trusted_local is constant False with accounts on
                                                                   test_two_callers_of_this_machine
  b  the chatd suite passes with local mode off                    tests/test_chatd_via_gateway_accounts.py
  c  a service token is in no file but <state>/run/ (and an external chatd's file from
     `tandem-app tokens chatd-key`), both 0600                     test_service_tokens_in_no_other_file
  d  service identities show in All tokens and cannot be edited there; an imported one is
     reviewed there                                                test_service_identities_read_only

  The owner's items:
  1  a token never exceeds its owner's role of now, at each request: an operator's inference
     token stops when the owner becomes a viewer (viewers_may_create off) and works again with
     the switch on or after a promotion; a management token loses what the role lost and gets it
     back                                                          test_a_token_never_exceeds_the_role_of_now
  2  /plugin-backends needs the relay's in-process secret: loopback alone, a user's token or a
     wrong secret is refused; the secret goes only to this controller's own plugin backends; the
     chart blocks the path and the route list marks it internal   test_plugin_backends_need_the_relay
     and a plugin's backend end to end through the relay           test_a_plugin_backend_through_the_relay
  3  a revoked token's open stream is closed within 5 s: revoked here (at once) and in the table
     by another process (the watcher)                              test_a_revoked_tokens_stream_closes

    python tests/test_auth_services.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_auth_tokens as tt  # noqa: E402
from proxy import accounts as acc_mod  # noqa: E402
from proxy import adminpw  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import authlocal, cli, collect_api  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

ON = tt.ON
PW = tt.PW
CHAT = tt.CHAT
j, bearer, make, sign_in, new_token = tt.j, tt.bearer, tt.make, tt.sign_in, tt.new_token


def accounts():
    return proxy_app.STATE["accounts"]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def owned_proxy(px) -> None:
    """An owner with a password, bound to loopback: local mode is off."""
    make("own", "owner")
    a = accounts()
    a.local.bind = "127.0.0.1"
    a.local.exists = lambda p: False
    assert not a.local.on()


# ---------------------------------------------------------------------------- Gherkin 1

def test_chatd_through_the_controller():
    from chatd_util import Chat
    import test_chat_off_engine_host as off_host
    from proxy import appchat
    from tandemapp import chat_forward
    srv, port = fake(models=["small-1"], cache=False)
    try:
        with Proxy(env={**tt.env_for(port), **ON}, conductor=True) as px:
            px.sync()
            owned_proxy(px)
            a = accounts()
            with open(os.path.join(px.dir, "chatd.env"), "w") as f:
                f.write(f"QSE_CHAT_URL=http://127.0.0.1:{free_port()}\n")
            chat = proxy_app.own_chat(argparse.Namespace(port=px.port, host="127.0.0.1"),
                                      px.svc.cfg, {})
            try:
                assert chat.service_key is not None
                # no users row and no engine.key for the own chatd
                assert chat.engine_key() is None and chat.child_env().get(
                    "QSE_CHAT_ENGINE_KEY") is None
                seen = {}

                def popen(argv, env=None, **kw):
                    seen.update(env or {})
                    raise OSError("not started in this test")
                real = appchat.subprocess.Popen
                appchat.subprocess.Popen = popen
                try:
                    rc, why = chat.start()
                finally:
                    appchat.subprocess.Popen = real
                assert rc == 1 and "not started" in why, (rc, why)
                key = seen.get("QSE_CHAT_ENGINE_KEY")
                assert key and tokens_mod.TOKEN_RE.match(key), sorted(seen)
                rows = [r for r in a.tokens.services() if r["label"] == "svc:chatd"]
                assert len(rows) == 1 and rows[0]["scopes"] == list(tokens_mod.CHATD_SCOPES)
                assert not os.path.exists(os.path.join(chat.data, appchat.ENGINE_KEY_FILE))
                with sqlite3.connect(px.svc.cfg.path) as con:
                    assert not con.execute("SELECT 1 FROM users WHERE label = 'chatd'").fetchone()
                # each start of chatd mints a fresh one: the one before stops
                appchat.subprocess.Popen = popen
                try:
                    chat.start()
                finally:
                    appchat.subprocess.Popen = real
                key2 = seen["QSE_CHAT_ENGINE_KEY"]
                assert key2 != key
                st, out = j(px.get("/chat-engine/v1/models", bearer(key)))
                assert st == 401 and out["error"]["code"] == "invalid_api_key", out
            finally:
                chat_forward.configure()
                proxy_app.STATE["chat"] = None
            # a chat runs: chatd's calls carry its token, the engine never sees it
            c = Chat(engine_url=f"http://127.0.0.1:{px.port}/chat-engine", engine_key=key2)
            try:
                cid = c.new()
                c.send(cid, "hello there")
                snap = c.wait_idle(cid, timeout=30)
                assert snap["messages"][-1]["status"] == "done", snap["messages"][-1]
                up = tt.upstream(srv)
                assert key2 not in json.dumps(up) and "authorization" not in up, up
                rows = px.rows()
                assert rows and all(r["client_kind"] == "chat-app" and r["user"] == "chatd"
                                    for r in rows if r["endpoint"].endswith("completions")), rows
                # a user skill goes to the controller's conductor (settings:write on
                # /chat-engine/* only)
                ct, body = off_host._multipart("tea.zip", off_host._zip(
                    {"tea-timer/SKILL.md": off_host.SKILL}))
                pkg = c.ok("POST", "/packages/upload", raw=body, headers={"Content-Type": ct})
                assert pkg["id"] == "tea-timer", pkg
                st, out = j(px.get("/chat-engine/v1/library", bearer(key2)))
                assert st == 200, out
                # svc:chatd outside /chat-engine: the completions only (inference:openai)
                assert px.get("/v1/settings", bearer(key2))[0] == 403
                assert px.get("/v1/fleet/nodes", bearer(key2))[0] == 403
            finally:
                c.close()
    finally:
        srv.shutdown()
    return "minted at chatd's start into its environment; a chat and a skill through it"


# ---------------------------------------------------------------------------- Gherkin 2

def test_loopback_alone_opens_nothing():
    with Proxy(env=ON) as px:
        owned_proxy(px)
        for method, path in (("GET", "/chat-engine/v1/models"), ("GET", "/chat-engine/v1/library"),
                             ("POST", "/chat-engine/v1/chat/completions"),
                             ("GET", "/chat-engine/v1/dashboard/system"),
                             ("GET", "/plugin-backends/p/b/v1/models"),
                             ("POST", "/v1/collect/b1"), ("GET", "/metrics"),
                             ("GET", "/v1/fleet/nodes"), ("GET", "/v1/settings"),
                             ("POST", "/v1/chat/completions"), ("GET", "/v1/models")):
            st, out = j(px.request(method, path, {} if method == "POST" else None))
            assert st == 401, (method, path, st, out)
        # /health says its status only
        assert j(px.get("/health")) == (200, {"status": "ok"})
        assert j(px.get("/chat-engine/health"))[0] == 200
    return "11 routes 401 from loopback, open /health status only"


# ---------------------------------------------------------------------------- Gherkin 3

def _doctor(px) -> tuple[int, str]:
    args = argparse.Namespace(config_db=px.svc.cfg.path, host="127.0.0.1", port=px.port)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.doctor(args)
    return rc, out.getvalue()


def test_the_cli():
    srv, port = fake(models=["small-1"], cache=False)
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            owned_proxy(px)
            a = accounts()
            said = []
            a.listen("127.0.0.1", px.port, say=said.append)      # what serve() runs at start
            path = tokens_mod.cli_token_path(px.dir)
            assert oct(os.stat(path).st_mode & 0o777) == "0o600", oct(os.stat(path).st_mode)
            assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
            assert os.stat(path).st_uid == os.getuid()
            first = tokens_mod.read_cli_token(px.dir)
            assert first and not any(first in m for m in said)
            # the user who owns the state dir: the full health
            rc, out = _doctor(px)
            assert rc == 0 and "full, with" in out and '"backends"' in out, out
            assert first not in out
            # svc:cli acts as an operator would through a token, never with trust
            assert px.get("/v1/settings", bearer(first))[0] == 200
            assert px.get("/v1/fleet/nodes", bearer(first))[0] == 200
            st, out2 = j(px.post("/v1/fleet/pair", {}, bearer(first)))
            assert st == 403, (st, out2)
            assert px.get("/v1/auth/tokens", bearer(first))[0] == 403
            # minted at each start: the one before stops
            a.listen("127.0.0.1", px.port, say=said.append)
            assert px.get("/v1/settings", bearer(first))[0] == 401
            # another local user cannot read the file: the open health only
            real = tokens_mod.read_cli_token
            if os.getuid() != 0:
                os.chmod(path, 0)
            else:
                tokens_mod.read_cli_token = lambda state: None    # root reads every file
            try:
                rc, out = _doctor(px)
            finally:
                tokens_mod.read_cli_token = real
                os.chmod(path, 0o600)
            assert rc == 0 and "open: the status only" in out and '"backends"' not in out, out
            # the installer's health check reads the same file (packaging/tandemapp_install.py)
            sys.path.insert(0, os.path.join(ROOT, "packaging"))
            import tandemapp_install as inst
            ok, said_ = inst.health("127.0.0.1", px.port, None, proxy_app.STATE["version"],
                                    None, 5.0, accounts=True, state=px.dir)
            assert ok, said_
            # it read the version with the token (the fallback without one takes any `ok`)
            ok, said_ = inst.health("127.0.0.1", px.port, None, "0.0.0-not-this", None, 1.0,
                                    accounts=True, state=px.dir)
            assert not ok and str(proxy_app.STATE["version"]) in said_, said_
            assert inst._cli_token(px.dir) == tokens_mod.read_cli_token(px.dir)
    finally:
        srv.shutdown()
    return "full health with the run folder's token, open health without; 0600 in 0700"


# ---------------------------------------------------------------------------- Gherkin 4

def test_a_collect_plugin():
    import test_proxy_collect as tpc
    s1, p1 = fake(models=["m-1"], dialect="vllm")
    s2, p2 = fake(models=["m-2"], dialect="vllm")
    try:
        env = {"TANDEM_BACKENDS": "b1,b2", "TANDEM_BACKEND_B1_KIND": "vllm",
               "TANDEM_BACKEND_B1_URL": f"http://127.0.0.1:{p1}/v1",
               "TANDEM_BACKEND_B2_KIND": "vllm", "TANDEM_BACKEND_B2_URL": f"http://127.0.0.1:{p2}/v1",
               "TANDEM_LOCAL_NODE": "off", **ON}
        with Proxy(env=env) as px:
            owned_proxy(px)
            t1 = collect_api.token_for(px.svc.cfg, env, "b1")
            assert collect_api.token_for(px.svc.cfg, env, "b1", mint=False).startswith("(")
            st, out = j(tpc._collect(px, "b1", tpc._batch(), token=t1))
            assert st == 200, out
            st, out = j(tpc._collect(px, "b2", tpc._batch(), token=t1))
            assert st == 403 and "another backend" in out["error"]["message"], out
            # no token, a user's token: refused at the gate
            assert tpc._collect(px, "b1", tpc._batch(), token=None)[0] == 401
            own = sign_in(px, "own")
            admin_tok, _ = new_token(px, own, label="all", scopes=list(tokens_mod.MANAGEMENT))
            assert tpc._collect(px, "b1", tpc._batch(), token=admin_tok)[0] == 403
            # a fresh launch mints a fresh one; the one before stops
            t1b = collect_api.token_for(px.svc.cfg, env, "b1")
            assert tpc._collect(px, "b1", tpc._batch(), token=t1)[0] == 401
            assert tpc._collect(px, "b1", tpc._batch(seq0=5), token=t1b)[0] == 200
            # the switch off: the shared token as before
            assert collect_api.token_for(px.svc.cfg, {}, "b1") == collect_api.token_of(
                px.svc.cfg, {})
    finally:
        s1.shutdown()
        s2.shutdown()
    return "b1's token: b1 200, b2 403; minted per launch"


def test_imported_metrics_and_collect_tokens():
    shared = "a-shared-collect-token-of-the-release-before"
    metrics = "a-metrics-token-of-the-release-before-0001"
    with Proxy(env={**ON, "QSE_METRICS_TOKEN": metrics, "TANDEM_COLLECT_TOKEN": shared}) as px:
        owned_proxy(px)
        a = accounts()
        rows = {r["label"]: r for r in a.tokens.services()}
        assert rows["svc:metrics"]["scopes"] == ["metrics:read"] and \
            rows["svc:metrics"]["needs_review"], rows
        assert rows["svc:collect"]["scopes"] == ["collect:write"] and \
            rows["svc:collect"]["needs_review"]
        assert px.get("/metrics", bearer(metrics))[0] == 200
        assert px.get("/v1/settings", bearer(metrics))[0] == 403
        assert any(f["code"] == "tokens_need_review" for f in a.findings())
        # imported once: a second run changes nothing
        assert a.tokens.import_legacy({"QSE_METRICS_TOKEN": metrics}) == {}
    return "QSE_METRICS_TOKEN and the shared collect token as service identities under Review"


# ---------------------------------------------------------------------------- Gherkin 5

def test_the_controller_move():
    import test_app_host as ah
    a = ah.Ctl("giver", lines=["TANDEM_AUTH=accounts"])
    b = ah.Ctl("taker", lines=[f"TANDEM_ADMIN_TOKEN={ah.ADMIN_B}"])
    adminpw.Password(os.path.join(a.state, "admin.json")).set(PW["own"])
    try:
        a.start()
        b.start()
        # the giver: the owner (made from admin.json) signs in once and makes a trust token
        st, h, out = _raw(a.url, "POST", "/v1/auth/session",
                          {"username": "owner", "password": PW["own"]})
        assert st == 200, (st, out, a.tail())
        own = {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}
        st, h, out = _raw(a.url, "POST", "/v1/auth/tokens",
                          {"label": "move", "scopes": ["trust"], "days": 30}, own)
        assert st == 201, out
        trust = json.loads(out)["token"]
        sessions_before = _count(a, "sessions")
        assert os.path.exists(tokens_mod.cli_token_path(a.state)), "minted at the start"
        # the taker starts the move with the token: no password
        st, job = b.send("POST", "/v1/fleet/controller/take", {"url": a.url, "token": trust},
                         token=ah.ADMIN_B)
        assert st == 200 and job["direction"] == "pull", job
        done = ah._until(lambda: ah._job_done(b, ah.ADMIN_B, job["id"]), 90)
        assert done and done["state"] == "done", (done, b.tail(), a.tail())
        st, va = a.get("/v1/fleet/controller", token=trust)
        assert st == 200 and va["moved_to"]["api_url"] == b.url, va
        # no password and no session cookie was sent: no sign-in, no new session at the giver
        assert _count(a, "sessions") == sessions_before
        log = open(a.log, errors="replace").read()
        assert "/v1/fleet/session" not in log and "/v1/auth/session" not in log.split(
            "move", 1)[-1]
        assert trust not in log and trust not in open(b.log, errors="replace").read()
        # a token without trust does not move it
        st, h, out = _raw(a.url, "POST", "/v1/auth/tokens",
                          {"label": "fleet", "scopes": ["fleet:read", "fleet:write"]}, own)
        weak = json.loads(out)["token"]
        st, out = ah._http(a.url, "POST", "/v1/fleet/controller/handover",
                           {"certificate": "x", "name": "x", "api_url": b.url}, weak)
        assert st == 403, (st, out)
    finally:
        a.stop()
        b.stop()
    return "pulled with a trust token alone; no sign-in at the giver"


def _raw(url: str, method: str, path: str, body=None, headers=None):
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=20)
    h = {"Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
              headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, dict(r.getheaders()), out


def _count(ctl, table: str) -> int:
    con = sqlite3.connect(os.path.join(ctl.state, "config.sqlite3"))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------------------- AC a

def test_two_callers_of_this_machine():
    # the source: this_machine has exactly two callers, and nothing else of the accounts' side
    # asks the old trusted-local test
    calls = []
    for name in sorted(os.listdir(os.path.join(ROOT, "proxy"))):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, "proxy", name)).read()
        for m in re.finditer(r"\bthis_machine\(", src):
            line = src[:m.start()].rsplit("\n", 1)[-1]
            if not line.lstrip().startswith("def "):
                calls.append(name)
    assert sorted(calls) == ["authlocal.py"], calls
    for name in ("accounts.py", "authlocal.py", "tokens.py", "authz.py"):
        src = open(os.path.join(ROOT, "proxy", name)).read()
        assert ".trusted_local(" not in src, name
    # the behaviour: every old guard asks auth.trusted_local, which is False with accounts on,
    # for the cleanest loopback request there is
    with Proxy(env=ON) as px:
        a = accounts()
        assert a.trusted_local("127.0.0.1", {"Host": f"127.0.0.1:{px.port}"}) is False
        # local mode (no owner yet) lets this machine in; with an owner nothing does
        a.local.bind = "127.0.0.1"
        a.local.exists = lambda p: False
        assert a.local.on() and px.get("/v1/fleet/nodes")[0] == 200
        assert authlocal.this_machine("127.0.0.1", {"Host": "localhost"})
        assert not authlocal.this_machine("127.0.0.1", {"Host": "localhost",
                                                        "X-Forwarded-For": "192.0.2.4"})
        assert not authlocal.this_machine("192.0.2.4", {})
        make("own", "owner")
        assert not a.local.on() and px.get("/v1/fleet/nodes")[0] == 401
        # the exposure rule only narrows a valid token: it never admits one
        assert px.get("/v1/models", bearer("tdm-0000000a-" + "x" * 43))[0] == 401
    return "two callers; the old guards' trusted_local is False with accounts on"


# ---------------------------------------------------------------------------- AC c

def test_service_tokens_in_no_other_file():
    with Proxy(env=ON) as px:
        owned_proxy(px)
        a = accounts()
        cli_path = a.write_cli_token()
        cli_tok = tokens_mod.read_cli_token(px.dir)
        chatd = a.tokens.mint_service(tokens_mod.CHATD_SERVICE, tokens_mod.CHATD_SCOPES)
        coll = collect_api.token_for(px.svc.cfg, ON, "b1")
        # an external chatd's file, from the CLI
        from proxy import tokenscli
        ext = os.path.join(px.dir, "external-chatd", "engine.key")
        os.makedirs(os.path.dirname(ext), mode=0o700)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert tokenscli.main(["chatd-key", ext, "--config-db", px.svc.cfg.path]) == 0
        ext_tok = open(ext).read().strip()
        assert oct(os.stat(ext).st_mode & 0o777) == "0o600"
        assert ext_tok not in out.getvalue()
        assert px.get("/chat-engine/v1/models", bearer(chatd))[0] == 401, "the one before stops"
        assert px.get("/chat-engine/v1/models", bearer(ext_tok))[0] == 200
        px.svc.cfg._conn.execute("PRAGMA wal_checkpoint(FULL)")
        found = {}
        for path in px.files():
            try:
                data = open(path, "rb").read()
            except OSError:
                continue
            for name, tok in (("cli", cli_tok), ("chatd", chatd), ("collect", coll),
                              ("external", ext_tok)):
                if tok.encode() in data:
                    found.setdefault(name, []).append(os.path.relpath(path, px.dir))
        assert found == {"cli": [os.path.join("run", "cli.token")],
                         "external": [os.path.join("external-chatd", "engine.key")]}, found
        assert oct(os.stat(cli_path).st_mode & 0o777) == "0o600"
    return "only run/cli.token and the external chatd's file, both 0600"


# ---------------------------------------------------------------------------- AC d

def test_service_identities_read_only():
    with Proxy(env={**ON, "QSE_METRICS_TOKEN": "a-metrics-token-of-the-release-before-0002"}) \
            as px:
        owned_proxy(px)
        a = accounts()
        a.write_cli_token()
        a.tokens.mint_service(tokens_mod.CHATD_SERVICE, tokens_mod.CHATD_SCOPES)
        make("adm", "admin")
        own, adm = sign_in(px, "own"), sign_in(px, "adm")
        st, out = j(px.get("/v1/auth/tokens?all=1", adm))
        assert st == 200
        svc = {t["label"]: t for t in out["tokens"] if t["kind"] == "service"}
        assert {"svc:cli", "svc:chatd", "svc:metrics"} <= set(svc), sorted(svc)
        assert all(t["owner"] is None for t in svc.values())
        for label in ("svc:cli", "svc:chatd"):
            tid = svc[label]["id"]
            for method, path, body in (("DELETE", f"/v1/auth/tokens/{tid}", None),
                                       ("PATCH", f"/v1/auth/tokens/{tid}", {"label": "x"}),
                                       ("PATCH", f"/v1/auth/tokens/{tid}",
                                        {"needs_review": False}),
                                       ("POST", f"/v1/auth/tokens/{tid}/rotate", {})):
                for who in (own, adm):
                    st, out = j(px.request(method, path, body, who))
                    assert st in (403, 409), (label, method, path, st, out)
            assert a.tokens.get(tid)["revoked_at"] is None
        # the imported metrics token: under Review, an admin confirms or revokes it
        tid = svc["svc:metrics"]["id"]
        assert svc["svc:metrics"]["needs_review"]
        st, out = j(px.request("PATCH", f"/v1/auth/tokens/{tid}", {"needs_review": False}, adm))
        assert st == 200 and not out["needs_review"], out
        st, out = j(px.request("DELETE", f"/v1/auth/tokens/{tid}", None, adm))
        assert st == 200 and out["state"] == "revoked", out
    return "svc:cli and svc:chatd listed, never changed; svc:metrics reviewed"


# ---------------------------------------------------------------------------- item 1

def test_a_token_never_exceeds_the_role_of_now():
    srv, port = fake(models=["small-1"], cache=False)
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            make("own", "owner")
            make("ops", "operator")
            make("adm", "admin")
            own, ops, adm = sign_in(px, "own"), sign_in(px, "ops"), sign_in(px, "adm")
            a = accounts()
            inf, _ = new_token(px, ops, label="inf", scopes=["inference:openai"])
            mgmt, _ = new_token(px, ops, label="mgmt", scopes=["settings:read", "settings:write",
                                                                "fleet:read", "fleet:write"])
            trust, _ = new_token(px, adm, label="trust", scopes=["trust"], days=30)
            plan = {"changes": [{"scope": "app", "target": "controller", "set": {"chat": "off"}}]}

            def chat(tok):
                return px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                               bearer(tok))[0]

            def role(name, r):
                aid = a.store.by_username(name)["id"]
                assert px.request("PATCH", f"/v1/auth/users/{aid}", {"role": r}, own)[0] == 200

            def viewers(on):
                a.store.cfg.set_setting("api.tokens.viewers_may_create", on, who="test")

            assert chat(inf) == 200 and px.post("/v1/settings/plan", plan, bearer(mgmt))[0] == 200
            role("ops", "viewer")
            # the operator's inference token stops: viewers make no inference tokens here
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT}, bearer(inf)))
            assert st == 403 and out["error"]["code"] == "permission_denied", out
            # the management token keeps the viewer's reads, loses the writes
            assert px.post("/v1/settings/plan", plan, bearer(mgmt))[0] == 403
            assert px.get("/v1/settings", bearer(mgmt))[0] == 200
            assert px.post("/v1/fleet/nodes/n-000000000000/actions/drain", {},
                           bearer(mgmt))[0] == 403
            # an admin opens inference tokens to viewers: it works again; closed: it stops
            viewers(True)
            assert chat(inf) == 200
            viewers(False)
            assert chat(inf) == 403
            # promoted back: everything again, nothing was revoked
            role("ops", "operator")
            assert chat(inf) == 200 and px.post("/v1/settings/plan", plan, bearer(mgmt))[0] == 200
            assert all(r["revoked_at"] is None for r in a.tokens.all() if r["owner_id"])
            # trust follows the admin role
            assert px.post("/v1/fleet/controller/handover", {}, bearer(trust))[0] == 400
            role("adm", "operator")
            assert px.post("/v1/fleet/controller/handover", {}, bearer(trust))[0] == 403
            role("adm", "admin")
            assert px.post("/v1/fleet/controller/handover", {}, bearer(trust))[0] == 400
    finally:
        srv.shutdown()
    return "inference by the right to make one, management cut to the role, both ways"


# ---------------------------------------------------------------------------- item 2

def test_plugin_backends_need_the_relay():
    from proxy import config as config_mod
    with Proxy(env=ON) as px:
        owned_proxy(px)
        a = accounts()
        own = sign_in(px, "own")
        everything, _ = new_token(px, own, label="all",
                                  scopes=list(tokens_mod.INFERENCE + tokens_mod.MANAGEMENT))
        path = "/plugin-backends/p1/echo/v1/models"
        assert px.get(path)[0] == 401                                  # loopback alone
        assert px.get(path, own)[0] == 403                             # the owner's session
        assert px.get(path, bearer(everything))[0] == 403              # any user token
        assert px.get(path, bearer("not-the-relay-secret-0123456789"))[0] == 401
        # the relay's secret: in, to the plugin host (no plugin p1 runs: 503)
        st, out = j(px.get(path, bearer(a.relay_secret)))
        assert st == 503 and out["error"]["type"] == "plugin_down", (st, out)
        # from another machine (a proxy's header) the secret alone is not enough either: the
        # shim answers this machine only
        # the secret goes to this controller's own /plugin-backends/* only
        own_url = f"http://127.0.0.1:{px.port}"
        assert a.relay_key(f"{own_url}/plugin-backends/p1/echo", own_url) == a.relay_secret
        for url in (f"http://127.0.0.1:{px.port + 1}/plugin-backends/p1/echo",
                    f"http://192.0.2.5:{px.port}/plugin-backends/p1/echo",
                    f"{own_url}/v1", f"{own_url}/plugin-backendsX/p"):
            assert a.relay_key(url, own_url) is None, url
        assert a.relay_key(f"{own_url}/plugin-backends/p", None) is None
        # never stored, never in a move's export: key_for only
        cfg = px.svc.cfg
        px.svc.plugin_host.own_url = own_url
        cfg.create_backend({"id": "plugin-p1-echo", "kind": "generic",
                            "url": f"{own_url}/plugin-backends/p1/echo", "models_mode": "list",
                            "models": ["plugin-echo"], "enabled": True}, who="test")
        assert cfg.key_for("plugin-p1-echo") == a.relay_secret
        assert cfg.own_key("plugin-p1-echo") is None
        with sqlite3.connect(cfg.path) as con:
            assert not con.execute("SELECT 1 FROM backend_secrets WHERE backend_id = ?",
                                   ("plugin-p1-echo",)).fetchone()
    # switched off: as before (loopback only), and key_for sends nothing
    with Proxy() as px:
        assert px.svc.cfg.service_key is None
    # the chart blocks the path at its ingress; the route list marks it internal (block)
    vals = open(os.path.join(ROOT, "charts", "tandemapp", "values.yaml")).read()
    blocked = vals.split("blockedPaths:", 1)[1].split("\n\n", 1)[0]
    assert "- /chat-engine" in blocked and "- /plugin-backends" in blocked, blocked
    routes = json.load(open(os.path.join(ROOT, "docs", "controller-routes.json")))["routes"]
    row = next(r for r in routes if r["prefix"] == "/plugin-backends")
    assert row["owner"] == "internal" and "block" in row["what"], row
    assert config_mod.ConfigDB.service_key is None
    return "loopback, sessions and user tokens refused; the secret to its own backends only"


def test_a_plugin_backend_through_the_relay():
    import test_plugin_relay as tpr
    from plugin_host_util import kitchen
    from proxy import pluginhost
    srv, port = fake(models=["fake-a"], key=tpr.KEY, cache=False)
    src = kitchen()
    try:
        with Proxy({**tpr._env(("f1", port)), **ON}) as px:
            px.sync()
            owned_proxy(px)
            own = sign_in(px, "own")
            host = tpr._host(px)
            p = j(px.post(tpr.BASE, {"source": {"type": "folder", "path": src}}, own))
            assert p[0] == 201, p
            pid = p[1]["plugin"]["id"]
            px.svc.plugins.write(pid, who="admin", grant=["prompts:meta"])
            assert px.post(f"{tpr.BASE}/{pid}/enable", {}, own)[0] == 200
            host.start()
            tpr._running(px, pid)
            bid = pluginhost.backend_id(pid, "echo")
            px.sync()
            st, _, out = px.post("/v1/chat/completions", tpr._chat("hi there", "plugin-echo"), own)
            assert st == 200 and json.loads(out)["choices"][0]["message"]["content"] == \
                "echo: hi there", (st, out)
            st, _, out = px.post("/v1/chat/completions", tpr._chat("streamed", "plugin-echo", True),
                                 own)
            assert st == 200 and b"echo: streamed" in out, out[:300]
            # without the relay's secret the controller's own call is refused like any other
            cfg = px.svc.cfg
            keep, cfg.service_key = cfg.service_key, None
            try:
                st, _, out = px.post("/v1/chat/completions", tpr._chat("hi", "plugin-echo"), own)
                assert st >= 400, (st, out)
            finally:
                cfg.service_key = keep
            assert bid
    finally:
        srv.shutdown()
        shutil.rmtree(src)
    return "a completion and a stream through the relay with its secret; refused without"


# ---------------------------------------------------------------------------- item 3

def _open_stream(px, token: str) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", px.port), timeout=10)
    body = json.dumps({"model": "small-1", "stream": True, **CHAT}).encode()
    head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{px.port}\r\n"
            f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    s.sendall(head + body)
    got = b""
    while b"data:" not in got:
        chunk = s.recv(4096)
        assert chunk, got
        got += chunk
    assert got.startswith(b"HTTP/1.1 200"), got[:200]
    return s


def _closed_within(s: socket.socket, limit: float) -> float | None:
    t0 = time.monotonic()
    s.settimeout(0.25)
    while time.monotonic() - t0 < limit:
        try:
            if s.recv(65536) == b"":
                return time.monotonic() - t0
        except socket.timeout:
            continue
        except OSError:
            return time.monotonic() - t0
    return None


def test_a_revoked_tokens_stream_closes():
    srv, port = fake(models=["small-1"], cache=False)
    srv.fake.script = lambda body, headers: {"content": "word " * 400, "tps": 5}
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            make("own", "owner")
            make("ops", "operator")
            own, ops = sign_in(px, "own"), sign_in(px, "ops")
            tok, view = new_token(px, ops, label="s1", scopes=["inference:openai"])
            s = _open_stream(px, tok)
            assert _closed_within(s, 1.5) is None, "the stream stays open while the token lives"
            st, _ = j(px.request("DELETE", f"/v1/auth/tokens/{view['id']}", None, ops))
            assert st == 200
            took = _closed_within(s, 5.0)
            assert took is not None and took < 5.0, took
            s.close()
            # revoked in the table by another process (the CLI): within the watcher's check
            tok2, view2 = new_token(px, ops, label="s2", scopes=["inference:openai"])
            s = _open_stream(px, tok2)
            other = acc_mod.AccountStore(px.svc.cfg)
            con = sqlite3.connect(px.svc.cfg.path)
            con.execute("UPDATE api_tokens SET revoked_at = 1 WHERE id = ?", (view2["id"],))
            con.commit()
            con.close()
            took = _closed_within(s, 5.0)
            assert took is not None and took < 5.0, took
            s.close()
            # the owner disabled: the tokens are revoked and the stream ends too
            tok3, _ = new_token(px, ops, label="s3", scopes=["inference:openai"])
            s = _open_stream(px, tok3)
            aid = accounts().store.by_username("ops")["id"]
            assert px.request("PATCH", f"/v1/auth/users/{aid}", {"enabled": False}, own)[0] == 200
            took = _closed_within(s, 5.0)
            assert took is not None and took < 5.0, took
            s.close()
            assert other is not None
            # another token's stream lives on
            tok4, _ = new_token(px, own, label="s4", scopes=["inference:openai"])
            s = _open_stream(px, tok4)
            assert _closed_within(s, 2.5) is None
            s.close()
    finally:
        srv.shutdown()
    return "closed at once from here, within the check from the table"


if __name__ == "__main__":
    run(globals())
