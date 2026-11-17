"""SRV-867: API tokens with scopes, models and expiry (proxy/tokens.py, proxy/tokenapi.py,
proxy/tokenscli.py), with TANDEM_AUTH=accounts.

The backend is the fake as our engine (kind tandemllm, the adapter that passes a client's headers
on), with the models small-1 and big-1; the controller is the standalone proxy in this process.

  Gherkin "API tokens":
  1  Create and use: a signed-in operator makes a token (inference:openai, "small-*"); the answer
     shows it once; a completion for small-1 answers 200; the ledger row names the token and the
     user; the engine gets the owner's username as the memory owner   test_create_and_use
  2  Outside the token's models or scopes: big-1 is 403 naming the model, /v1/settings 403 naming
     the scope (in Authorization and in x-api-key)                     test_outside_models_and_scopes
  3  Expired and revoked: both 401, the message says which             test_expired_and_revoked
  4  An imported key: a users row and a TANDEM_API_KEYS key answer as before, with the same user
     in the ledger and the same memory owner upstream                  test_an_imported_key
  5  The admin token for scripts: a settings read 200, pairing 403, a sign-in with it 401, under
     Review with 30 days, finding legacy_admin_token until revoked or expired
                                                                       test_the_admin_token_for_scripts
  6  Going back after a revocation: the release before's own key rule (tests/int5_schema.py) on
     the migrated state: the other keys work, the revoked one is refused
                                                                       test_going_back_after_a_revocation
  7  The owner loses rights: an operator's settings:write token after the operator became a
     viewer: 403                                                       test_the_owner_loses_rights
  8  Nothing leaks upstream: either header, with and without the backend's own key
                                                                       test_nothing_leaks_upstream
  9  A viewer and inference tokens: 403 saying an admin must allow it; allowed, the viewer makes
     one that still never exceeds a viewer; an operator always could  test_a_viewer_and_inference_tokens
  10 The rollback guard: the first token with an empty users table writes rollback-guard; the
     release before then answers 401 without a key; revoking every imported key keeps it closed
                                                                       test_the_rollback_guard

  Acceptance criteria:
  a  the database holds only the hash, the id and 4 characters      test_only_the_hash_is_stored
  b  a second read of a token's row never returns the token          test_create_and_use
  c  rotate keeps id, label and usage; the old secret stops after the grace time  test_rotate
  d  last_used_at is written at most once a minute per token          test_last_used_once_a_minute
  e  1,000 tokens: the check adds under 1 ms to a request (measured)  test_a_thousand_tokens
  f  the import run twice changes nothing; no legacy variable or users row is read for auth after
     it; the row chatd becomes svc:chatd, not a key under Review     test_the_import_twice
  g  the imported admin token's exact scopes, no trust, 30 days, needs_review, never a sign-in,
     the finding until revoked or expired                           test_the_admin_token_for_scripts
  h  a disabled or removed account's tokens are revoked, through the routes and the CLI
                                                                       test_disable_and_remove_revoke
  i  the tokens CLI: create, list, rotate, revoke, confirm           test_the_tokens_cli
  j  the ledger at schema 13 is additive: the release before opens it and writes its rows
                                                                       test_the_ledger_13
  k  a token never manages tokens, sessions or users; local mode makes none; the routes are
     no-store                                                          test_what_a_token_never_does
  l  each scope opens its routes and no other (usage:read: the ledger's views only)
                                                                       test_each_scope_opens_its_routes

    python tests/test_auth_tokens.py
"""

from __future__ import annotations

import io
import contextlib
import hashlib
import functools
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import int5_schema  # noqa: E402
from proxy import adminpw, authlegacy  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy as _Proxy, fake, run  # noqa: E402

# SEC-28: these tests run as before the flip made local mode the default of a bare
# process: a controller that is not listening yet, so this machine is trusted for nothing
Proxy = functools.partial(_Proxy, local=False)

ON = {"TANDEM_AUTH": "accounts"}
PW = {n: f"the password of {n} here" for n in ("own", "adm", "ops", "view")}
ADMIN_TOKEN = "an-admin-token-of-forty-characters-00000"
KEY_DB = "tdm-a-key-made-before-the-upgrade-0001"
KEY_ENV = "tdm-an-env-key-made-before-the-upgrade"
CHAT = {"messages": [{"role": "user", "content": "hi"}]}


def accounts():
    return proxy_app.STATE["accounts"]


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def engine():
    srv, port = fake(models=["small-1", "big-1"], cache=False)
    return srv, port


def env_for(port, **more) -> dict:
    return {"TANDEM_BACKENDS": "engine", "TANDEM_BACKEND_ENGINE_KIND": "tandemllm",
            "TANDEM_BACKEND_ENGINE_URL": f"http://127.0.0.1:{port}/v1",
            "TANDEM_DEFAULT_BACKEND": "engine", "TANDEM_LOCAL_NODE": "off", **more}


def make(name: str, role: str) -> dict:
    return accounts().store.create(name, role, PW[name], actor="test")


def sign_in(px, name: str) -> dict:
    st, h, out = px.post("/v1/auth/session", {"username": name, "password": PW[name]})
    assert st == 200, (st, out)
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def new_token(px, who: dict, **body) -> tuple[str, dict]:
    st, h, out = px.post("/v1/auth/tokens", body, who)
    doc = json.loads(out)
    assert st == 201, (st, doc)
    assert h.get("Cache-Control") == "no-store", h
    return doc.pop("token"), doc


