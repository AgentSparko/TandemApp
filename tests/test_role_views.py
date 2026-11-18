"""SRV-890: the views each role sees (proxy/authz.py SECTIONS, the `auth.views.<role>` settings)
and the role a new account gets (`auth.default_role`), with TANDEM_AUTH=accounts.

  Gherkin "An admin decides which pages each role sees":
  1  The server's sections are the page's menu (dashboard/src/lib/menu.ts MENU); every routed
     section is one of them; the menu-only ones route nothing; the shell's reads stay open
                                                                   test_the_route_map
  2  A viewer shown API and Models: /v1/auth/session names them; a hidden section's own route is
     403 view_hidden by session; a shown one is not                test_a_hidden_section_by_session
  3  An operator shown Chat alone: its API token is refused the same routes; its inference is not
                                                                   test_a_hidden_section_by_token
  4  The owner sees every page whatever the settings; an admin's own row applies to admins
                                                                   test_the_owner_sees_everything
  5  A viewer's Chat is `auth.chat_min_role`, not a second switch  test_a_viewers_chat
  6  A new account without a role gets `auth.default_role`; a wrong page name is 422; an
     operator may not change the views                             test_default_role_and_writes

    python tests/test_role_views.py
"""

from __future__ import annotations

import functools
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_auth_tokens as tt  # noqa: E402
from proxy import authz  # noqa: E402
from proxy_util import Proxy as _Proxy, run  # noqa: E402

Proxy = functools.partial(_Proxy, local=False)
ON = tt.ON
j, make, sign_in, bearer, new_token = tt.j, tt.make, tt.sign_in, tt.bearer, tt.new_token
MENU_TS = os.path.join(ROOT, "dashboard", "src", "lib", "menu.ts")
# the reads every page makes (the top bar, the health dot, the live panel): never a section's
SHELL = ("/v1/dashboard/summary", "/v1/dashboard/system", "/v1/fleet/alerts",
         "/v1/fleet/controller", "/v1/fleet/deployments", "/v1/fleet/health",
         "/v1/fleet/layouts/fleet", "/v1/fleet/metrics", "/v1/fleet/nodes", "/v1/models",
         "/v1/chat-app/capabilities", "/v1/conductor/meta", "/v1/settings/auth",
         "/v1/settings/api", "/v1/settings/plan", "/v1/auth/session", "/v1/auth/tokens")


def change(px, who: dict, sets: dict) -> tuple[int, dict]:
    st, plan = j(px.post("/v1/settings/auth/plan", {"set": sets}, who))
    if st != 200:
        return st, plan
    return j(px.post("/v1/settings/auth/apply", {"set": sets, "confirm": plan["confirm"]}, who))


def hidden(res) -> bool:
    st, out = j(res)
    return st == 403 and (out or {}).get("error", {}).get("code") == "view_hidden"


def test_the_route_map():
    src = open(MENU_TS).read()
    menu = src[src.index("export const MENU"):src.index("export const ACCOUNT_MENU")]
    ids = re.findall(r"id: '([a-z-]+)'", menu)
    assert tuple(ids) == authz.SECTIONS, (ids, authz.SECTIONS)
    routed = {s for _r, s in authz.SECTION_ROUTES if s}
    assert routed <= set(authz.SECTIONS) and not routed & set(authz.MENU_ONLY), routed
    assert set(authz.SECTIONS) == routed | set(authz.MENU_ONLY), routed
    for p in SHELL:
        assert authz.section_for(p) is None, p
    for p, s in (("/v1/conductor/profiles", "conductor"), ("/v1/fleet/host", "hardware"),
                 ("/v1/fleet/ledger/requests", "usage"), ("/v1/dashboard/logs/export", "dev"),
                 ("/v1/settings/app/plan", "app"), ("/v1/chat-app/chats", "chat"),
                 ("/v1/config/backends/x", "api"), ("/v1/fleet/weights", "models")):
        assert authz.section_for(p) == s, (p, authz.section_for(p))
    return f"{len(authz.SECTIONS)} sections = the menu; {len(routed)} routed, shell open"


