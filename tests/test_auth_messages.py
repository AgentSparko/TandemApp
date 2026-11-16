"""The Messages API (SRV-879, SRV-881) with TANDEM_AUTH=accounts (SRV-867, SRV-871), and the same
seams with the switch off: the gate and the handler agree on the routes and the refusal shape,
a request is authorized once with either header, a token's model list holds on both paths, the
ledger row names the token and the protocol, the model list's shape follows the header that held
the token, and the thinking stash is the token's (a rotation keeps it).

  The gate and the handler                  test_the_gate_and_the_handler_agree
  Authorized once, either header            test_authorized_once_with_either_header
  A model the token may not use             test_a_model_the_token_may_not_use,
                                            test_a_model_the_token_may_not_use_passing_through
  The model list's shape, on and off        test_the_model_list_shape_with_accounts_on,
                                            test_the_model_list_shape_with_accounts_off
  The stash is the token's                  test_the_stash_is_the_tokens,
                                            test_the_stash_owner_of_a_principal

    python tests/test_auth_messages.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_anthropic  # noqa: E402
import test_auth_exposure as te  # noqa: E402
import test_auth_tokens as tt  # noqa: E402
from proxy import stash as stash_mod  # noqa: E402
from proxy.accounts import Principal  # noqa: E402
from proxy_util import FIXTURE_KEYS, Proxy, fake, run  # noqa: E402

ON = tt.ON
j = tt.j
bearer = tt.bearer
MSG = {"model": "small-1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
BACKEND_KEY = FIXTURE_KEYS[0]
FN = {"type": "function", "function": {"name": "get_weather", "description": "Weather of a city",
                                       "parameters": {"type": "object", "properties": {
                                           "city": {"type": "string"}}}}}


def engine():
    return fake(models=["small-1", "big-1"], cache=False)


def anth_env(port) -> dict:
    return {"TANDEM_BACKENDS": "anth", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
            "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}",
            "TANDEM_BACKEND_ANTH_KEY": BACKEND_KEY, "TANDEM_LOCAL_NODE": "off"}


def client_id(token: str) -> str:
    return "k:" + hashlib.sha256(token.encode()).hexdigest()[:12]


def gate_refused(px) -> None:
    """A row route the gate answered: no ledger row comes (Proxy.rows would wait for one)."""
    px.sent -= 1


def is_messages_error(st, h, out, status: int, etype: str) -> None:
    doc = json.loads(out)
    assert st == status and doc["type"] == "error" and doc["error"]["type"] == etype, (st, doc)
    assert h.get("request-id", "").startswith("req_"), h


# ---------------------------------------------------------------------------- the gate

def test_the_gate_and_the_handler_agree():
    """The gate takes /v1/messages and every path under it, as the handler does: a token reaches
    the handler's 404 for a path it does not serve; every refusal there is in the Messages API's
    shape with a request-id, the gate's own included."""
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = te.setup(px)
            tok, _ = tt.new_token(px, own, label="t", scopes=["inference:anthropic"])
            for h in ({"x-api-key": tok}, bearer(tok)):
                st, hd, out = px.post("/v1/messages", MSG, h)
                assert st == 200 and json.loads(out)["type"] == "message", (st, out)
                st, hd, out = px.post("/v1/messages/batches", {"requests": []}, h)
                is_messages_error(st, hd, out, 404, "not_found_error")
                assert "not served" in json.loads(out)["error"]["message"]
            # no credential: the gate's 401, in the route's shape
            st, hd, out = px.post("/v1/messages", MSG, te.LAN)
            gate_refused(px)
            is_messages_error(st, hd, out, 401, "authentication_error")
            assert hd.get("WWW-Authenticate", "").startswith("Bearer"), hd
            # a token the gate refuses: its shape and a request-id too
            st, hd, out = px.post("/v1/messages", MSG, {"x-api-key": "tdm-0123abcd-" + "x" * 43})
            gate_refused(px)
            is_messages_error(st, hd, out, 401, "authentication_error")
            only_openai, _ = tt.new_token(px, own, label="o", scopes=["inference:openai"])
            st, hd, out = px.post("/v1/messages/count_tokens", MSG, bearer(only_openai))
            gate_refused(px)
            is_messages_error(st, hd, out, 403, "permission_error")
            # the OpenAI routes keep their own shape
            st, out = j(px.post("/v1/chat/completions", {**MSG}, te.LAN))
            gate_refused(px)
            assert st == 401 and out["error"]["type"] == "unauthorized", out
    finally:
        srv.shutdown()
    return "200 and 404 from the handler; 401 and 403 from the gate, all in the Messages shape"


