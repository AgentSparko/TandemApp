"""SEC-24: server-side sessions, the cookie, one CSRF rule, the sign-in limits and the audit
(proxy/accounts.py, proxy/authapi.py), with TANDEM_AUTH=accounts.

  1  Gherkin "Sign in and out": a cookie with HttpOnly, SameSite=Strict, Path=/, no Domain,
     Max-Age the idle time (no Secure over http); the list shows one row with the address and the
     browser; signed out, the same cookie is 401; https (X-Forwarded-Proto from a loopback or a
     trusted proxy peer only) names the cookie __Host-tandem_session with Secure
                                                                   test_sign_in_and_out
  2  Gherkin "An admin ends someone's session": the next request with the cookie is 401 and the
     viewer's open event stream is closed within 5 s; a session ended in the table by another
     process closes its stream too; a viewer cannot end another's session   test_admin_ends_session
  3  Gherkin "A write from another site": a form post from another origin with the cookie is 403
     and changes nothing; an Origin of this host passes                     test_cross_site_write
  4  Gherkin "A write without the CSRF header": 403; a wrong token 403; the right one passes; the
     sign-in itself needs no token but is same-origin JSON                   test_csrf_header
  5  Gherkin "Too many wrong passwords": five failures for one name (from five addresses) and the
     sixth is 429 with Retry-After; the right password works after the wait (no lock for good);
     five from one address and the sixth is 429 for a minute; an unknown name waits the same
                                                                   test_sign_in_limits
  6  AC: the database holds no session id, only its hash; the CSRF token is not stored either
                                                                   test_only_hashes_stored
  7  AC: a restart keeps sessions; a password change ends every session of the account (other
     browsers too) and gives this browser a new one; a wrong current password is 403
                                                                   test_restart_and_password_change
  8  AC: a request with an API credential and a stray cookie is the credential: its cookie is
     ignored and it is never checked for CSRF; with no token resolver it is a 401
                                                                   test_token_with_stray_cookie
  9  AC: a WebSocket upgrade with a foreign Origin, or none, is refused; an event stream from
     another origin is refused                                     test_websocket_origin
 10  AC: every event (sign-in ok and failed, sign-out, revoke, account made, role, disable,
     removal, password) is an audit row naming the account, with no password, session id, CSRF
     token, hash or salt in it, in the log or in any file but the hash columns
                                                                   test_audit_rows
 11  the timeouts are settings: the idle time renews at most once a day (the cookie again), a
     shorter idle or absolute time ends open sessions, the cap is 400 days   test_timeouts
 12  sign out everywhere and the old session routes over the accounts       test_everywhere_and_old_routes

    python tests/test_auth_sessions.py
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import accounts as acc_mod  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

ON = {"TANDEM_AUTH": "accounts"}
PW = {"own": "the first long password", "adm": "an admin's long password",
      "ops": "an operator's long password", "view": "a viewer's long password"}
NEW_PW = "a brand new long password"
UA = "TestBrowser/1.0 (session test)"


def accounts():
    return proxy_app.STATE["accounts"]


def make(name: str, role: str) -> dict:
    return accounts().store.create(name, role, PW[name], actor="test")


def sign_in(px, name: str, password: str | None = None, headers=None):
    h = {"User-Agent": UA, **(headers or {})}
    st, rh, out = px.post("/v1/auth/session", {"username": name,
                                               "password": password or PW[name]}, h)
    assert st == 200, (st, out)
    sid = rh["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    return {"Cookie": rh["Set-Cookie"].split(";", 1)[0],
            "X-Tandem-CSRF": json.loads(out)["csrf"]}, rh["Set-Cookie"], sid


def j(res):
    st, h, out = res
    return st, (json.loads(out) if out else None)


def ro(h: dict) -> dict:
    """A read with the cookie only."""
    return {"Cookie": h["Cookie"]}


def open_stream(px, headers: dict) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", px.port), timeout=10)
    lines = [f"GET /v1/dashboard/live HTTP/1.1", f"Host: 127.0.0.1:{px.port}",
             "Accept: text/event-stream"] + [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        got = s.recv(4096)
        assert got, head
        head += got
    assert head.startswith(b"HTTP/1.1 200"), head[:200]
    return s


def closed_within(s: socket.socket, limit: float) -> float | None:
    """Seconds until the server closed the stream, or None."""
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


# ---------------------------------------------------------------------------- 1

def test_sign_in_and_out():
    with Proxy(env=ON) as px:
        make("view", "viewer")
        h, cookie, sid = sign_in(px, "view", headers={"X-Real-IP": "192.0.2.44"})
        flags = [x.strip() for x in cookie.split(";")]
        assert flags[0].startswith(acc_mod.COOKIE_HTTP + "=")
        assert {"HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={30 * 86400}"} <= set(flags)
        assert not any(f.lower().startswith(("domain", "secure")) for f in flags), flags
        st, out = j(px.get("/v1/auth/sessions", ro(h)))
        assert st == 200 and len(out["sessions"]) == 1, out
        row = out["sessions"][0]
        assert row["address"] == "192.0.2.44" and row["agent"] == UA and row["current"]
        assert sid not in json.dumps(out) and "_id_hash" not in row
        st, out = j(px.get("/v1/auth/session", ro(h)))
        assert out["authenticated"] and out["account"]["username"] == "view"
        assert out["csrf"] == h["X-Tandem-CSRF"] and "self" in out["permissions"]
        assert px.get("/v1/dashboard/summary", ro(h))[0] == 200
        st, rh, _ = px.request("DELETE", "/v1/auth/session", None, h)
        assert st == 204 and "Max-Age=0" in rh["Set-Cookie"]
        assert px.get("/v1/dashboard/summary", ro(h))[0] == 401
        assert j(px.get("/v1/auth/session", ro(h)))[1]["authenticated"] is False
        # https: from this machine's proxy, or a trusted one; never from anyone's header
        _, cookie, _ = sign_in(px, "view", headers={"X-Forwarded-Proto": "https"})
        assert cookie.startswith(acc_mod.COOKIE_HTTPS + "=") and "Secure" in cookie
        a = accounts()
        assert a.secure("127.0.0.1", {"X-Forwarded-Proto": "https"})
        assert not a.secure("192.0.2.9", {"X-Forwarded-Proto": "https"})
        a.trusted_proxies = frozenset({"192.0.2.9"})
        assert a.secure("192.0.2.9", {"X-Forwarded-Proto": "https"})
        assert not a.secure("127.0.0.1", {})
        # the __Host- cookie wins when both are sent
        both = {"Cookie": f"{acc_mod.COOKIE_HTTP}=nothing; {cookie.split(';')[0]}"}
        assert px.get("/v1/dashboard/summary", both)[0] == 200


# ---------------------------------------------------------------------------- 2

def test_admin_ends_session():
    with Proxy(env=ON) as px:
        make("adm", "admin")
        make("view", "viewer")
        adm, _, _ = sign_in(px, "adm")
        view, _, _ = sign_in(px, "view")
        s = open_stream(px, ro(view))
        assert closed_within(s, 1.5) is None, "the stream stays open while the session lives"
        sessions = j(px.get(f"/v1/auth/sessions?user="
                            f"{accounts().store.by_username('view')['id']}", ro(adm)))[1]
        sid = sessions["sessions"][0]["id"]
        # a viewer cannot end someone else's
        st, out = j(px.request("DELETE", f"/v1/auth/sessions/"
                                         f"{j(px.get('/v1/auth/sessions', ro(adm)))[1]['sessions'][0]['id']}",
                               None, view))
        assert st == 403 and out["error"]["permission"] == "users"
        st, _ = j(px.request("DELETE", f"/v1/auth/sessions/{sid}", None, adm))
        assert st == 204
        took = closed_within(s, 5.0)
        assert took is not None and took < 5.0, took
        s.close()
        assert px.get("/v1/dashboard/summary", ro(view))[0] == 401
        assert px.get("/v1/dashboard/summary", ro(adm))[0] == 200, "the admin's own lives on"
        # ended in the table by another process (the CLI, another replica): within the check
        view, _, sid2 = sign_in(px, "view")
        s = open_stream(px, ro(view))
        con = sqlite3.connect(px.svc.cfg.path)
        con.execute("UPDATE sessions SET revoked_at = 1 WHERE id_hash = ?",
                    (hashlib.sha256(sid2.encode()).hexdigest(),))
        con.commit()
        con.close()
        took = closed_within(s, 5.0)
        assert took is not None and took < 5.0, took
        s.close()
        assert j(px.request("DELETE", "/v1/auth/sessions/0123456789abcdef", None, adm))[0] == 404


# ---------------------------------------------------------------------------- 3

def test_cross_site_write():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        h, _, _ = sign_in(px, "ops")
        nid = px.svc.cfg.nodes()[0]["id"] if px.svc.cfg.nodes() else None
        before = px.svc.cfg.audit(limit=500)
        evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/x-www-form-urlencoded"}
        for path in ("/v1/settings/plan", "/v1/settings/apply", "/v1/fleet/layouts/fleet"):
            st, out = j(px.request("POST" if "settings" in path else "PUT", path,
                                   b"changes=1", {"Cookie": h["Cookie"], **evil}))
            assert st == 403, (path, st, out)
        # another origin without Sec-Fetch-Site, with the CSRF token even: refused
        st, out = j(px.post("/v1/settings/plan", {"changes": []},
                            {**h, "Origin": "http://evil.example"}))
        assert st == 403 and out["error"]["code"] == "cross_origin", out
        assert px.svc.cfg.audit(limit=500) == before, "nothing changed"
        st, _ = j(px.post("/v1/settings/plan", {"changes": []},
                          {**h, "Origin": f"http://127.0.0.1:{px.port}"}))
        assert st not in (401, 403), st
        del nid


# ---------------------------------------------------------------------------- 4

def test_csrf_header():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        h, _, _ = sign_in(px, "ops")
        body = {"device": "desk", "doc": {"a": 1}}
        st, out = j(px.request("PUT", "/v1/fleet/layouts/fleet", body, ro(h)))
        assert st == 403 and out["error"]["code"] == "csrf", out
        st, out = j(px.request("PUT", "/v1/fleet/layouts/fleet", body,
                               {**ro(h), acc_mod.CSRF_HEADER: "0" * 64}))
        assert st == 403 and out["error"]["code"] == "csrf"
        # a body that is not declared JSON
        st, out = j(px.request("PUT", "/v1/fleet/layouts/fleet", json.dumps(body).encode(),
                               {**h, "Content-Type": "text/plain"}))
        assert st == 415 and out["error"]["code"] == "json_only"
        st, _ = j(px.request("PUT", "/v1/fleet/layouts/fleet", body, h))
        assert st not in (401, 403, 415), st
        # a sign-out is a write too
        assert j(px.request("DELETE", "/v1/auth/session", None, ro(h)))[0] == 403
        assert px.get("/v1/settings", ro(h))[0] == 200
        # the sign-in: no token, but JSON from this origin
        st, out = j(px.request("POST", "/v1/auth/session",
                               b"username=ops&password=x",
                               {"Content-Type": "application/x-www-form-urlencoded"}))
        assert st == 415, (st, out)
        st, out = j(px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]},
                            {"Origin": "http://evil.example"}))
        assert st == 403, out
        # reads need no token
        assert px.get("/v1/auth/sessions", ro(h))[0] == 200


# ---------------------------------------------------------------------------- 5

def test_sign_in_limits():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        for i in range(acc_mod.ACCOUNT_FAILS):
            st, _, _ = px.post("/v1/auth/session", {"username": "ops", "password": f"wrong {i} xx"},
                               {"X-Real-IP": f"192.0.2.{10 + i}"})
            assert st == 401, (i, st)
        st, h, out = px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]},
                             {"X-Real-IP": "192.0.2.99"})
        assert st == 429 and int(h["Retry-After"]) >= 1, (st, out)
        # not locked for good: after the wait the right password works
        time.sleep(int(h["Retry-After"]) + 0.2)
        st, _, _ = px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]},
                           {"X-Real-IP": "192.0.2.98"})
        assert st == 200
        # an unknown name waits the same, so the wait does not tell who exists
        for i in range(acc_mod.ACCOUNT_FAILS):
            px.post("/v1/auth/session", {"username": "nobody", "password": f"wrong {i} xx"},
                    {"X-Real-IP": f"198.51.100.{10 + i}"})
        st, h, _ = px.post("/v1/auth/session", {"username": "nobody", "password": "x"},
                           {"X-Real-IP": "198.51.100.99"})
        assert st == 429 and h.get("Retry-After")
        # per address: five failures a minute, then 429 for a minute, whatever the name
        for i in range(5):
            assert px.post("/v1/auth/session", {"username": f"n{i}", "password": "wrong xx"},
                           {"X-Real-IP": "203.0.113.5"})[0] == 401
        st, h, _ = px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]},
                           {"X-Real-IP": "203.0.113.5"})
        assert st == 429 and h["Retry-After"] == "60"
    # the doubling wait, on a clock of its own: 2 s, 4 s, ... up to 300 s
    clock = [1000.0]
    lim = acc_mod.SignInLimits(clock=lambda: clock[0])
    waits = []
    for i in range(14):
        lim.failed(f"192.0.2.{i}", "ana")
        waits.append(lim.wait("198.51.100.1", "ana"))
        clock[0] += waits[-1]
    assert waits[:4] == [0, 0, 0, 0] and waits[4:8] == [2, 4, 8, 16] and max(waits) == 300
    clock[0] += acc_mod.ACCOUNT_FORGET_S + 1
    assert lim.wait("198.51.100.1", "ana") == 0


# ---------------------------------------------------------------------------- 6

def test_only_hashes_stored():
    with Proxy(env=ON) as px:
        make("view", "viewer")
        h, _, sid = sign_in(px, "view")
        con = sqlite3.connect(px.svc.cfg.path)
        rows = con.execute("SELECT * FROM sessions").fetchall()
        con.close()
        assert len(rows) == 1
        text = repr(rows)
        assert sid not in text and h["X-Tandem-CSRF"] not in text
        assert rows[0][0] == hashlib.sha256(sid.encode()).hexdigest()
        assert rows[0][2] == hashlib.sha256(h["X-Tandem-CSRF"].encode()).hexdigest()
        for f in px.files():
            assert sid.encode() not in open(f, "rb").read(), f


# ---------------------------------------------------------------------------- 7

def test_restart_and_password_change():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        one, _, _ = sign_in(px, "ops")
        two, _, _ = sign_in(px, "ops")
        # a restart: a new gate over the same database; nothing in memory carries over
        fresh = acc_mod.AccountsAuth(px.svc.cfg, log=px.logs.append)
        proxy_app.STATE.update(accounts=fresh, auth=fresh)
        px.svc.auth = fresh
        assert px.get("/v1/settings", ro(one))[0] == 200 and px.get("/v1/settings", ro(two))[0] \
            == 200
        st, out = j(px.request("PUT", "/v1/auth/password", {"current": "not it at all",
                                                            "password": NEW_PW}, one))
        assert st == 403 and out["error"]["code"] == "wrong_password"
        st, h, out = px.request("PUT", "/v1/auth/password", {"current": PW["ops"],
                                                             "password": "short"}, one)
        assert st == 400
        st, h, out = px.request("PUT", "/v1/auth/password", {"current": PW["ops"],
                                                             "password": NEW_PW}, one)
        assert st == 200, out
        new = {"Cookie": h["Set-Cookie"].split(";", 1)[0]}
        assert px.get("/v1/settings", ro(one))[0] == 401
        assert px.get("/v1/settings", ro(two))[0] == 401
        assert px.get("/v1/settings", new)[0] == 200
        assert px.post("/v1/auth/session", {"username": "ops", "password": PW["ops"]})[0] == 401
        sign_in(px, "ops", NEW_PW)


# ---------------------------------------------------------------------------- 8

def test_token_with_stray_cookie():
    with Proxy(env=ON) as px:
        make("adm", "admin")
        adm, _, _ = sign_in(px, "adm")
        # no API tokens yet: a credential is a 401, whatever cookie comes with it
        assert px.get("/v1/settings", {**adm, "Authorization": "Bearer anything"})[0] == 401
        assert px.get("/v1/settings", {**adm, "x-api-key": "anything"})[0] == 401
        # a token that may read: its cookie is ignored, and there is no CSRF check
        token = acc_mod.Principal(kind="token", id="t1", name="t1", via="token",
                                  scopes=frozenset({"read"}))
        accounts().token_resolver = lambda h: token if h.get("Authorization") == \
            "Bearer t1-secret" else None
        tok = {"Authorization": "Bearer t1-secret"}
        assert px.get("/v1/settings", tok)[0] == 200
        assert px.get("/v1/auth/users", {**tok, "Cookie": adm["Cookie"]})[0] == 403
        st, out = j(px.post("/v1/settings/plan", {"changes": []},
                            {**tok, "Cookie": adm["Cookie"]}))
        assert st == 403 and out["error"]["code"] == "permission_denied", out
        assert out["error"]["permission"] == "settings:write"
        assert j(px.get("/v1/auth/session", {**tok, "Cookie": adm["Cookie"]}))[1]["principal"] \
            == {"kind": "token", "name": "t1"}


# ---------------------------------------------------------------------------- 9

def _upgrade(px, headers: dict) -> bytes:
    s = socket.create_connection(("127.0.0.1", px.port), timeout=10)
    lines = ["GET /v1/chat-app/ws HTTP/1.1", f"Host: 127.0.0.1:{px.port}", "Upgrade: websocket",
             "Connection: Upgrade", "Sec-WebSocket-Version: 13",
             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="] + \
        [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    s.settimeout(10)
    out = b""
    while b"\r\n\r\n" not in out:
        got = s.recv(4096)
        if not got:
            break
        out += got
    s.close()
    return out


def test_websocket_origin():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        h, _, _ = sign_in(px, "ops")
        for origin in ({"Origin": "http://evil.example"}, {}):
            got = _upgrade(px, {"Cookie": h["Cookie"], **origin})
            assert got.startswith(b"HTTP/1.1 403"), got[:120]
        got = _upgrade(px, {"Cookie": h["Cookie"], "Origin": f"http://127.0.0.1:{px.port}"})
        assert not got.startswith((b"HTTP/1.1 403", b"HTTP/1.1 401")), got[:120]
        assert _upgrade(px, {"Origin": f"http://127.0.0.1:{px.port}"}).startswith(
            b"HTTP/1.1 401")
        # an event stream: never from another site or origin; a same-origin one sends no Origin
        st, _, _ = px.get("/v1/dashboard/parallel", {**ro(h), "Accept": "text/event-stream",
                                                     "Origin": "http://evil.example"})
        assert st == 403
        st, _, _ = px.get("/v1/dashboard/parallel?once=1", {**ro(h), "Accept":
                                                            "text/event-stream",
                                                            "Sec-Fetch-Site": "cross-site"})
        assert st == 403


# ---------------------------------------------------------------------------- 10

def test_audit_rows():
    with Proxy(env=ON) as px:
        make("own", "owner")
        own, _, own_sid = sign_in(px, "own")
        px.post("/v1/auth/session", {"username": "own", "password": "a wrong password xx"},
                {"X-Real-IP": "192.0.2.61"})
        st, out = j(px.post("/v1/auth/users", {"username": "view", "role": "viewer",
                                               "password": PW["view"]}, own))
        vid = out["user"]["id"]
        view, _, view_sid = sign_in(px, "view")
        px.request("PATCH", f"/v1/auth/users/{vid}", {"role": "operator"}, own)
        px.request("PATCH", f"/v1/auth/users/{vid}", {"enabled": False}, own)
        px.request("PATCH", f"/v1/auth/users/{vid}", {"enabled": True}, own)
        view, _, view_sid2 = sign_in(px, "view")
        sid_public = j(px.get(f"/v1/auth/sessions?user={vid}", ro(own)))[1]["sessions"][0]["id"]
        px.request("DELETE", f"/v1/auth/sessions/{sid_public}", None, own)
        view, _, view_sid3 = sign_in(px, "view")
        px.request("DELETE", "/v1/auth/session", None, view)
        st, h, _ = px.request("PUT", "/v1/auth/password", {"current": PW["own"],
                                                           "password": NEW_PW}, own)
        assert st == 200
        own2 = {"Cookie": h["Set-Cookie"].split(";", 1)[0]}
        csrf = j(px.get("/v1/auth/session", own2))[1]["csrf"]
        assert j(px.request("DELETE", f"/v1/auth/users/{vid}", None,
                            {**own2, "X-Tandem-CSRF": csrf}))[0] == 204
        rows = px.svc.cfg.audit(limit=200)
        got = {(r["who"], r["action"], r["object"]) for r in rows}
        for want in (("own", "sign_in", "session"), ("own", "sign_in_failed", "session"),
                     ("own", "create", "account"), ("own", "role", "account"),
                     ("own", "disable", "account"), ("own", "enable", "account"),
                     ("own", "revoke", "session"), ("view", "sign_out", "session"),
                     ("own", "password", "account"), ("own", "delete", "account")):
            assert want in got, (want, sorted(got))
        failed = next(r for r in rows if r["action"] == "sign_in_failed")
        assert failed["detail"] == {"address": "192.0.2.61"}
        con = sqlite3.connect(px.svc.cfg.path)
        docs = [json.loads(r[0]) for r in con.execute("SELECT pw FROM accounts")]
        con.close()
        secrets_ = [PW["own"], PW["view"], NEW_PW, "a wrong password xx", own_sid, view_sid,
                    view_sid2, view_sid3, own["X-Tandem-CSRF"], view["X-Tandem-CSRF"], csrf]
        hashes = [x for d in docs for x in (d["hash"], d["salt"])]
        text = json.dumps(rows) + "\n".join(px.logs)
        for s in secrets_ + hashes:
            assert s not in text, s[:6]
        for f in px.files():
            data = open(f, "rb").read()
            for s in secrets_:
                assert s.encode() not in data, (f, s[:6])
            if not os.path.basename(f).startswith("config.sqlite3"):
                for s in hashes:
                    assert s.encode() not in data, (f, s[:6])


# ---------------------------------------------------------------------------- 11

def test_timeouts():
    with Proxy(env=ON) as px:
        make("view", "viewer")
        cfg = px.svc.cfg
        h, _, sid = sign_in(px, "view")
        ih = hashlib.sha256(sid.encode()).hexdigest()
        day = acc_mod.DAY_MS

        def row():
            return accounts().store.session_row(ih)

        # renewed at most once a day: not now, then after a day of the idle time has passed
        st, rh, _ = px.get("/v1/settings", ro(h))
        assert st == 200 and "Set-Cookie" not in rh
        with cfg._lock:
            cfg._conn.execute("UPDATE sessions SET idle_until = idle_until - ?, last_seen_at = "
                              "last_seen_at - ? WHERE id_hash = ?", (2 * day, 2 * day, ih))
            cfg._conn.commit()
        old = row()["idle_until"]
        st, rh, _ = px.get("/v1/settings", ro(h))
        assert st == 200 and f"Max-Age={30 * 86400}" in rh.get("Set-Cookie", ""), rh
        assert row()["idle_until"] > old + day
        st, rh, _ = px.get("/v1/settings", ro(h))
        assert "Set-Cookie" not in rh
        # a shorter idle time ends a session idle longer than it, at once
        with cfg._lock:
            cfg._conn.execute("UPDATE sessions SET last_seen_at = last_seen_at - ? WHERE "
                              "id_hash = ?", (3 * day, ih))
            cfg._conn.commit()
        cfg.set_setting("auth.session.idle_days", 2, who="test")
        assert px.get("/v1/settings", ro(h))[0] == 401
        cfg.set_setting("auth.session.idle_days", 30, who="test")
        # the absolute time: from the start, never renewed
        h, cookie, sid = sign_in(px, "view")
        assert f"Max-Age={30 * 86400}" in cookie
        ih = hashlib.sha256(sid.encode()).hexdigest()
        with cfg._lock:
            cfg._conn.execute("UPDATE sessions SET created_at = created_at - ? WHERE id_hash = "
                              "?", (10 * day, ih))
            cfg._conn.commit()
        assert px.get("/v1/settings", ro(h))[0] == 200
        cfg.set_setting("auth.session.absolute_days", 9, who="test")
        assert px.get("/v1/settings", ro(h))[0] == 401
        # the cap: 400 days, at least 1
        cfg.set_setting("auth.session.absolute_days", 100000, who="test")
        cfg.set_setting("auth.session.idle_days", 0, who="test")
        pol = accounts().policy
        assert pol.absolute_days == 400 and pol.idle_days == 1
        _, cookie, _ = sign_in(px, "view")
        assert "Max-Age=86400" in cookie
        # rows of sessions that ended more than a day ago go
        n = len(px.svc.cfg._conn.execute("SELECT * FROM sessions").fetchall())
        with cfg._lock:
            cfg._conn.execute("UPDATE sessions SET last_seen_at = last_seen_at - ? WHERE "
                              "id_hash = ?", (3 * day, ih))
            cfg._conn.commit()
        assert accounts().store.prune() == 2           # this one and the first (idle 5 days)
        assert row() is None
        assert len(px.svc.cfg._conn.execute("SELECT * FROM sessions").fetchall()) == n - 2


# ---------------------------------------------------------------------------- 12

def test_everywhere_and_old_routes():
    with Proxy(env=ON) as px:
        make("ops", "operator")
        make("adm", "admin")
        a1, _, _ = sign_in(px, "ops")
        a2, _, _ = sign_in(px, "ops")
        adm, _, _ = sign_in(px, "adm")
        assert len(j(px.get("/v1/auth/sessions", ro(a1)))[1]["sessions"]) == 2
        # another account's: `users`
        aid = accounts().store.by_username("adm")["id"]
        assert j(px.get(f"/v1/auth/sessions?user={aid}", ro(a1)))[0] == 403
        assert j(px.request("DELETE", f"/v1/auth/sessions?user={aid}", None, a1))[0] == 403
        assert len(j(px.get("/v1/auth/sessions?user=*", ro(adm)))[1]["sessions"]) == 3
        st, rh, _ = px.request("DELETE", "/v1/auth/sessions", None, a1)
        assert st == 204 and "Max-Age=0" in rh["Set-Cookie"]
        assert px.get("/v1/settings", ro(a1))[0] == 401 and px.get("/v1/settings", ro(a2))[0] \
            == 401
        assert px.get("/v1/settings", ro(adm))[0] == 200
        # an admin signs another account out everywhere
        a1, _, _ = sign_in(px, "ops")
        oid = accounts().store.by_username("ops")["id"]
        assert j(px.request("DELETE", f"/v1/auth/sessions?user={oid}", None, adm))[0] == 204
        assert px.get("/v1/settings", ro(a1))[0] == 401
        # the old routes answer over the accounts
        a1, _, _ = sign_in(px, "ops")
        st, out = j(px.get("/v1/dashboard/session", ro(a1)))
        assert st == 200 and out["authenticated"] and out["login"] and out["password_set"]
        assert j(px.get("/v1/fleet/session", ro(a1)))[1]["authenticated"] is True
        assert j(px.get("/v1/fleet/session"))[1]["authenticated"] is False
        assert px.post("/v1/dashboard/session", {"token": PW["ops"]})[0] == 404
        assert px.post("/v1/chat-app/session", {"password": PW["ops"]})[0] == 404
        st, out = j(px.request("PUT", "/v1/fleet/app/password", {"password": NEW_PW}, a1))
        assert st in (403, 404), (st, out)
        assert j(px.get("/v1/fleet/app/password", ro(a1)))[0] == 200
        assert px.request("DELETE", "/v1/fleet/session", None, a1)[0] == 204
        assert px.get("/v1/settings", ro(a1))[0] == 401


if __name__ == "__main__":
    run(globals())
