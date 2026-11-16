"""SEC-22/SEC-24: with the accounts switch off (TANDEM_AUTH unset, or anything but `accounts`) every
route answers exactly as before the accounts existed.

  1  a matrix of routes x credentials x controller setups (open; the admin token; the token with
     the dashboard login on; a password in admin.json) answers the status codes recorded on the
     tree before the accounts (tests/auth_legacy_golden.json), for TANDEM_AUTH unset, `legacy`
     and a value nobody knows; the full /health body keeps its keys        test_matrix_unchanged
  2  switched off, nothing of the accounts exists: the policy object is the old one, no new table
     is in the config database, /v1/auth/* is 404                          test_nothing_new_exists

    python tests/test_auth_switch_off.py [--write]     (--write records the golden file)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import adminpw  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402
from tandemapp import auth as auth_mod  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_legacy_golden.json")
TOKEN = "legacy-admin-token-0123456789abcdef0123"
PW = "a legacy controller password"
LAN = {"X-Real-IP": "192.0.2.77"}

SETUPS = {
    "open": ({}, "off", False),
    "token": ({"TANDEM_ADMIN_TOKEN": TOKEN}, "off", False),
    "token-login": ({"TANDEM_ADMIN_TOKEN": TOKEN}, "on", False),
    "password": ({}, "off", True),
}
ROUTES = (
    ("GET", "/health", None), ("GET", "/metrics", None),
    ("GET", "/v1/dashboard/summary", None), ("GET", "/v1/dashboard/session", None),
    ("GET", "/v1/fleet/session", None), ("GET", "/v1/fleet/nodes", None),
    ("GET", "/v1/fleet/app", None), ("GET", "/v1/fleet/app/password", None),
    ("POST", "/v1/fleet/pair", {}), ("DELETE", "/v1/fleet/nodes/n-000000000000", None),
    ("PUT", "/v1/fleet/layouts/fleet", {"device": "desk", "doc": {}}),
    ("GET", "/v1/settings", None), ("POST", "/v1/settings/plan", {}),
    ("GET", "/v1/settings/audit", None),
    ("GET", "/v1/config/backends", None), ("POST", "/v1/config/backends", {}),
    ("GET", "/v1/models", None), ("POST", "/v1/chat/completions", {"model": "m", "messages": []}),
    ("GET", "/v1/chat-app/session", None), ("GET", "/v1/chat-app/chats", None),
    ("GET", "/v1/conductor/settings", None), ("GET", "/v1/library", None),
    ("POST", "/v1/cache/peek", {}),
    ("GET", "/v1/auth/session", None), ("POST", "/v1/auth/session", {"username": "a",
                                                                      "password": "b"}),
    ("GET", "/v1/auth/sessions", None), ("GET", "/v1/auth/users", None),
    ("GET", "/v1/no-such-route", None), ("GET", "/plugin-panels/x/y", None),
    ("GET", "/chat-engine/health", None), ("GET", "/s/abcdefabcdef/v1/library", None),
)
CREDS = ("local", "lan", "lan-bearer", "lan-bad-bearer", "lan-cookie")


def _cookie_of(px, setup: str) -> str | None:
    if setup == "open":
        return None
    secret = PW if setup == "password" else TOKEN
    st, h, _ = px.request("POST", "/v1/fleet/session", {"token": secret}, headers=LAN)
    c = (h.get("Set-Cookie") or "").split(";", 1)[0]
    k, _, v = c.partition("=")
    return v if st == 204 and k == auth_mod.COOKIE else None


def _headers(cred: str, cookie: str | None) -> dict:
    if cred == "local":
        return {}
    h = dict(LAN)
    if cred == "lan-bearer":
        h["Authorization"] = f"Bearer {TOKEN}"
    elif cred == "lan-bad-bearer":
        h["Authorization"] = "Bearer not-the-token-0123456789abcdef0123456"
    elif cred == "lan-cookie" and cookie:
        h["Cookie"] = f"{auth_mod.COOKIE}={cookie}"
    return h


def _matrix(extra_env: dict) -> dict:
    out = {}
    for name, (env, login, password) in SETUPS.items():
        with Proxy(env={**env, **extra_env}, login=login) as px:
            if password:
                adminpw.Password(os.path.join(px.dir, "admin.json")).set(PW)
            cookie = _cookie_of(px, name)
            for cred in CREDS:
                for method, path, body in ROUTES:
                    st, _, raw = px.request(method, path, body, headers=_headers(cred, cookie))
                    key = f"{name} {cred} {method} {path}"
                    out[key] = st
                    if path == "/health":
                        out[key + " keys"] = sorted(json.loads(raw))
    return out


def test_matrix_unchanged():
    with open(GOLDEN) as f:
        want = json.load(f)
    for extra in ({}, {"TANDEM_AUTH": "legacy"}, {"TANDEM_AUTH": "no-such-mode"}):
        got = _matrix(extra)
        diff = {k: (want.get(k), v) for k, v in got.items() if want.get(k) != v}
        assert not diff and set(got) == set(want), (extra, sorted(diff.items())[:20])


def test_nothing_new_exists():
    with Proxy() as px:
        assert type(proxy_app.STATE["auth"]) is auth_mod.Auth
        con = sqlite3.connect(px.svc.cfg.path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert not tables & {"accounts", "sessions", "group_roles", "api_tokens"}, tables
        for path in ("/v1/auth/session", "/v1/auth/sessions", "/v1/auth/users"):
            assert px.get(path)[0] == 404


if __name__ == "__main__":
    if "--write" in sys.argv:
        with open(GOLDEN, "w") as f:
            json.dump(_matrix({}), f, indent=0, sort_keys=True)
            f.write("\n")
        print(f"wrote {GOLDEN}")
    else:
        run(globals())