def upstream(srv) -> dict:
    return {k.lower(): v for k, v in srv.fake.last["headers"].items()}


def copy_state(src: Proxy, dst: Proxy) -> None:
    for name in ("config.sqlite3", "config.sqlite3-wal", "config.sqlite3-shm", "admin.json",
                 "secrets.key"):
        p = os.path.join(src.dir, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dst.dir, name))


def controller_findings(px, h) -> dict:
    st, out = j(px.get("/v1/fleet/health/controller", h))
    assert st == 200, (st, out)
    return {f["code"]: f for f in out["findings"]}


def findings_now() -> dict:
    """The sign-in's findings as the health page builds them, without its cache."""
    return {f["code"]: f for f in accounts().findings()}


def shift(days: float) -> None:
    """The accounts' clock (tokens, sessions) `days` ahead of now."""
    base = time.time()
    accounts().store.clock = lambda: base + days * 86400


class Legacy:
    """SEC-28: the state of the release before, as that release writes it (tests/int5_schema.py
    state_like_int5): a key `phone` in `users`; its TANDEM_API_KEYS (`laptop`) is the new
    build's env. `svc.cfg._conn` writes more rows as it did; copy_state takes `dir`."""

    def __init__(self):
        import types
        self.dir = tempfile.mkdtemp(prefix="int5-state-")
        int5_schema.state_like_int5(self.dir, keys={"phone": KEY_DB})
        conn = sqlite3.connect(os.path.join(self.dir, "config.sqlite3"))
        self.svc = types.SimpleNamespace(cfg=types.SimpleNamespace(_conn=conn))

    def __exit__(self, *exc):
        self.svc.cfg._conn.close()


def legacy_state(port, **env) -> Legacy:
    """The release before's state, its keys in `users` and TANDEM_API_KEYS (the switch-off
    controller that stood for it is gone with the switch)."""
    return Legacy()


# ---------------------------------------------------------------------------- Gherkin 1 + AC b

def test_create_and_use():
    srv, port = engine()
    try:
        with Proxy(env={**ON, **env_for(port)}) as px:
            px.sync()
            make("own", "owner")
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, view = new_token(px, ops, label="laptop", scopes=["inference:openai"],
                                    models=["small-*"])
            assert tokens_mod.TOKEN_RE.match(token) and view["id"] == token[4:12], view
            assert view["owner"] == "ops" and view["state"] == "active" and \
                view["expires_at"] is not None, view
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer(token)))
            assert st == 200, (st, out)
            # whose memory: the owner's username (decision 5)
            got = upstream(srv)
            assert got["x-tandem-user"] == "ops", got
            row = px.rows()[-1]
            assert row["token_id"] == view["id"] and row["user"] == "ops", row
            assert row["api"] == "openai" and row["status"] == 200, row
            assert row["client_id"] == "k:" + hashlib.sha256(token.encode()).hexdigest()[:12]
            # AC b: no later read holds the token
            for path in ("/v1/auth/tokens", f"/v1/auth/tokens/{view['id']}"):
                st, h, out = px.get(path, ops)
                assert st == 200 and token.encode() not in out and \
                    token[12:].encode() not in out, out
                assert h.get("Cache-Control") == "no-store"
            audit = json.dumps(px.svc.cfg.audit(limit=500))
            assert token[12:] not in audit
            assert not any(token[12:] in line for line in px.logs)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 2

def test_outside_models_and_scopes():
    srv, port = engine()
    try:
        with Proxy(env={**ON, **env_for(port)}) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, _ = new_token(px, ops, label="laptop", scopes=["inference:openai"],
                                 models=["small-*"])
            for h in (bearer(token), {"x-api-key": token}):
                st, out = j(px.post("/v1/chat/completions", {"model": "big-1", **CHAT}, h))
                assert st == 403 and "big-1" in out["error"]["message"], (st, out)
                assert out["error"]["code"] == "permission_denied", out
                st, out = j(px.get("/v1/settings", h))
                assert st == 403 and out["error"]["scope"] == "settings:read", (st, out)
                assert "settings:read" in out["error"]["message"]
                assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT}, h)[0] == 200
            # a token of the other protocol only: the OpenAI routes name its scope
            t2, _ = new_token(px, ops, label="claude", scopes=["inference:anthropic"])
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer(t2)))
            assert st == 403 and out["error"]["scope"] == "inference:openai", out
            assert px.get("/v1/models", bearer(t2))[0] == 200
            # the Messages API's routes answer a token in their own shape
            st, out = j(px.post("/v1/messages", {"model": "small-1"}, bearer(token)))
            assert st == 403 and out["type"] == "error" and \
                out["error"]["type"] == "permission_error", out
            # two different tokens in the two headers: refused
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                {**bearer(token), "x-api-key": t2}))
            assert st == 401 and out["error"]["code"] == "invalid_api_key", out
            # a cache route takes the model list too (a cache route that answers 200 here)
            px.svc.cache_route = lambda path, body, headers: (200, {"ok": True}, ())
            st, out = j(px.post("/v1/cache/peek", {"model": "big-1", **CHAT}, bearer(token)))
            assert st == 403 and "big-1" in out["error"]["message"], (st, out)
            assert px.post("/v1/cache/peek", {"model": "small-1", **CHAT}, bearer(token))[0] \
                == 200
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 3