def test_authorized_once_with_either_header():
    """The gate resolves the token once; the handler's rebuilt request is authorized by the
    gate's principal, never by a header; the row names the token, the protocol and the client id
    the token's hash gave; neither header goes upstream."""
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = te.setup(px)
            tok, view = tt.new_token(px, own, label="t", scopes=["inference:anthropic"])
            acc = tt.accounts()
            resolved, authorized = [], []
            resolver, authorize = acc.token_resolver, px.svc.authorize

            def counting_resolver(headers):
                resolved.append(1)
                return resolver(headers)

            def counting_authorize(handler, headers=()):
                got = authorize(handler, headers)
                authorized.append(got)
                return got
            acc.token_resolver = counting_resolver
            px.svc.authorize = counting_authorize
            for h in ({"x-api-key": tok}, bearer(tok), {"x-api-key": tok, **bearer(tok)}):
                del resolved[:], authorized[:]
                st, out = j(px.post("/v1/messages", MSG, h))
                assert st == 200 and out["type"] == "message", (st, out)
                assert len(resolved) == 1, (h.keys(), resolved)
                assert authorized == [(True, "own")], authorized
                up = tt.upstream(srv)
                assert "x-api-key" not in up and tok not in json.dumps(up), up
                row = px.rows()[-1]
                assert (row["token_id"], row["api"], row["endpoint"]) == \
                    (view["id"], "anthropic", "messages"), row
                assert row["client_id"] == client_id(tok) and row["user"] == "own", row
            # an OpenAI route on the same controller: api openai
            st, _ = j(px.post("/v1/chat/completions", {**MSG}, bearer(tok)))
            gate_refused(px)
            assert st == 403, "inference:anthropic alone does not open the OpenAI routes"
    finally:
        srv.shutdown()
    return "one resolve, one authorize per request, x-api-key and Bearer alike"


# ---------------------------------------------------------------------------- the model list

def test_a_model_the_token_may_not_use():
    """Service.resolve refuses a model outside the token's list; the Messages handler answers
    403 permission_error naming it, for a message and for a count, with either header, and the
    refusal's row names the token."""
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            own = te.setup(px)
            tok, view = tt.new_token(px, own, label="t", scopes=["inference:anthropic"],
                                     models=["small-*"])
            for h in ({"x-api-key": tok}, bearer(tok)):
                for path in ("/v1/messages", "/v1/messages/count_tokens"):
                    st, hd, out = px.post(path, {**MSG, "model": "big-1"}, h)
                    is_messages_error(st, hd, out, 403, "permission_error")
                    assert "big-1" in json.loads(out)["error"]["message"], out
                    row = px.rows()[-1]
                    assert (row["status"], row["error_type"], row["token_id"], row["api"]) == \
                        (403, "permission_denied", view["id"], "anthropic"), row
                    assert row["client_id"] == client_id(tok), row
                assert px.post("/v1/messages", MSG, h)[0] == 200
    finally:
        srv.shutdown()
    return "403 permission_error naming the model, on both routes and both headers"