def test_a_hidden_section_by_session():
    with Proxy(env=ON) as px:
        px.sync()
        make("own", "owner")
        make("view", "viewer")
        own = sign_in(px, "own")
        assert change(px, own, {"views.viewer": "api,models"})[0] == 200
        view = sign_in(px, "view")
        st, me = j(px.get("/v1/auth/session", view))
        assert st == 200 and me["views"] == ["models", "api"], me
        for p in ("/v1/fleet/host", "/v1/conductor/profiles", "/v1/dashboard/logs",
                  "/v1/fleet/ledger/requests", "/v1/settings/app"):
            assert hidden(px.get(p, view)), p
        for p in ("/v1/fleet/weights", "/v1/config/backends", "/v1/fleet/health",
                  "/v1/fleet/nodes"):
            assert not hidden(px.get(p, view)), p
    return "views [models, api]; 5 hidden routes 403, shown and shell routes not"


def test_a_hidden_section_by_token():
    with Proxy(env=ON) as px:
        px.sync()
        make("own", "owner")
        make("ops", "operator")
        own = sign_in(px, "own")
        assert change(px, own, {"views.operator": "chat"})[0] == 200
        ops = sign_in(px, "ops")
        tok, _ = new_token(px, ops, label="script", scopes=["fleet:read", "settings:read"])
        for p in ("/v1/fleet/host", "/v1/fleet/weights", "/v1/settings/engine"):
            assert hidden(px.get(p, bearer(tok))), p
        assert not hidden(px.get("/v1/fleet/nodes", bearer(tok)))
        assert not hidden(px.get("/v1/chat-app/chats", ops))
    return "an operator's token: hidden routes 403 view_hidden, the fleet list not"


def test_the_owner_sees_everything():
    with Proxy(env=ON) as px:
        px.sync()
        make("own", "owner")
        make("adm", "admin")
        own = sign_in(px, "own")
        for role in authz.VIEW_ROLES:
            assert change(px, own, {f"views.{role}": ""})[0] == 200
        st, me = j(px.get("/v1/auth/session", own))
        assert me["views"] is None, me
        assert not hidden(px.get("/v1/fleet/host", own))
        adm = sign_in(px, "adm")
        st, me = j(px.get("/v1/auth/session", adm))
        assert me["views"] == [], me
        assert hidden(px.get("/v1/fleet/host", adm))
        assert j(px.get("/v1/auth/users", adm))[0] == 200     # Users & access is never a view
    return "owner: every view; admin with none: 403 on a section, Users still 200"


def test_a_viewers_chat():
    with Proxy(env=ON) as px:
        px.sync()
        make("own", "owner")
        make("view", "viewer")
        own = sign_in(px, "own")
        view = sign_in(px, "view")
        st, me = j(px.get("/v1/auth/session", view))
        assert me["views"] is not None and "chat" not in me["views"], me
        assert change(px, own, {"chat_min_role": "viewer"})[0] == 200
        st, me = j(px.get("/v1/auth/session", view))
        assert "chat" in me["views"] and "app" in me["views"], me
        assert change(px, own, {"views.viewer": "api"})[0] == 200
        st, me = j(px.get("/v1/auth/session", view))
        assert me["views"] == ["chat", "api"], me
    return "a viewer's Chat follows chat_min_role, with or without a list"


def test_default_role_and_writes():
    with Proxy(env=ON) as px:
        px.sync()
        make("own", "owner")
        make("adm", "admin")
        make("ops", "operator")
        adm = sign_in(px, "adm")
        st, out = j(px.post("/v1/auth/users", {"username": "first"}, adm))
        assert st == 201 and out["user"]["role"] == "viewer", out
        assert change(px, adm, {"default_role": "operator"})[0] == 200
        st, out = j(px.post("/v1/auth/users", {"username": "second"}, adm))
        assert st == 201 and out["user"]["role"] == "operator", out
        st, out = change(px, adm, {"views.viewer": "chat,nope"})
        assert st == 422 and "nope" in json.dumps(out), (st, out)
        st, out = change(px, sign_in(px, "ops"), {"views.viewer": "chat"})
        assert st == 403, (st, out)
    return "no role: viewer, then operator; a wrong page 422; an operator 403"


if __name__ == "__main__":
    run(globals())