def test_expired_and_revoked():
    srv, port = engine()
    try:
        with Proxy(env={**ON, **env_for(port)}) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            short, _ = new_token(px, ops, label="short", scopes=["inference:openai"], days=1)
            gone, gone_view = new_token(px, ops, label="gone", scopes=["inference:openai"])
            for t in (short, gone):
                assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                               bearer(t))[0] == 200
            st, out = j(px.request("DELETE", f"/v1/auth/tokens/{gone_view['id']}", None, ops))
            assert st == 200 and out["state"] == "revoked", out
            shift(2)
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer(short)))
            assert st == 401 and "expired" in out["error"]["message"], out
            assert out["error"]["code"] == "invalid_api_key"
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer(gone)))
            assert st == 401 and "revoked" in out["error"]["message"], out
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer("tdm-00000000-" + "x" * 43)))
            assert st == 401 and "not known" in out["error"]["message"], out
            # on the Messages API's routes: its authentication_error
            st, out = j(px.post("/v1/messages", {"model": "small-1"}, {"x-api-key": gone}))
            assert st == 401 and out["error"]["type"] == "authentication_error", out
            # an open route stays open with a dead token
            assert px.get("/health", bearer(gone))[0] == 200
    finally:
        srv.shutdown()


def test_wrong_tokens_are_limited():
    """SEC-28: 20 wrong API tokens a minute from one address, then every request with a token
    from there is a 429 for the rest of the minute; a session, another address and the next
    minute are not affected."""
    srv, port = engine()
    try:
        with Proxy(env={**ON, **env_for(port)}) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            good, _ = new_token(px, ops, label="good", scopes=["inference:openai"])
            body = {"model": "small-1", **CHAT}
            for i in range(20):
                assert px.post("/v1/chat/completions", body,
                               bearer(f"tdm-{i:08x}-" + "x" * 43))[0] == 401, i
            st, h, out = px.post("/v1/chat/completions", body, bearer("tdm-0000ffff-" + "y" * 43))
            assert st == 429 and h.get("Retry-After") == "60", (st, h, out)
            assert json.loads(out)["error"]["code"] == "rate_limited", out
            # a right token from the same address waits too, in either header and shape
            assert px.post("/v1/chat/completions", body, bearer(good))[0] == 429
            st, out = j(px.post("/v1/messages", {"model": "small-1"}, {"x-api-key": good}))
            assert st == 429 and out["type"] == "error", out
            # the page's session is not a token: it still works
            assert px.get("/v1/auth/session", ops)[0] == 200
            # a service identity from the same address goes on (chatd, a collector, the relay)
            chatd = accounts().tokens.mint_service(tokens_mod.CHATD_SERVICE,
                                                   tokens_mod.CHATD_SCOPES)
            assert px.get("/chat-engine/v1/models", bearer(chatd))[0] == 200
            # another address (from the loopback proxy, X-Real-IP counts) is not limited: it
            # meets the exposure rule (this-machine by default), not the limit
            st, _, out = px.post("/v1/chat/completions", body,
                                 {**bearer(good), "X-Real-IP": "203.0.113.9"})
            assert st == 403 and json.loads(out)["error"]["code"] == "api_not_exposed", (st, out)
            # the next minute the address is free again
            base = time.time()
            accounts().limits.tokens.clock = lambda: base + 61
            assert px.post("/v1/chat/completions", body, bearer(good))[0] == 200
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 4

def test_an_imported_key():
    srv, port = engine()
    try:
        old = legacy_state(port)
        try:
            # the release before's key rule: both keys answer, each as its label (the ledger's
            # user and the user it sent upstream)
            c = old.svc.cfg._conn
            env_users = {tokens_mod.key_hash(KEY_ENV): "laptop"}
            got = {k: int5_schema.key_rule_like_int5(c, k, env_users) for k in (KEY_DB, KEY_ENV)}
            assert got == {KEY_DB: (200, "phone"), KEY_ENV: (200, "laptop")}, got
            before = {k: (v[1], v[1]) for k, v in got.items()}
            new = Proxy(env={**ON, **env_for(port, TANDEM_API_KEYS=f"laptop:{KEY_ENV}")})
            copy_state(old, new)
        finally:
            old.__exit__(None, None, None)
        with new as px:
            px.sync()
            for key in (KEY_DB, KEY_ENV):
                st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                    bearer(key)))
                assert st == 200, (st, out)
                got = upstream(srv)
                assert (px.rows()[-1]["user"], got["x-tandem-user"]) == before[key], got
                assert "authorization" not in got and "x-api-key" not in got, got
            rows = {r["label"]: r for r in accounts().tokens.all()}
            assert rows["phone"]["source"] == "users" and rows["laptop"]["source"] == "env"
            for r in rows.values():
                assert r["needs_review"] and r["expires_at"] is None and \
                    r["scopes"] == ["inference:openai"] and r["owner_id"] is None, r
            make("own", "owner")
            own = sign_in(px, "own")
            f = controller_findings(px, own)
            assert f["tokens_need_review"]["facts"] == {"n": 2}, f
            # confirmed, it leaves Review
            st, out = j(px.request("PATCH", f"/v1/auth/tokens/{rows['phone']['id']}",
                                   {"needs_review": False}, own))
            assert st == 200 and out["needs_review"] is False, out
            assert findings_now()["tokens_need_review"]["facts"] == {"n": 1}
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 5 + AC g

