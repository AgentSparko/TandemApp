"""SEC-28: the matrix of SEC-22's switch-off golden, after the flip. Until the flip that golden
(tests/auth_legacy_golden.json, recorded on the tree before the accounts) held every answer of
the old sign-in with the switch off. The switch is gone, so its guarantee changes on purpose:
the same routes x credentials x setups now answer by one rule, and the golden stays as the
record of what each one answered before.

  1  the rule: a route with no permission is 403 unmapped_route; an open route (health, the
     page, the sign-in, the setup) answers for itself; every other route is the gate's 401 for
     every credential of before (this machine with no credential, another device, the admin
     token as a bearer, a bad bearer, the `qse_dash` cookie), in every setup (open, an admin
     token, the token with the dashboard login on, a password in admin.json); only the open
     setup on loopback is local mode, its own machine acting as the owner `local`; /health's
     full body only for that one                                test_matrix_by_rule
  2  against the golden: every entry that answered 2xx before to a credential of before, on a
     route that is not open, answers 401 now (local mode excepted); nothing that was 401 before
     opens; the golden's keys are the matrix's                   test_nothing_opens_that_was_closed

    python tests/test_auth_flip_matrix.py
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import int5_schema  # noqa: E402
from proxy import adminpw  # noqa: E402
from proxy import authz  # noqa: E402
from proxy_util import Proxy, run  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_legacy_golden.json")
TOKEN = "legacy-admin-token-0123456789abcdef0123"
PW = "a legacy controller password"
LAN = {"X-Real-IP": "192.0.2.77"}

SETUPS = {
    "open": ({}, False),
    "token": ({"TANDEM_ADMIN_TOKEN": TOKEN}, False),
    "token-login": ({"TANDEM_ADMIN_TOKEN": TOKEN, "TANDEM_DASHBOARD_LOGIN": "on"}, False),
    "password": ({}, True),
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


def _headers(cred: str, cookie: str | None) -> dict:
    if cred == "local":
        return {}
    h = dict(LAN)
    if cred == "lan-bearer":
        h["Authorization"] = f"Bearer {TOKEN}"
    elif cred == "lan-bad-bearer":
        h["Authorization"] = "Bearer not-the-token-0123456789abcdef0123456"
    elif cred == "lan-cookie" and cookie:
        h["Cookie"] = f"qse_dash={cookie}"
    return h


def _matrix() -> dict:
    """{"setup cred METHOD path": (status, error code or None)} plus /health's keys."""
    out = {}
    for name, (env, password) in SETUPS.items():
        px = Proxy(env=env)
        if password:
            adminpw.Password(os.path.join(px.dir, "admin.json")).set(PW)
        with px:
            # the cookie the release before made for this setup's secret (none while open)
            cookie = None
            if name != "open":
                cookie = int5_schema.cookie_like_int5(
                    px.dir, None if password else TOKEN)
            for cred in CREDS:
                for method, path, body in ROUTES:
                    st, _, raw = px.request(method, path, body, headers=_headers(cred, cookie))
                    try:
                        code = (json.loads(raw).get("error") or {}).get("code")
                    except (ValueError, AttributeError):
                        code = None
                    key = f"{name} {cred} {method} {path}"
                    out[key] = (st, code)
                    if path == "/health":
                        out[key + " keys"] = sorted(json.loads(raw))
    return out


def _kind(method: str, path: str) -> str:
    perm = authz.permission_for(method, path)
    return "unmapped" if perm is None else "open" if perm == authz.OPEN else "guarded"


def test_matrix_by_rule():
    got = _matrix()
    for key, val in got.items():
        if key.endswith(" keys"):
            local = key.startswith("open local ")
            assert (val != ["status"]) == local, (key, val)
            continue
        name, cred, method, path = key.split(" ", 3)
        st, code = val
        kind = _kind(method, path)
        if kind == "unmapped":
            assert (st, code) == (403, "unmapped_route"), (key, val)
        elif kind == "open":
            assert code != "sign_in_required", (key, val)
        elif name == "open" and cred == "local" and \
                authz.permission_for(method, path) != authz.SESSION_ADDRESS:
            # local mode: this machine is the owner `local`, nothing asks for a sign-in (a
            # session address is its own credential, which local mode does not give)
            assert st != 401 and code != "sign_in_required", (key, val)
        else:
            assert st == 401, (key, val)


def test_nothing_opens_that_was_closed():
    with open(GOLDEN) as f:
        before = json.load(f)
    got = _matrix()
    assert set(got) == set(before), sorted(set(got) ^ set(before))[:10]
    opened, closed = [], 0
    for key, was in before.items():
        if key.endswith(" keys"):
            continue
        name, cred, method, path = key.split(" ", 3)
        now = got[key][0]
        local = name == "open" and cred == "local"
        if _kind(method, path) == "guarded" and not local:
            if 200 <= was < 300:
                assert now == 401, (key, was, now)
                closed += 1
            if was == 401:
                assert now == 401, (key, was, now)
        if was in (401, 403) and not 200 <= was < 300 and 200 <= now < 300 and not local:
            opened.append(key)
    assert opened == [], opened
    # the old answers this closes: every open-by-default entry of before, and more
    assert closed > 100, closed


if __name__ == "__main__":
    run(globals())
