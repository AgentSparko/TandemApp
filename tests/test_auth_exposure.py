"""SRV-871: the exposure switches per protocol, the `auth` and `api` settings scopes and the health
findings of the sign-in and the API, with TANDEM_AUTH=accounts. "Another device" is a request
through a proxy's header (X-Real-IP), which is never this machine (proxy/authlocal.py
this_machine). The Messages API's cells go to its real route (SRV-879): a refusal in its shape,
or the route's answer, with the token in Authorization or in x-api-key.

  Gherkin "API exposure":
  1  Off by default: both switches this-machine; another device with a token: 403
     api_not_exposed; the page's Playground: 200                    test_off_by_default
  2  Switched on: the owner sets openai.exposure network; the same call answers 200; /v1/messages
     from that device still 403 (the Messages shape)                test_switched_on
  3  Never tokenless: openai network, another device without a token: 401
                                                                    test_never_tokenless
  4  On, but unreachable: listening on 127.0.0.1 and anthropic network: exposure_unreachable with
     "listening on this machine only" on the health page             test_on_but_unreachable
  5  Only the owner: an admin, or any token, changing a switch: 403  test_only_the_owner
  6  Viewers and inference tokens: off on a fresh controller; an admin switches it on, a viewer
     makes an inference token, an audit row holds the old and the new value; the admin still
     cannot change openai.exposure                                   test_viewers_and_inference_tokens
  7  Exposure after a migration with legacy keys: openai network, anthropic this-machine,
     tokens_need_review with the number, cleared by confirming or revoking each
                                                                    test_exposure_after_a_migration
  8  The imported admin token: legacy_admin_token until it is revoked or expires
                                                                    test_the_imported_admin_token

  Acceptance criteria:
  a  the matrix protocol x switch x caller (page, service, local token, remote token, remote no
     token; the tokens also in x-api-key)                            test_the_matrix
  b  each finding of design 6.3 fires and clears (idp_unreachable comes with the providers)
                                                                    test_each_finding_fires_and_clears
  c  a change of either scope is an audit row with the old and the new value
                                                                    test_audit_rows_old_and_new
  d  api.tokens.viewers_may_create defaults to off; an admin switches it, an operator or a token
     cannot                                                          test_viewers_may_create_rights
  e  legacy keys: openai network and anthropic this-machine; none: both this-machine
                                                                    test_exposure_after_a_migration
  f  legacy_admin_token is in the codes (Python and contract), fires while the imported admin
     token works and clears when it is revoked or expires           test_the_imported_admin_token
  And: the auth scope holds auth.local_mode; the exposure, the listen address and the provider
  need the owner's password (never from local mode); api.listen writes TANDEM_HOST to the env
  file for the next restart and is locked on Kubernetes; a token reads neither scope; a change set
  or a restore through another route is refused the same way   test_scopes_rules
  A move carries none of either scope's settings: the export leaves them out, an import drops any
  a bundle holds                                                 test_a_move_carries_no_account_settings

    python tests/test_auth_exposure.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_auth_tokens as tt  # noqa: E402
from proxy import adminpw, enginecodes, fleethealth  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import exposure as exposure_mod  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

ON = tt.ON
PW = tt.PW
LAN = {"X-Real-IP": "192.0.2.77"}
CHAT = {"model": "small-1", **tt.CHAT}
MSG = {"model": "small-1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
j, bearer, make, sign_in, new_token = tt.j, tt.bearer, tt.make, tt.sign_in, tt.new_token


def accounts():
    return proxy_app.STATE["accounts"]


def codes(a=None) -> dict:
    return {f["code"]: f.get("facts", {}) for f in (a or accounts()).findings()}


def bare(a, bind: str = "127.0.0.1") -> None:
    a.local.bind = bind
    a.local.exists = lambda p: False


def setup(px, bind: str = "127.0.0.1") -> dict:
    """An owner with a password, the process bound at `bind`: local mode off."""
    make("own", "owner")
    bare(accounts(), bind)
    return sign_in(px, "own")


def change(px, who: dict, scope: str, sets: dict, unsets=()) -> tuple[int, dict]:
    """A plan and its apply on /v1/settings/{scope}; the refusal of either as it came."""
    body = {"set": sets}
    if unsets:
        body["unset"] = list(unsets)
    st, plan = j(px.post(f"/v1/settings/{scope}/plan", body, who))
    if st != 200:
        return st, plan
    st, out = j(px.post(f"/v1/settings/{scope}/apply", {**body, "confirm": plan["confirm"]}, who))
    return st, out


def value(px, who: dict, scope: str, k: str):
    st, out = j(px.get(f"/v1/settings/{scope}", who))
    assert st == 200, out
    return next(v for v in out["values"] if v["key"] == k)


def chat(px, headers: dict) -> int:
    return px.post("/v1/chat/completions", CHAT, headers)[0]


def engine():
    return fake(models=["small-1"], cache=False)


# ---------------------------------------------------------------------------- Gherkin 1, 2, 3

def test_off_by_default():
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = setup(px)
            tok, _ = new_token(px, own, label="t", scopes=["inference:openai",
                                                            "inference:anthropic"])
            assert value(px, own, "api", "openai.exposure")["value"] == "this-machine"
            assert value(px, own, "api", "anthropic.exposure")["value"] == "this-machine"
            st, out = j(px.post("/v1/chat/completions", CHAT, {**bearer(tok), **LAN}))
            assert st == 403 and out["error"]["code"] == "api_not_exposed", out
            assert out["error"]["message"] == exposure_mod.NOT_EXPOSED
            # the page's Playground, signed in, from that device: 200
            assert chat(px, {**own, **LAN}) == 200
            # the same token on this machine: 200
            assert chat(px, bearer(tok)) == 200
    finally:
        srv.shutdown()
    return "403 api_not_exposed from another device; the page and this machine 200"


def test_switched_on():
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = setup(px)
            tok, _ = new_token(px, own, label="t", scopes=["inference:openai",
                                                            "inference:anthropic"])
            st, out = change(px, own, "api", {"openai.exposure": "network"})
            assert st == 200, out
            assert chat(px, {**bearer(tok), **LAN}) == 200
            st, out = j(px.post("/v1/messages", MSG, {**bearer(tok), **LAN}))
            assert st == 403 and out == {"type": "error", "error": {
                "type": "permission_error", "message": exposure_mod.NOT_EXPOSED}}, out
            # the models list: open with one exposed protocol the token holds
            assert px.get("/v1/models", {**bearer(tok), **LAN})[0] == 200
            only_a, _ = new_token(px, own, label="a", scopes=["inference:anthropic"])
            assert px.get("/v1/models", {**bearer(only_a), **LAN})[0] == 403
    finally:
        srv.shutdown()
    return "openai 200 from another device, the Messages API still 403 in its own shape"


def test_never_tokenless():
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = setup(px)
            assert change(px, own, "api", {"openai.exposure": "network",
                                           "anthropic.exposure": "network"})[0] == 200
            for path, body in (("/v1/chat/completions", CHAT), ("/v1/messages", MSG)):
                assert px.post(path, body, LAN)[0] == 401, path
                assert px.post(path, body, {**LAN, **bearer("tdm-0123abcd-" + "x" * 43)})[0] \
                    == 401
            assert px.get("/v1/models", LAN)[0] == 401
    finally:
        srv.shutdown()
    return "401 without a token whatever the switch"


# ---------------------------------------------------------------------------- Gherkin 4

def test_on_but_unreachable():
    with Proxy(env=ON) as px:
        own = setup(px, "127.0.0.1")
        new_token(px, own, label="t", scopes=["inference:anthropic"])
        assert change(px, own, "api", {"anthropic.exposure": "network"})[0] == 200
        f = codes()
        assert f["exposure_unreachable"] == {"protocol": "Anthropic",
                                             "reason": "listening on this machine only"}, f
        # on the health page: the controller's finding with its sentence
        fd = fleethealth.finding("exposure_unreachable", **f["exposure_unreachable"])
        assert fd["sentence"] == ("The Anthropic API is switched on for other devices, but they "
                                  "cannot reach it: listening on this machine only."), fd
        # bound beyond loopback: it clears
        accounts().local.bind = "0.0.0.0"
        assert "exposure_unreachable" not in codes()
        # on Kubernetes the chart says what its ingress routes
        a = accounts()
        a.env["TANDEM_INGRESS_API"] = "openai"
        assert codes()["exposure_unreachable"]["reason"] == "the ingress does not route it"
        a.env["TANDEM_INGRESS_API"] = "openai,anthropic"
        assert "exposure_unreachable" not in codes()
    return "listening on this machine only; the ingress's word on Kubernetes"


# ---------------------------------------------------------------------------- Gherkin 5

def test_only_the_owner():
    with Proxy(env=ON) as px:
        own = setup(px)
        make("adm", "admin")
        make("ops", "operator")
        adm, ops = sign_in(px, "adm"), sign_in(px, "ops")
        tok, _ = new_token(px, own, label="m", scopes=["settings:read", "settings:write"],
                           days="never")
        for who in (adm, ops):
            st, out = change(px, who, "api", {"openai.exposure": "network"})
            assert st == 403 and out["error"]["type"] == "owner_required", out
        st, out = change(px, bearer(tok), "api", {"openai.exposure": "network"})
        assert st == 403 and out["error"]["type"] == "token_refused", out
        # the change set route and a restore are refused the same way
        st, out = j(px.post("/v1/settings/plan", {"changes": [
            {"scope": "api", "set": {"openai.exposure": "network"}}]}, adm))
        assert st == 403 and out["error"]["type"] == "owner_required", out
        st, out = j(px.post("/v1/settings/plan", {"changes": [
            {"scope": "auth", "set": {"chat_min_role": "viewer"}}]}, bearer(tok)))
        assert st == 403 and out["error"]["type"] == "token_refused", out
        assert change(px, own, "api", {"openai.exposure": "network"})[0] == 200
        assert change(px, own, "api", {"openai.exposure": "this-machine"})[0] == 200
        st, vs = j(px.get("/v1/settings/api/versions", own))
        net = next(v["id"] for v in vs["versions"]
                   if v["values"].get("openai.exposure") == "network")
        st, out = j(px.post("/v1/settings/api/restore", {"version": net}, adm))
        assert st == 403 and out["error"]["type"] == "owner_required", out
        assert value(px, own, "api", "openai.exposure")["value"] == "this-machine"
    return "owner only; an admin, an operator and a token 403, on every route"


# ---------------------------------------------------------------------------- Gherkin 6

def test_viewers_and_inference_tokens():
    with Proxy(env=ON) as px:
        own = setup(px)
        make("adm", "admin")
        make("view", "viewer")
        adm, view = sign_in(px, "adm"), sign_in(px, "view")
        assert value(px, adm, "api", "tokens.viewers_may_create")["value"] == "off"
        st, out = j(px.post("/v1/auth/tokens", {"label": "v", "scopes": ["inference:openai"]},
                            view))
        assert st == 403 and out["error"]["permission"] == "tokens:inference", out
        st, out = change(px, adm, "api", {"tokens.viewers_may_create": "on"})
        assert st == 200, out
        new_token(px, view, label="v", scopes=["inference:openai"])
        row = _audit(px, "api.tokens.viewers_may_create")
        assert row["who"] == "adm" and row["detail"] == {"from": "off", "to": "on"}, row
        st, out = change(px, adm, "api", {"openai.exposure": "network"})
        assert st == 403 and out["error"]["type"] == "owner_required", out
        assert own
    return "off; an admin turns it on, a viewer makes one, the audit row says off -> on"


def _audit(px, key: str) -> dict:
    rows = [r for r in px.svc.cfg.audit(500) if r["object"] == "setting" and
            r["object_id"] == key]
    assert rows, key
    return rows[0]


# ---------------------------------------------------------------------------- Gherkin 7, AC e

def test_exposure_after_a_migration():
    srv, port = engine()
    try:
        # the release before's state with two legacy keys (a users row and TANDEM_API_KEYS)
        old = tt.legacy_state(port)
        try:
            new = Proxy(env={**ON, **tt.env_for(port, TANDEM_API_KEYS=f"laptop:{tt.KEY_ENV}")})
            tt.copy_state(old, new)
        finally:
            old.__exit__(None, None, None)
        with new as px:
            px.sync()
            own = setup(px)
            a = accounts()
            assert value(px, own, "api", "openai.exposure") == {
                "key": "openai.exposure", "value": "network", "set": False,
                "source": "migration"}
            assert value(px, own, "api", "anthropic.exposure")["value"] == "this-machine"
            assert codes()["tokens_need_review"] == {"n": 2}
            f = tt.controller_findings(px, own)
            assert f["tokens_need_review"]["facts"] == {"n": 2}, f
            # the clients keep working from other devices
            for key in (tt.KEY_DB, tt.KEY_ENV):
                assert px.post("/v1/chat/completions", CHAT, {**bearer(key), **LAN})[0] == 200
            rows = {r["label"]: r for r in a.tokens.all() if r["source"] in ("users", "env")}
            st, _ = j(px.request("PATCH", f"/v1/auth/tokens/{rows['phone']['id']}",
                                 {"needs_review": False}, own))
            assert st == 200 and codes()["tokens_need_review"] == {"n": 1}
            assert j(px.request("DELETE", f"/v1/auth/tokens/{rows['laptop']['id']}", None,
                                own))[0] == 200
            assert "tokens_need_review" not in codes()
    finally:
        srv.shutdown()
    # an install with none: both this-machine
    with Proxy(env=ON) as px:
        own = setup(px)
        assert value(px, own, "api", "openai.exposure") == {
            "key": "openai.exposure", "value": "this-machine", "set": False, "source": "default"}
        assert value(px, own, "api", "anthropic.exposure")["value"] == "this-machine"
    return "openai network after legacy keys, the review count to zero; else this-machine"


# ---------------------------------------------------------------------------- Gherkin 8, AC f

def test_the_imported_admin_token():
    assert "legacy_admin_token" in enginecodes.CATALOG
    doc = json.load(open(os.path.join(ROOT, "contracts", "fleet-v1", "engine-codes.json")))
    assert "legacy_admin_token" in doc["codes"] and "legacy_admin_token" in doc["findings"]
    for how in ("revoke", "expire"):
        with Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": tt.ADMIN_TOKEN}) as px:
            adminpw.Password(os.path.join(px.dir, "admin.json")).set(PW["own"])
            from proxy import authlegacy
            a = accounts()
            authlegacy.migrate(a, px.dir)
            a.tokens.import_legacy(a.env, first_owner=a.store.first_owner())
            row = a.tokens.admin_token_live()
            assert row is not None and "legacy_admin_token" in codes()
            assert px.get("/v1/settings", bearer(tt.ADMIN_TOKEN))[0] == 200
            if how == "revoke":
                a.tokens.revoke(row["id"], actor="test")
            else:
                with a.cfg._lock:
                    a.cfg._conn.execute("UPDATE api_tokens SET expires_at = ? WHERE id = ?",
                                        (a.store.now_ms() - 1, row["id"]))
                    a.cfg._conn.commit()
            assert "legacy_admin_token" not in codes()
            assert px.get("/v1/settings", bearer(tt.ADMIN_TOKEN))[0] == 401
    return "fires while it works; gone after a revoke, and after its 30 days"


# ---------------------------------------------------------------------------- AC a

def test_the_matrix():
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = setup(px)
            a = accounts()
            tok, _ = new_token(px, own, label="t", scopes=["inference:openai",
                                                            "inference:anthropic"])
            a.write_cli_token()
            svc = tokens_mod.read_cli_token(px.dir)       # svc:cli: both inference scopes
            callers = {
                "page": {**own, **LAN},
                "service": {**bearer(svc), **LAN},
                "local token": bearer(tok),
                "remote token": {**bearer(tok), **LAN},
                "local token in x-api-key": {"x-api-key": tok},
                "remote token in x-api-key": {"x-api-key": tok, **LAN},
                "remote no token": dict(LAN),
            }
            routes = {"openai": ("/v1/chat/completions", CHAT), "anthropic": ("/v1/messages", MSG)}
            cells = 0
            for proto, (path, body) in routes.items():
                for switch in exposure_mod.VALUES:
                    assert change(px, own, "api", {f"{proto}.exposure": switch})[0] == 200
                    for caller, h in callers.items():
                        st, out = j(px.post(path, body, h))
                        if caller == "remote no token":
                            want = 401
                        elif caller.startswith("remote token") and switch == "this-machine":
                            want = 403
                        else:
                            want = 200
                        assert st == want, (proto, switch, caller, st, out)
                        if proto == "anthropic":
                            # the real route: its answer, or a refusal in its shape
                            assert out["type"] == ("message" if want == 200 else "error"), out
                            if want == 403:
                                assert out["error"] == {"type": "permission_error",
                                                        "message": exposure_mod.NOT_EXPOSED}
                        else:
                            assert ("choices" in out) == (want == 200), out
                        cells += 1
            assert cells == 28
    finally:
        srv.shutdown()
    return "2 protocols x 2 switches x 7 callers"


# ---------------------------------------------------------------------------- AC b

def test_each_finding_fires_and_clears():
    seen = set()

    def fired(code, f=None):
        assert code in (f if f is not None else codes()), (code, codes())
        seen.add(code)

    def cleared(code):
        assert code not in codes(), (code, codes())

    # auth_setup_pending: bound beyond loopback, no owner
    with Proxy(env=ON) as px:
        a = accounts()
        bare(a, "0.0.0.0")
        fired("auth_setup_pending")
        make("own", "owner")
        cleared("auth_setup_pending")
    # auth_owner_no_password and local_mode_proxy_seen: local mode
    with Proxy(env=ON) as px:
        a = accounts()
        bare(a)
        fired("auth_owner_no_password")
        assert px.get("/v1/fleet/nodes", {"X-Forwarded-For": "192.0.2.4"})[0] == 401
        fired("local_mode_proxy_seen")
        make("own", "owner")
        cleared("auth_owner_no_password")
        cleared("local_mode_proxy_seen")
    # legacy_env_set, legacy_env_invalid, legacy_users_from_unreadable: each variable, and its
    # absence at the next start
    for env, code in (({"TANDEM_API_KEYS": "phone:tdm-a-legacy-key-of-sixteen+"},
                       "legacy_env_set"),
                      ({"TANDEM_API_KEYS": "this is not a key list"}, "legacy_env_invalid"),
                      ({"TANDEM_API_USERS_FROM": "/nonexistent/config.sqlite3"},
                       "legacy_users_from_unreadable")):
        with Proxy(env={**ON, **env}):
            fired(code)
        with Proxy(env=ON):
            cleared(code)
    # bootstrap_secret_left: an owner and the setup Secret still mounted
    with tempfile.TemporaryDirectory() as d:
        secret = os.path.join(d, "code")
        with open(secret, "w") as f:
            f.write("ABCDEFGHJKMNPQRSTVWX\n")
        with Proxy(env={**ON, "TANDEM_SETUP_CODE_FILE": secret}):
            make("own", "owner")
            fired("bootstrap_secret_left")
            os.unlink(secret)
            cleared("bootstrap_secret_left")
    # tokens_need_review, legacy_admin_token: test_exposure_after_a_migration and
    # test_the_imported_admin_token; once more here for the walk
    with Proxy(env={**ON, "QSE_METRICS_TOKEN": "a-metrics-token-of-the-release-before-0003"}):
        a = accounts()
        fired("tokens_need_review")
        a.tokens.confirm(next(r["id"] for r in a.tokens.all() if r["needs_review"]), actor="t")
        cleared("tokens_need_review")
    with Proxy(env={**ON, "TANDEM_ADMIN_TOKEN": tt.ADMIN_TOKEN}) as px:
        adminpw.Password(os.path.join(px.dir, "admin.json")).set(PW["own"])
        from proxy import authlegacy
        a = accounts()
        authlegacy.migrate(a, px.dir)
        a.tokens.import_legacy(a.env, first_owner=a.store.first_owner())
        fired("legacy_admin_token")
        a.tokens.revoke(a.tokens.admin_token_live()["id"], actor="t")
        cleared("legacy_admin_token")
    # the exposure findings and token_expiring
    with Proxy(env=ON) as px:
        own = setup(px, "0.0.0.0")
        a = accounts()
        assert change(px, own, "api", {"openai.exposure": "network"})[0] == 200
        assert codes()["exposure_no_tokens"] == {"protocol": "OpenAI"}
        fired("exposure_no_tokens")
        tok, view = new_token(px, own, label="t", scopes=["inference:openai"], days=3)
        cleared("exposure_no_tokens")
        assert codes()["token_expiring"] == {"n": 1}
        fired("token_expiring")
        a.local.bind = "127.0.0.1"
        fired("exposure_unreachable")
        a.local.bind = "0.0.0.0"
        cleared("exposure_unreachable")
        # a token from another device on plain http; over https (a trusted front) it is not
        assert px.get("/v1/models", {**bearer(tok), **LAN, "X-Forwarded-Proto": "https"})[0] \
            == 200
        cleared("exposure_plain_http")
        assert px.get("/v1/models", {**bearer(tok), **LAN})[0] == 200
        fired("exposure_plain_http")
        a.exposure.clock = lambda: a.exposure.plain_http + exposure_mod.PLAIN_HTTP_S + 1
        cleared("exposure_plain_http")
        j(px.request("DELETE", f"/v1/auth/tokens/{view['id']}", None, own))
        cleared("token_expiring")
        fired("exposure_no_tokens")
        assert change(px, own, "api", {"openai.exposure": "this-machine"})[0] == 200
        cleared("exposure_no_tokens")
    want = {"auth_setup_pending", "auth_owner_no_password", "local_mode_proxy_seen",
            "legacy_env_set", "legacy_env_invalid", "legacy_users_from_unreadable",
            "tokens_need_review", "exposure_no_tokens", "exposure_unreachable",
            "exposure_plain_http", "token_expiring", "bootstrap_secret_left",
            "legacy_admin_token"}
    assert seen == want, want ^ seen
    for code in want:
        assert code in enginecodes.CATALOG and code in fleethealth.FINDINGS, code
    # design 6.3's last row waits for the identity providers (later)
    assert "idp_unreachable" not in enginecodes.CATALOG
    return f"{len(seen)} findings fire and clear; idp_unreachable comes with the providers"


# ---------------------------------------------------------------------------- AC c

def test_audit_rows_old_and_new():
    with Proxy(env=ON) as px:
        own = setup(px)
        assert change(px, own, "auth", {"chat_min_role": "viewer",
                                         "session.idle_days": 7})[0] == 200
        assert change(px, own, "api", {"tokens.default_days": 30})[0] == 200
        assert _audit(px, "auth.chat_min_role")["detail"] == {"from": "operator", "to": "viewer"}
        assert _audit(px, "auth.session.idle_days")["detail"] == {"from": 30, "to": 7}
        assert _audit(px, "api.tokens.default_days")["detail"] == {"from": 90, "to": 30}
        assert _audit(px, "api.tokens.default_days")["who"] == "own"
        # unset: back to the default, with both values
        assert change(px, own, "auth", {}, unsets=["chat_min_role"])[0] == 200
        assert _audit(px, "auth.chat_min_role")["detail"] == {"from": "viewer", "to": "operator"}
        # the readers see it at once
        assert accounts().policy.idle_days == 7
    return "each key: one row, the old and the new value"


# ---------------------------------------------------------------------------- AC d

def test_viewers_may_create_rights():
    with Proxy(env=ON) as px:
        own = setup(px)
        make("adm", "admin")
        make("ops", "operator")
        adm, ops = sign_in(px, "adm"), sign_in(px, "ops")
        tok, _ = new_token(px, own, label="m", scopes=["settings:read", "settings:write"])
        assert value(px, own, "api", "tokens.viewers_may_create")["value"] == "off"
        assert not accounts().policy.viewers_may_create
        st, out = change(px, ops, "api", {"tokens.viewers_may_create": "on"})
        assert st == 403 and out["error"]["type"] == "admin_required", out
        st, out = change(px, bearer(tok), "api", {"tokens.viewers_may_create": "on"})
        assert st == 403, out
        assert change(px, adm, "api", {"tokens.viewers_may_create": "on"})[0] == 200
        assert accounts().policy.viewers_may_create
        assert change(px, own, "api", {"tokens.viewers_may_create": "off"})[0] == 200
        assert not accounts().policy.viewers_may_create
    return "off; an admin and an owner switch it; an operator and a token cannot"


# ---------------------------------------------------------------------------- the move

def test_a_move_carries_no_account_settings():
    """Only the highest role changes the auth and api settings, so a move (another controller's
    export, a take by an admin's trust token) brings none: the export leaves them out, and an
    import drops any a bundle holds."""
    import base64
    import sqlite3
    from proxy import config as config_mod
    from proxy import ctlmove
    with Proxy(env=ON) as px:
        own = setup(px)
        assert change(px, own, "api", {"openai.exposure": "network"})[0] == 200
        assert change(px, own, "auth", {"local_mode": "off"})[0] == 200
        px.svc.cfg.set_setting("app.marker", "kept", who="test")
        bundle = ctlmove.export(px.svc.cfg)
    d = tempfile.mkdtemp(prefix="move-settings-")
    raw = os.path.join(d, "b.sqlite3")
    with open(raw, "wb") as f:
        f.write(base64.b64decode(bundle["db"]))
    con = sqlite3.connect(raw)
    keys = {r[0] for r in con.execute("SELECT key FROM settings")}
    assert "app.marker" in keys and not any(k.startswith(("auth.", "api.")) for k in keys), keys
    # a bundle that holds them anyway (an older or a crafted export)
    for k, v in (("api.openai.exposure", "network"), ("auth.local_mode", "off"),
                 ("api.tokens.viewers_may_create", "on")):
        con.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, 1)",
                    (k, json.dumps(v)))
    con.commit()
    con.close()
    with open(raw, "rb") as f:
        bundle["db"] = base64.b64encode(f.read()).decode()
    cfg = config_mod.ConfigDB(os.path.join(d, "config.sqlite3"), env={}, use_keychain=False).open()
    try:
        ctlmove.import_bundle(cfg, bundle, os.path.join(d, "c.pem"), os.path.join(d, "k.pem"))
        assert cfg.setting("app.marker") == "kept"
        for k in ("api.openai.exposure", "auth.local_mode", "api.tokens.viewers_may_create"):
            assert cfg.setting(k) is None, k
    finally:
        cfg.close()
    return "left out of the export, dropped from an import"


# ---------------------------------------------------------------------------- the rest

def test_scopes_rules():
    # local mode: the owner `local` changes ordinary keys, never what lets other devices in
    with Proxy(env=ON) as px:
        a = accounts()
        bare(a)
        assert a.local.on()
        local = {"Origin": f"http://127.0.0.1:{px.port}", "Host": f"127.0.0.1:{px.port}"}
        st, out = change(px, local, "auth", {"local_mode": "off"})
        assert st == 200, out
        assert not a.local.on() and a.local.why_not() == "auth.local_mode is off"
        a.cfg.delete_setting("auth.local_mode", who="test")
        assert a.local.on()
        proxy_app.STATE["settings"].scopes["api"].env_file = os.path.join(px.dir, "app.env")
        for scope, k, v in (("api", "openai.exposure", "network"), ("api", "listen", "0.0.0.0"),
                            ("auth", "provider", "local")):
            st, out = change(px, local, scope, {k: v})
            assert st == 409 and out["error"]["type"] == "password_first", (k, st, out)
    # api.listen: TANDEM_HOST in the env file, for the next restart
    with Proxy(env=ON) as px:
        own = setup(px)
        env_file = os.path.join(px.dir, "app.env")
        with open(env_file, "w") as f:
            f.write("TANDEM_HOST=127.0.0.1\nTANDEM_PORT=8080\n")
        os.chmod(env_file, 0o600)
        api = proxy_app.STATE["settings"].scopes["api"]
        assert value(px, own, "api", "listen")["value"] == "127.0.0.1"
        # without an env file there is nowhere to write it: locked, saying so
        st, out = j(px.post("/v1/settings/api/plan", {"set": {"listen": "0.0.0.0"}}, own))
        assert st == 422 and out["error"]["type"] == "locked" and "env file" in \
            out["error"]["message"], out
        api.env_file = env_file
        st, plan = j(px.post("/v1/settings/api/plan", {"set": {"listen": "0.0.0.0"}}, own))
        assert st == 200 and plan["restart_needed"] and plan["restart"] == "app", plan
        assert any("next restart" in w for p in plan["parts"] for w in p["warnings"]), plan
        st, out = j(px.post("/v1/settings/api/apply", {"set": {"listen": "0.0.0.0"},
                                                       "confirm": plan["confirm"]}, own))
        assert st == 200, out
        text = open(env_file).read()
        assert "TANDEM_HOST=0.0.0.0\n" in text and "TANDEM_PORT=8080" in text and \
            text.count("TANDEM_HOST") == 1, text
        assert oct(os.stat(env_file).st_mode & 0o777) == "0o600"
        st, out = change(px, own, "api", {"listen": "not an address!"})
        assert st == 422 and out["error"]["type"] == "bad_value", out
        # a token reads neither scope
        tok, _ = new_token(px, own, label="m", scopes=["settings:read", "settings:write"])
        for path in ("/v1/settings/api", "/v1/settings/auth/schema", "/v1/settings/api/versions"):
            st, out = j(px.get(path, bearer(tok)))
            assert st == 403 and out["error"]["type"] == "token_refused", (path, out)
        assert px.get("/v1/settings/app", bearer(tok))[0] == 200
        # both scopes in the overview, in their order
        st, ov = j(px.get("/v1/settings", own))
        assert ov["order"][:3] == ["app", "auth", "api"] and ov["contract_version"] == "1.2", ov
    # on Kubernetes the listen address is the chart's
    with Proxy(env={**ON, "TANDEM_DEPLOY": "kubernetes"}) as px:
        own = setup(px)
        accounts().local.exists = lambda p: False
        st, sch = j(px.get("/v1/settings/api/schema", own))
        k = next(x for x in sch["keys"] if x["key"] == "listen")
        assert not k["writable"] and "chart" in k["why"], k
        assert change(px, own, "api", {"listen": "0.0.0.0"})[0] == 422
    # switched off: neither scope exists
    with Proxy() as px:
        st, ov = j(px.get("/v1/settings"))
        assert "auth" not in ov["order"] and "api" not in ov["order"], ov
        assert px.get("/v1/settings/api")[0] == 404
    return "local mode, listen, tokens, order, Kubernetes and the switch off"


if __name__ == "__main__":
    run(globals())