def test_the_admin_token_for_scripts():
    # an install with admin.json (the owner) whose scripts send the admin token
    px = Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": ADMIN_TOKEN})
    adminpw.Password(os.path.join(px.dir, "admin.json")).set(PW["own"])
    with px:
        a = accounts()
        owner = a.store.by_username("owner")
        row = next(r for r in a.tokens.all() if r["source"] == "admin_token")
        assert row["scopes"] == ["settings:read", "settings:write", "fleet:read", "fleet:write",
                                 "metrics:read"] and "trust" not in row["scopes"], row
        assert row["owner_id"] == owner["id"] and row["needs_review"], row
        days = (row["expires_at"] - row["created_at"]) / 86_400_000
        assert abs(days - 30) < 0.001, days          # two clock reads apart
        h = bearer(ADMIN_TOKEN)
        assert px.get("/v1/settings", h)[0] == 200
        st, out = j(px.post("/v1/fleet/pair", {"address": "192.0.2.9"}, h))
        assert st == 403 and out["error"]["permission"] == "trust", out
        # it never signs in to the page
        assert px.post("/v1/auth/session", {"username": "owner", "password": ADMIN_TOKEN})[0] \
            == 401
        assert px.post("/v1/dashboard/session", {"token": ADMIN_TOKEN})[0] == 401
        PW["owner"] = PW["own"]
        own = sign_in(px, "owner")
        st, out = j(px.get("/v1/auth/tokens?all=1", own))
        mine = next(t for t in out["tokens"] if t["source"] == "admin_token")
        assert mine["needs_review"] and mine["label"] == "admin token", mine
        f = controller_findings(px, own)
        assert "legacy_admin_token" in f and f["tokens_need_review"]["facts"] == {"n": 1}, f
        # it expires: the finding goes with it
        shift(31)
        assert "legacy_admin_token" not in findings_now()
        assert px.get("/v1/settings", h)[0] == 401
        shift(0)
        # revoked by an admin: the finding goes too, and a restart does not bring it back
        make("adm", "admin")
        adm = sign_in(px, "adm")
        st, out = j(px.request("DELETE", f"/v1/auth/tokens/{mine['id']}", None, adm))
        assert st == 200 and out["state"] == "revoked", out
        assert "legacy_admin_token" not in findings_now()
        a.store.set_state("tokens_imported", {})
        authlegacy.migrate(a, px.dir)
        assert px.get("/v1/settings", h)[0] == 401


# ---------------------------------------------------------------------------- Gherkin 6