def test_a_model_the_token_may_not_use_passing_through():
    """On an all-Messages-API route the request would pass through: the same 403, before any
    translation (a body only pass-through takes is refused for its model, not as untranslatable)
    and nothing reaches the backend."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    try:
        with Proxy(env={**anth_env(port), **ON}) as px:
            px.sync()
            own = te.setup(px)
            tok, _ = tt.new_token(px, own, label="t", scopes=["inference:anthropic"],
                                  models=["model-a"])
            server_tool = {"type": "web_search_20260209", "name": "web_search"}
            body = {"model": "model-b", "max_tokens": 8, "tools": [server_tool],
                    "messages": [{"role": "user", "content": "hi"}]}
            before = len(srv.fake.raws)
            for h in ({"x-api-key": tok}, bearer(tok)):
                st, hd, out = px.post("/v1/messages", body, h)
                is_messages_error(st, hd, out, 403, "permission_error")
                assert "model-b" in json.loads(out)["error"]["message"], out
            assert len(srv.fake.raws) == before, "nothing went upstream"
            st, _, out = px.post("/v1/messages", {**body, "model": "model-a"}, {"x-api-key": tok})
            assert st == 200, out
            assert len(srv.fake.raws) == before + 1
    finally:
        srv.shutdown()
    return "403 on the pass-through path too, the server tool never translated"


def test_the_model_list_shape_with_accounts_on():
    """The gate strips the token; the list's shape follows the header that held it: x-api-key
    alone is the Messages API's, Bearer (or both) the OpenAI one, anthropic-version always the
    Messages API's; the page's cookie gets the OpenAI list."""
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), **ON}) as px:
            px.sync()
            own = te.setup(px)
            tok, _ = tt.new_token(px, own, label="t", scopes=["inference:openai",
                                                            "inference:anthropic"])
            cases = (({"x-api-key": tok}, "messages"), (bearer(tok), "openai"),
                     ({"x-api-key": tok, **bearer(tok)}, "openai"),
                     ({"anthropic-version": "2023-06-01", **bearer(tok)}, "messages"),
                     (own, "openai"), ({"anthropic-version": "2023-06-01", **own}, "messages"))
            for h, shape in cases:
                st, out = j(px.get("/v1/models", h))
                assert st == 200, (h.keys(), st, out)
                got = "messages" if "has_more" in out else "openai"
                assert got == shape and (out.get("object") == "list") == (shape == "openai"), \
                    (sorted(h), out)
                assert "small-1" in [m["id"] for m in out["data"]]
            st, out = j(px.get("/v1/models/small-1", {"x-api-key": tok}))
            assert st == 200 and out["type"] == "model", out
    finally:
        srv.shutdown()
    return "x-api-key alone: the Messages list; Bearer, both, the cookie: the OpenAI list"


def test_the_model_list_shape_with_accounts_off():
    """With the switch off nothing is stripped and the headers decide, as before."""
    srv, port = engine()
    try:
        with Proxy(env={**tt.env_for(port), "TANDEM_API_KEYS": "laptop:key-of-the-laptop-01"}) \
                as px:
            px.sync()
            key = "key-of-the-laptop-01"
            cases = (({"x-api-key": key}, "messages"), (bearer(key), "openai"),
                     ({"x-api-key": key, **bearer(key)}, "openai"),
                     ({"anthropic-version": "2023-06-01", **bearer(key)}, "messages"))
            for h, shape in cases:
                st, out = j(px.get("/v1/models", {**h, **te.LAN}))
                assert st == 200, (h.keys(), st, out)
                assert ("messages" if "has_more" in out else "openai") == shape, (sorted(h), out)
                assert "small-1" in [m["id"] for m in out["data"]]
            assert px.get("/v1/models", {"x-api-key": "wrong-key-000000", **te.LAN})[0] == 401
    finally:
        srv.shutdown()
    return "the headers decide with the switch off"


# ---------------------------------------------------------------------------- the stash

def _turns(px, h, msgs):
    """One OpenAI-shaped turn of a tool loop; the assistant message it answered."""
    st, _, data = px.post("/v1/chat/completions", {"model": "model-a", "messages": msgs,
                                                   "tools": [FN], "reasoning_effort": "low"},
                          headers=h)
    assert st == 200, data
    msg = json.loads(data)["choices"][0]["message"]
    msg.pop("reasoning_content", None)
    return msg


def test_the_stash_is_the_tokens():
    """With the accounts on, the stash is keyed by the token's id: a rotated token's new secret
    finds the thinking its old one left; another token sending the same history finds none."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.think = True
    try:
        with Proxy(env={**anth_env(port), **ON}) as px:
            px.sync()
            own = te.setup(px)
            tok, view = tt.new_token(px, own, label="t", scopes=["inference:openai"])
            other, other_view = tt.new_token(px, own, label="o", scopes=["inference:openai"])
            stash, owners = px.svc.relay.stash, []
            restore = stash.restore

            def spying(scope, msgs):
                owners.append(scope[0])
                return restore(scope, msgs)
            stash.restore = spying
            msgs = [{"role": "user", "content": "weather in two cities?"}]
            first = _turns(px, bearer(tok), msgs)
            assert first.get("tool_calls"), first
            nxt = msgs + [first] + [{"role": "tool", "tool_call_id": c["id"], "content": "12 C"}
                                    for c in first["tool_calls"]]
            st, out = j(px.post(f"/v1/auth/tokens/{view['id']}/rotate", {}, own))
            assert st == 200 and out["id"] == view["id"], out
            rotated = out["token"]
            assert rotated != tok
            for h, kept in ((bearer(rotated), True), (bearer(other), False)):
                _turns(px, h, nxt)
                sent = srv.fake.last["body"]["messages"][1]["content"]
                th = [b for b in sent if b["type"] == "thinking"]
                assert (bool(th) and th[0]["signature"] in srv.fake.signatures) if kept \
                    else th == [], (kept, sent)
            assert owners and set(owners) <= {"t:" + view["id"], "t:" + other_view["id"]}, \
                owners
    finally:
        srv.shutdown()
    return "the rotated secret keeps the stash; another token gets none"


def test_the_stash_owner_of_a_principal():
    """The owner with the accounts on; with them off the bearer's hash stays the key
    (test_proxy_passthrough.py test_the_stash_is_per_token_over_http)."""
    tok = Principal(kind="user", id="tok-1", name="own", via="token")
    acct = Principal(kind="account", id="acc-1", name="own", via="cookie")
    assert stash_mod.owner_of(tok) == "t:tok-1"
    assert stash_mod.owner_of(acct) == "p:account:acc-1"
    h = {"Authorization": "Bearer abc"}
    assert stash_mod.token_of(h) == "k:" + hashlib.sha256(b"abc").hexdigest()
    return "t:<token id>, p:<kind>:<id>; k:<hash> with the switch off"


if __name__ == "__main__":
    run(globals())