def test_going_back_after_a_revocation():
    srv, port = engine()
    try:
        old = legacy_state(port)
        other = "tdm-another-key-made-before-the-0002"
        old.svc.cfg._conn.execute(
            "INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) VALUES "
            "('tablet', ?, ?, 'user', 1, 1)", (tokens_mod.key_hash(other), other[-4:]))
        old.svc.cfg._conn.commit()
        new = Proxy(env={**ON, **env_for(port)})
        copy_state(old, new)
        old.__exit__(None, None, None)
        with new as px:
            make("own", "owner")
            own = sign_in(px, "own")
            phone = next(r for r in accounts().tokens.all() if r["label"] == "phone")
            assert px.request("DELETE", f"/v1/auth/tokens/{phone['id']}", None, own)[0] == 200
            db = px.svc.cfg.path
            back = os.path.join(tempfile.mkdtemp(prefix="int5-back-"), "config.sqlite3")
            px.svc.cfg._conn.execute("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(db, back)
        c = sqlite3.connect(back)
        assert int5_schema.migrate_like_int5(c) == int5_schema.SCHEMA_VERSION
        assert int5_schema.key_rule_like_int5(c, other) == (200, "tablet")
        assert int5_schema.key_rule_like_int5(c, KEY_DB) == (401, None)
        assert int5_schema.key_rule_like_int5(c, None) == (401, None)
        c.close()
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 7

def test_the_owner_loses_rights():
    with Proxy(env=ON) as px:
        make("own", "owner")
        make("ops", "operator")
        own, ops = sign_in(px, "own"), sign_in(px, "ops")
        token, view = new_token(px, ops, label="ci", scopes=["settings:read", "settings:write"])
        plan = {"changes": [{"scope": "app", "target": "controller", "set": {"chat": "off"}}]}
        st, out = j(px.post("/v1/settings/plan", plan, bearer(token)))
        assert st not in (401, 403), (st, out)
        ops_id = accounts().store.by_username("ops")["id"]
        assert px.request("PATCH", f"/v1/auth/users/{ops_id}", {"role": "viewer"}, own)[0] == 200
        st, out = j(px.post("/v1/settings/plan", plan, bearer(token)))
        assert st == 403 and out["error"]["permission"] == "settings:write", (st, out)
        # still the viewer's: a read answers, the token itself is not revoked
        assert px.get("/v1/settings", bearer(token))[0] == 200
        assert accounts().tokens.get(view["id"])["revoked_at"] is None


# ---------------------------------------------------------------------------- Gherkin 8

def test_nothing_leaks_upstream():
    srv, port = engine()
    try:
        for backend_key in (None, "sk-backend-000000000001"):
            more = {"TANDEM_BACKEND_ENGINE_KEY": backend_key} if backend_key else {}
            with Proxy(env={**ON, **env_for(port, **more)}) as px:
                px.sync()
                make("ops", "operator")
                ops = sign_in(px, "ops")
                token, _ = new_token(px, ops, label="t", scopes=["inference:openai"])
                for h in (bearer(token), {"x-api-key": token},
                          {**bearer(token), "x-api-key": token}):
                    for stream in (False, True):
                        st, _, _ = px.post("/v1/chat/completions",
                                           {"model": "small-1", "stream": stream, **CHAT}, h)
                        assert st == 200
                        got = upstream(srv)
                        assert "x-api-key" not in got, got
                        assert got.get("authorization") == (f"Bearer {backend_key}" if
                                                            backend_key else None), got
                        assert token not in json.dumps(got)
                # the gateway route of the page, too
                st, _, _ = px.post("/v1/fleet/gateway/chat/completions",
                                   {"model": "small-1", **CHAT}, bearer(token))
                assert st == 200 and token not in json.dumps(upstream(srv))
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 9

def test_a_viewer_and_inference_tokens():
    with Proxy(env=ON) as px:
        make("own", "owner")
        make("ops", "operator")
        make("view", "viewer")
        view, ops = sign_in(px, "view"), sign_in(px, "ops")
        st, out = j(px.post("/v1/auth/tokens", {"label": "x", "scopes": ["inference:openai"]},
                            view))
        assert st == 403 and "admin must allow" in out["error"]["message"], out
        assert out["error"]["permission"] == "tokens:inference"
        new_token(px, ops, label="x", scopes=["inference:openai"])
        px.svc.cfg.set_setting("api.tokens.viewers_may_create", True, who="test")
        token, _ = new_token(px, view, label="x", scopes=["inference:openai"])
        # never more than a viewer: no management scope, and the token reads no settings
        st, out = j(px.post("/v1/auth/tokens", {"label": "y", "scopes": ["settings:read"]},
                            view))
        assert st == 403 and out["error"]["permission"] == "tokens:management", out
        assert px.get("/v1/settings", bearer(token))[0] == 403
        # a viewer's never-expiring token needs an admin
        st, out = j(px.post("/v1/auth/tokens", {"label": "z", "scopes": ["inference:openai"],
                                                "days": "never"}, view))
        assert st == 403, out
        # trust: an admin's token only, 30 days at most
        st, out = j(px.post("/v1/auth/tokens", {"label": "t", "scopes": ["trust"]}, ops))
        assert st == 403 and out["error"]["permission"] == "trust", out
        own = sign_in(px, "own")
        st, out = j(px.post("/v1/auth/tokens", {"label": "t", "scopes": ["trust"], "days": 31},
                            own))
        assert st == 400, out
        new_token(px, own, label="t", scopes=["trust"], days=30)
        # service scopes are never on a person's token
        st, out = j(px.post("/v1/auth/tokens", {"label": "s", "scopes": ["chat-engine"]}, own))
        assert st == 403, out


# ---------------------------------------------------------------------------- Gherkin 10

def test_the_rollback_guard():
    with Proxy(env=ON) as px:
        c = px.svc.cfg._conn
        assert c.execute("SELECT COUNT(*) FROM users").fetchone() == (0,)
        make("ops", "operator")
        token, _ = new_token(px, sign_in(px, "ops"), label="first", scopes=["inference:openai"])
        rows = c.execute("SELECT label, key_hash, enabled FROM users").fetchall()
        assert [(r[0], r[2]) for r in rows] == [("rollback-guard", 1)], rows
        assert len(rows[0][1]) == 64 and rows[0][1] != tokens_mod.key_hash(token)
        assert int5_schema.key_rule_like_int5(c, None) == (401, None)
        assert int5_schema.key_rule_like_int5(c, token) == (401, None)
        # a second token writes nothing more
        new_token(px, sign_in(px, "ops"), label="second", scopes=["inference:openai"])
        assert c.execute("SELECT COUNT(*) FROM users").fetchone() == (1,)
    # every imported key revoked: the guard keeps the release before closed
    srv, port = engine()
    try:
        old = legacy_state(port)
        new = Proxy(env={**ON, **env_for(port)})
        copy_state(old, new)
        old.__exit__(None, None, None)
        with new as px:
            c = px.svc.cfg._conn
            assert c.execute("SELECT label FROM users WHERE enabled = 1").fetchall() == \
                [("phone",)]
            ts = accounts().tokens
            for r in ts.all():
                ts.revoke(r["id"], actor="test")
            assert c.execute("SELECT label FROM users WHERE enabled = 1").fetchall() == \
                [("rollback-guard",)]
            assert int5_schema.key_rule_like_int5(c, None) == (401, None)
            assert int5_schema.key_rule_like_int5(c, KEY_DB) == (401, None)
            # and the import never takes the guard for a key
            a = accounts()
            a.store.set_state("tokens_imported", {})
            authlegacy.migrate(a, px.dir)
            assert not any(r["label"] == "rollback-guard" for r in ts.all())
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC a

def test_only_the_hash_is_stored():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        token, view = new_token(px, sign_in(px, "ops"), label="t",
                                scopes=["inference:openai"])
        c = px.svc.cfg._conn
        row = c.execute("SELECT id, hash, last4 FROM api_tokens").fetchone()
        assert row == (view["id"], hashlib.sha256(token.encode()).hexdigest(), token[-4:]), row
        c.execute("PRAGMA wal_checkpoint(FULL)")
        secret = token[13:].encode()
        for name in os.listdir(px.dir):
            p = os.path.join(px.dir, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    data = f.read()
                assert secret not in data and secret[:20] not in data, name
        # the full tables, every text column: no part of the secret beyond the last 4
        for table in ("api_tokens", "audit", "settings"):
            for r in c.execute(f"SELECT * FROM {table}"):
                assert token[13:-4] not in json.dumps(r, default=str), table


# ---------------------------------------------------------------------------- AC c

def test_rotate():
    srv, port = engine()
    try:
        with Proxy(env={**ON, **env_for(port)}) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            old, view = new_token(px, ops, label="rot", scopes=["inference:openai"])
            assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                           bearer(old))[0] == 200
            st, h, out = px.post(f"/v1/auth/tokens/{view['id']}/rotate", {"grace_s": 60}, ops)
            doc = json.loads(out)
            new = doc.pop("token")
            assert st == 200 and h.get("Cache-Control") == "no-store", doc
            assert (doc["id"], doc["label"]) == (view["id"], view["label"]) and new != old
            for t in (new, old):
                assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                               bearer(t))[0] == 200
            shift(61 / 86400)
            st, out = j(px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                                bearer(old)))
            assert st == 401, (st, out)
            assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                           bearer(new))[0] == 200
            # usage stays the token's: every row names the one id
            ids = {r["token_id"] for r in px.rows() if r["status"] == 200}
            assert ids == {view["id"]}, ids
            # no grace: the old secret stops at once
            shift(0)
            _, newer = (lambda d: (d, d.pop("token")))(json.loads(px.post(
                f"/v1/auth/tokens/{view['id']}/rotate", {}, ops)[2]))
            assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                           bearer(new))[0] == 401
            assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                           bearer(newer))[0] == 200
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC d

def test_last_used_once_a_minute():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        token, view = new_token(px, sign_in(px, "ops"), label="t", scopes=["metrics:read"])
        ts = accounts().tokens
        writes = []
        px.svc.cfg._conn.set_trace_callback(
            lambda sql: writes.append(sql) if "SET last_used_at" in sql else None)
        try:
            ts._flushed = time.monotonic()
            for _ in range(20):
                assert px.get("/metrics", bearer(token))[0] == 200
            assert writes == [] and ts.get(view["id"])["last_used_at"] is None
            ts._flushed -= tokens_mod.SEEN_FLUSH_S + 1          # a minute later
            assert px.get("/metrics", bearer(token))[0] == 200
            assert len(writes) == 1 and ts.get(view["id"])["last_used_at"] is not None
            for _ in range(20):
                px.get("/metrics", bearer(token))
            assert len(writes) == 1, writes
            # the list shows the last use from memory meanwhile
            st, out = j(px.get(f"/v1/auth/tokens/{view['id']}", sign_in(px, "ops")))
            assert out["last_used_at"] is not None
        finally:
            px.svc.cfg._conn.set_trace_callback(None)


# ---------------------------------------------------------------------------- AC e

def test_a_thousand_tokens():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        st = accounts().store
        ops = st.by_username("ops")
        ts = st.tokens
        made = [ts.create(ops, label=f"t{i}", scopes=["inference:openai"], actor="test")[1]
                for i in range(1000)]
        assert len({tokens_mod.key_hash(t) for t in made}) == 1000
        want = made[500]
        h = {"Authorization": f"Bearer {want}"}
        times = []
        for _ in range(2000):
            t0 = time.perf_counter()
            p = ts.resolve(h)
            ok = tokens_mod.scope_for("POST", "/v1/chat/completions")[0] in p.scopes and \
                "inference" in p.perms
            times.append(time.perf_counter() - t0)
            assert ok
        med = statistics.median(times) * 1000
        assert med < 1.0, f"the check takes {med:.3f} ms"
        return f"median {med:.3f} ms over 2000 checks with 1000 tokens"


# ---------------------------------------------------------------------------- AC f

def test_the_import_twice():
    srv, port = engine()
    try:
        old = legacy_state(port)
        c = old.svc.cfg._conn
        chatd_key = "tdm-chatd-own-key-made-before-0003"
        c.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) VALUES "
                  "('chatd', ?, ?, 'user', 1, 1)", (tokens_mod.key_hash(chatd_key),
                                                    chatd_key[-4:]))
        c.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) VALUES "
                  "('old-off', ?, NULL, 'user', 0, 1)",
                  (tokens_mod.key_hash("tdm-a-disabled-key-before-upgrade"),))
        c.commit()
        other = os.path.join(tempfile.mkdtemp(prefix="users-from-"), "config.sqlite3")
        oc = sqlite3.connect(other)
        oc.executescript(int5_schema.DDL)
        from_key = "tdm-a-key-of-the-other-database-04"
        oc.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) "
                   "VALUES ('bot', ?, ?, 'user', 1, 1)", (tokens_mod.key_hash(from_key),
                                                          from_key[-4:]))
        oc.commit()
        env = {**ON, **env_for(port, TANDEM_API_KEYS=f"laptop:{KEY_ENV}",
                                 TANDEM_API_USERS_FROM=other)}
        new = Proxy(env=env)
        copy_state(old, new)
        old.__exit__(None, None, None)
        with new as px:
            px.sync()
            a = accounts()
            ts = a.tokens
            rows = {r["label"]: r for r in ts.all()}
            assert set(rows) == {"phone", "laptop", "bot", "svc:chatd", "old-off"}, set(rows)
            assert rows["svc:chatd"]["kind"] == "service" and \
                not rows["svc:chatd"]["needs_review"] and \
                rows["svc:chatd"]["scopes"] == ["inference:openai", "chat-engine"], rows
            assert rows["old-off"]["revoked_at"] is not None and \
                not rows["old-off"]["needs_review"]
            assert rows["bot"]["source"] == "users_from"
            assert ts.need_review() == 3

            def snap():
                return (px.svc.cfg._conn.execute("SELECT * FROM api_tokens ORDER BY id")
                        .fetchall(),
                        px.svc.cfg._conn.execute("SELECT * FROM users ORDER BY id").fetchall())
            before = snap()
            assert authlegacy.migrate(a, px.dir)["tokens"] == {}
            assert snap() == before
            # even with the record of the first run gone: the hashes are known, nothing changes
            a.store.set_state("tokens_imported", {})
            did = authlegacy.migrate(a, px.dir)["tokens"]
            assert did == {"users": 0, "env": 0, "users_from": 0}, did
            assert snap() == before
            # no legacy source is read for auth any more
            late_db = "tdm-a-users-row-added-after-start-05"
            px.svc.cfg._conn.execute(
                "INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) VALUES "
                "('late', ?, NULL, 'user', 1, 1)", (tokens_mod.key_hash(late_db),))
            px.svc.cfg._conn.commit()
            late_env = "tdm-an-env-key-added-after-start-06"
            px.svc.cfg.env_users[tokens_mod.key_hash(late_env)] = "late-env"
            late_from = "tdm-a-key-the-other-db-got-later-07"
            oc.execute("INSERT INTO users(label, key_hash, key_hint, role, enabled, created_at) "
                       "VALUES ('late-bot', ?, NULL, 'user', 1, 1)",
                       (tokens_mod.key_hash(late_from),))
            oc.commit()
            for key in (late_db, late_env, late_from):
                assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                               bearer(key))[0] == 401, key
            for key in (KEY_DB, KEY_ENV, from_key):
                assert px.post("/v1/chat/completions", {"model": "small-1", **CHAT},
                               bearer(key))[0] == 200, key
        oc.close()
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC h

def test_disable_and_remove_revoke():
    from proxy import userscli
    with Proxy(env=ON) as px:
        make("own", "owner")
        for n in ("ops", "adm", "view"):
            make(n, {"ops": "operator", "adm": "admin", "view": "viewer"}[n])
        own = sign_in(px, "own")
        st = accounts().store
        tok = {}
        for n in ("ops", "adm"):
            tok[n] = st.tokens.create(st.by_username(n), label="t", scopes=["metrics:read"],
                                      actor="test")[1]
            assert px.get("/metrics", bearer(tok[n]))[0] == 200
        # through the routes
        ops_id = st.by_username("ops")["id"]
        assert px.request("PATCH", f"/v1/auth/users/{ops_id}", {"enabled": False}, own)[0] == 200
        st_, out = j(px.get("/metrics", bearer(tok["ops"])))
        assert st_ == 401 and "revoked" in out["error"]["message"], out
        # enabled again, the token stays revoked
        assert px.request("PATCH", f"/v1/auth/users/{ops_id}", {"enabled": True}, own)[0] == 200
        assert px.get("/metrics", bearer(tok["ops"]))[0] == 401
        # through the CLI (it writes the database only): the next request is refused
        with contextlib.redirect_stdout(io.StringIO()):
            assert userscli.main(["remove", "adm", "--config-db", px.svc.cfg.path]) == 0
        assert px.get("/metrics", bearer(tok["adm"]))[0] == 401
        assert all(r["revoked_at"] for r in st.tokens.all())
        view_tok = None
        px.svc.cfg.set_setting("api.tokens.viewers_may_create", True, who="test")
        view_tok = st.tokens.create(st.by_username("view"), label="v",
                                    scopes=["inference:openai"], actor="test")[1]
        with contextlib.redirect_stdout(io.StringIO()):
            assert userscli.main(["disable", "view", "--config-db", px.svc.cfg.path]) == 0
        assert px.get("/v1/models", bearer(view_tok))[0] == 401


# ---------------------------------------------------------------------------- AC i

def test_the_tokens_cli():
    from proxy import tokenscli
    with Proxy(env=ON) as px:
        make("ops", "operator")
        path = px.svc.cfg.path

        def cli(*args) -> tuple[int, str]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = tokenscli.main([*args, "--config-db", path])
            return rc, out.getvalue()
        rc, out = cli("create", "ops", "--label", "cli", "--scope", "metrics:read")
        assert rc == 0 and "shown once" in out, out
        token = next(w for w in out.split() if w.startswith("tdm-"))
        assert px.get("/metrics", bearer(token))[0] == 200
        tid = token[4:12]
        rc, out = cli("list")
        assert rc == 0 and tid in out and token not in out, out
        rc, out = cli("rotate", tid)
        new = next(w for w in out.split() if w.startswith("tdm-") and len(w) > 40)
        assert rc == 0 and px.get("/metrics", bearer(token))[0] == 401
        assert px.get("/metrics", bearer(new))[0] == 200
        rc, out = cli("revoke", tid)
        assert rc == 0 and px.get("/metrics", bearer(new))[0] == 401
        # a viewer's account cannot get an inference token by the CLI either
        make("view", "viewer")
        rc, out = cli("create", "view", "--label", "x", "--scope", "inference:openai")
        assert rc == 1 and "admin must allow" in out, out
        rc, out = cli("confirm", "nope")
        assert rc == 1


# ---------------------------------------------------------------------------- AC j

def test_the_ledger_13():
    from tandemapp import ledger as ledger_mod
    path = os.path.join(tempfile.mkdtemp(prefix="led13-"), "ledger.sqlite3")
    c = sqlite3.connect(path)
    assert ledger_mod.migrate(c) == 0
    cols = [r[1] for r in c.execute("PRAGMA table_info(requests)")]
    assert cols[-2:] == ["token_id", "api"] and ledger_mod.SCHEMA_VERSION == 13, cols
    # the release before: its version 12, its columns, on this file
    saved = ledger_mod.SCHEMA_VERSION, ledger_mod.ADDED, ledger_mod.COLUMNS
    try:
        ledger_mod.SCHEMA_VERSION = 12
        ledger_mod.ADDED = saved[1][:-2]
        ledger_mod.COLUMNS = saved[2][:-2]
        assert ledger_mod.migrate(c) == 13
        c.execute(f"INSERT INTO requests ({', '.join(ledger_mod.COLUMNS)}) VALUES "
                  f"({', '.join('?' * len(ledger_mod.COLUMNS))})",
                  tuple(1 for _ in ledger_mod.COLUMNS))
    finally:
        ledger_mod.SCHEMA_VERSION, ledger_mod.ADDED, ledger_mod.COLUMNS = saved
    assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == \
        ("13",)
    assert c.execute("SELECT token_id, api FROM requests").fetchall() == [(None, None)]
    c.close()
    # client_of reads x-api-key as it reads a bearer
    a = ledger_mod.client_of({"x-api-key": "k1"})[0]
    assert a == ledger_mod.client_of({"Authorization": "Bearer k1"})[0] != "anon"


# ---------------------------------------------------------------------------- AC l

def test_each_scope_opens_its_routes():
    with Proxy(env=ON) as px:
        make("own", "owner")
        own = sign_in(px, "own")
        t = {s: new_token(px, own, label=s, scopes=[s])[0] for s in tokens_mod.MANAGEMENT}
        plan = {"changes": [{"scope": "app", "target": "controller", "set": {"chat": "off"}}]}
        cases = [
            ("usage:read", "GET", "/v1/dashboard/usage", 200),
            ("usage:read", "GET", "/v1/fleet/ledger/usage", 200),
            ("usage:read", "GET", "/v1/dashboard/logs", 403),        # the ledger's views only
            ("usage:read", "GET", "/v1/dashboard/system", 403),
            ("usage:read", "GET", "/v1/settings", 403),
            ("usage:read", "GET", "/v1/fleet/nodes", 403),
            ("settings:read", "GET", "/v1/settings", 200),
            ("settings:read", "POST", "/v1/settings/plan", 403),
            ("settings:read", "GET", "/v1/fleet/nodes", 403),
            ("settings:read", "GET", "/v1/dashboard/usage", 403),
            ("settings:write", "POST", "/v1/settings/plan", "not 403"),
            ("fleet:read", "GET", "/v1/fleet/nodes", 200),
            ("fleet:read", "GET", "/v1/fleet/ledger/usage", 200),
            ("fleet:read", "GET", "/v1/settings", 403),
            ("fleet:write", "POST", "/v1/fleet/pair", 403),            # pairing is trust's
            ("metrics:read", "GET", "/metrics", 200),
            ("metrics:read", "GET", "/v1/settings", 403),
        ]
        for scope, method, path, want in cases:
            st, out = j(px.request(method, path, plan if method == "POST" else None,
                                   bearer(t[scope])))
            if want == "not 403":
                assert st not in (401, 403), (scope, path, st, out)
            else:
                assert st == want, (scope, method, path, st, out)
        # metrics:read gets the full health; an inference token only the status
        assert "backends" in j(px.get("/health", bearer(t["metrics:read"])))[1]
        inf, _ = new_token(px, own, label="inf", scopes=["inference:openai"])
        assert j(px.get("/health", bearer(inf))) == (200, {"status": "ok"})


# ---------------------------------------------------------------------------- AC k

def test_what_a_token_never_does():
    with Proxy(env=ON) as px:
        make("own", "owner")
        own = sign_in(px, "own")
        token, view = new_token(px, own, label="all", days="never",
                                scopes=list(tokens_mod.INFERENCE + tokens_mod.MANAGEMENT))
        for method, path in (("GET", "/v1/auth/tokens"), ("POST", "/v1/auth/tokens"),
                             ("GET", "/v1/auth/users"), ("GET", "/v1/auth/sessions"),
                             ("DELETE", f"/v1/auth/tokens/{view['id']}"),
                             ("GET", "/v1/chat-app/chats")):
            st, out = j(px.request(method, path, {"label": "x", "scopes": ["metrics:read"]}
                                   if method == "POST" else None, bearer(token)))
            assert st == 403, (method, path, st, out)
        assert accounts().tokens.get(view["id"])["revoked_at"] is None
        # local mode makes no token: a token is for another device
        a = accounts()
        with Proxy(env=ON) as px2:
            a = accounts()
            a.local.bind = "127.0.0.1"
            a.local.exists = lambda p: False
            st, out = j(px2.post("/v1/auth/tokens", {"label": "x",
                                                     "scopes": ["inference:openai"]}))
            assert st == 409 and out["error"]["code"] == "password_first", (st, out)


if __name__ == "__main__":
    run(globals())
