"""SRV-873: limits and usage per API token and user (proxy/tokenlimits.py, proxy/tokenusage.py,
the ledger views' token dimension), with TANDEM_AUTH=accounts.

The backend is the fake as our engine (kind tandemllm) with small-1 (no price) and big-1 (a
price); for the pass-through, the fake Messages server. The controller is the standalone proxy
in this process. "A restart" is a new limiter on the same ledger: what a start does to the
counters.

  Gherkin "Limits and usage":
  1  A rate limit: a token limited to 10 requests a minute, 12 requests: 10 answer, 2 get 429
     with Retry-After (and the Messages API's 429 rate_limit_error with retry-after, translated
     and passed through)                                               test_a_rate_limit
  2  A spend limit: 1 dollar a day on a priced model; past it the next request is 403
     spend_limit_reached (Messages: permission_error), and after a restart still refused
                                                                       test_a_spend_limit
  3  A model without a price: the same token answers on it; the token's usage says the spend
     limit does not apply to it (unpriced)                             test_a_model_without_a_price
  4  Usage adds up: 3 tokens of 2 users; the usage view grouped by token, then by user: each sum
     equals the ledger's rows (and the token and account usage routes too)
                                                                       test_usage_adds_up

  Acceptance criteria:
  a  50 parallel requests never pass a limit by more than the requests in flight (requests a
     minute exactly; requests at once at the backend; spend by at most the requests in flight)
                                                                       test_fifty_parallel_requests,
                                                                       test_spend_under_parallel_load
  b  refused requests are ledger rows with the token and the reason   test_refused_requests_are_rows
  c  the usage routes need usage:read or the token's owner             test_the_usage_routes_rights

  Also:
  d  defaults per role from api.limits.default.*, written by an admin; a token never above its
     owner's role, at creation, on a change and at each request        test_role_defaults_and_caps
  e  a spend limit reached mid-stream ends the stream cleanly, in both protocols; an unlimited
     stream is not watched                                             test_a_limit_mid_stream
  f  the findings token_limit_near and token_limit_reached fire and clear   test_the_findings
  g  the counters come from the ledger at the first sight, and roll at midnight
                                                                       test_counters_from_the_ledger
  h  switched off, nothing of this exists                             test_switched_off

    python tests/test_token_limits.py
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sqlite3
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_anthropic  # noqa: E402
from proxy import app as proxy_app  # noqa: E402
from proxy import authz  # noqa: E402
from proxy import tokenlimits  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402
from test_auth_tokens import (CHAT, ON, accounts, bearer, env_for, j, make,  # noqa: E402
                              new_token, sign_in)

PRICE = {"TANDEM_PRICE_1": "engine/big-1=20000,20000,20000"}     # 2 cents a token
BIG = {"model": "big-1", **CHAT}
SMALL = {"model": "small-1", **CHAT}


def engine():
    return fake(models=["small-1", "big-1"], cache=False)


def controller(port, **more):
    return Proxy(env={**ON, **env_for(port, **PRICE, **more)})


def limits() -> tokenlimits.TokenLimits:
    return accounts().token_limits


def restart(px) -> None:
    """A start's effect on the counters: a new limiter over the same ledger, flushed."""
    px.rows()
    led = proxy_app.STATE["ledger"]
    px.svc.limits = accounts().token_limits = tokenlimits.TokenLimits(
        accounts().policy, reader=led.reader)


def msg(model="small-1", stream=False) -> dict:
    return {"model": model, "max_tokens": 64, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}]}


def setting(px, key: str, value) -> None:
    px.svc.cfg.set_setting(key, value, who="test")


def ledger_sums(px, where: str, args=()) -> dict:
    con = sqlite3.connect(proxy_app.STATE["ledger"].path)
    n, p, o, cost = con.execute(
        "SELECT COUNT(*), SUM(COALESCE(prompt_tokens, 0)), SUM(COALESCE(completion_tokens, 0)), "
        f"SUM(COALESCE(cost_micros, 0)) FROM requests WHERE {where}", args).fetchone()
    con.close()
    return {"requests": n, "total_tokens": (p or 0) + (o or 0), "cost": cost or 0}


# ---------------------------------------------------------------------------- Gherkin 1

def test_a_rate_limit():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, view = new_token(px, ops, label="ten", scopes=["inference:openai"],
                                    limits={"requests_per_minute": 10})
            assert view["limits"] == {"requests_per_minute": 10}, view
            got = [px.post("/v1/chat/completions", SMALL, bearer(token)) for _ in range(12)]
            assert [st for st, _, _ in got] == [200] * 10 + [429] * 2, [g[0] for g in got]
            for st, h, out in got[10:]:
                doc = json.loads(out)
                assert doc["error"]["code"] == "rate_limited", doc
                assert doc["error"]["limit"] == "requests_per_minute", doc
                assert 1 <= int(h["Retry-After"]) <= 60, h
            # the Messages API: its own 429, with retry-after
            t2, _ = new_token(px, ops, label="one", scopes=["inference:anthropic"],
                              limits={"requests_per_minute": 1})
            h2 = {"x-api-key": t2, "anthropic-version": "2023-06-01"}
            assert px.post("/v1/messages", msg(), h2)[0] == 200
            for stream in (False, True):
                st, h, out = px.post("/v1/messages", msg(stream=stream), h2)
                doc = json.loads(out)
                assert st == 429 and doc["type"] == "error" and \
                    doc["error"]["type"] == "rate_limit_error", (st, doc)
                low = {k.lower(): v for k, v in h.items()}
                assert int(low["retry-after"]) >= 1 and low["request-id"], h
            # the minute's window holds the 10 let in, not the 2 refused
            assert limits().state(view["id"], view["limits"], "operator")["limits"][
                "requests_per_minute"]["used"] == 10
    finally:
        srv.shutdown()


def test_a_rate_limit_passes_through():
    """The Messages API on a Messages API backend (bytes pass): the same 429 in its shape."""
    srv, port = fake_anthropic.start(key="sk-fixture-ABCDEFGH1234")
    try:
        env = {**ON, "TANDEM_BACKENDS": "anth", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
               "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}",
               "TANDEM_BACKEND_ANTH_KEY": "sk-fixture-ABCDEFGH1234", "TANDEM_LOCAL_NODE": "off"}
        with Proxy(env=env) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, view = new_token(px, ops, label="one", scopes=["inference:anthropic"],
                                    limits={"requests_per_minute": 1})
            h = {"x-api-key": token, "anthropic-version": "2023-06-01"}
            model = srv.fake.models[0]
            st, _, out = px.post("/v1/messages", msg(model), h)
            assert st == 200, out
            n = len(srv.fake.requests)
            st, hh, out = px.post("/v1/messages", msg(model), h)
            doc = json.loads(out)
            assert st == 429 and doc["error"]["type"] == "rate_limit_error", (st, doc)
            assert {k.lower(): v for k, v in hh.items()}["retry-after"], hh
            assert len(srv.fake.requests) == n, "a refused request reached the backend"
            row = px.rows()[-1]
            assert row["token_id"] == view["id"] and row["error_type"] == "rate_limited" and \
                row["api"] == "anthropic", row
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 2

def test_a_spend_limit():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, view = new_token(px, ops, label="dollar", scopes=["inference:openai",
                                                                     "inference:anthropic"],
                                    limits={"cost_per_day": 1})
            spent, n = 0, 0
            while True:
                st, h, out = px.post("/v1/chat/completions", BIG, bearer(token))
                if st != 200:
                    break
                n += 1
                spent = sum(r["cost_micros"] or 0 for r in px.rows()
                            if r["token_id"] == view["id"])
                assert n < 20, "the spend limit never held"
            doc = json.loads(out)
            assert st == 403 and doc["error"]["code"] == "spend_limit_reached", (st, doc)
            assert "Retry-After" not in h, h        # SDKs retry a 429 on their own; a 403 not
            assert spent >= 1_000_000 and n >= 2, (spent, n)
            # the Messages API's shape
            hm = {"x-api-key": token, "anthropic-version": "2023-06-01"}
            st, _, out = px.post("/v1/messages", msg("big-1"), hm)
            doc = json.loads(out)
            assert st == 403 and doc["error"]["type"] == "permission_error", (st, doc)
            # after a restart: read from the ledger, still refused
            restart(px)
            st, _, out = px.post("/v1/chat/completions", BIG, bearer(token))
            assert st == 403 and json.loads(out)["error"]["code"] == "spend_limit_reached", out
            state = limits().state(view["id"], view["limits"], "operator")
            assert state["over"] and state["limits"]["cost_per_day"]["used"] == spent, state
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 3

def test_a_model_without_a_price():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            token, view = new_token(px, ops, label="dollar", scopes=["inference:openai"],
                                    limits={"cost_per_day": 1})
            while px.post("/v1/chat/completions", BIG, bearer(token))[0] == 200:
                pass
            # over the spend limit: the unpriced model still answers
            st, _, out = px.post("/v1/chat/completions", SMALL, bearer(token))
            assert st == 200, out
            assert px.rows()[-1]["cost_micros"] is None
            st, use = j(px.get(f"/v1/auth/tokens/{view['id']}/usage", ops))
            assert st == 200, use
            assert use["unpriced"] == ["small-1"], use
            by = {m["model"]: m for m in use["models"]}
            assert by["small-1"]["priced"] is False and by["big-1"]["priced"] is True, by
            assert use["limits"]["limits"]["cost_per_day"]["reached"] is True, use["limits"]
            # the refused rows do not make big-1 look unpriced
            assert by["big-1"]["refused"] >= 1, by
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- Gherkin 4

def test_usage_adds_up():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            make("adm", "admin")
            ops, adm = sign_in(px, "ops"), sign_in(px, "adm")
            t1, v1 = new_token(px, ops, label="a", scopes=["inference:openai"])
            t2, v2 = new_token(px, ops, label="b", scopes=["inference:openai"],
                               limits={"requests_per_minute": 2})
            t3, v3 = new_token(px, adm, label="c", scopes=["inference:openai", "usage:read"])
            for tok, body, n in ((t1, BIG, 3), (t1, SMALL, 1), (t2, BIG, 4), (t3, SMALL, 2),
                                 (t3, BIG, 1)):
                for _ in range(n):
                    px.post("/v1/chat/completions", body, bearer(tok))
            # the page's own gateway use: the user's row without a token
            px.post("/v1/fleet/gateway/chat/completions", SMALL, adm)
            px.rows()
            q = "/v1/dashboard/usage?bucket=day&tz=UTC"
            for group, col in (("token", "token_id"), ("user", "user")):
                st, out = j(px.get(f"{q}&group={group}", adm))
                assert st == 200 and out["group"] == group, out
                assert sum(g["requests"] for g in out["groups"]) == out["totals"]["requests"]
                for g in out["groups"]:
                    where, args = (f"{col} = ?", (g["key"],)) if g["key"] is not None else \
                        (f"{col} IS NULL", ())
                    want = ledger_sums(px, where, args)
                    assert (g["requests"], g["total_tokens"], g["cost_micros"] or 0) == \
                        (want["requests"], want["total_tokens"], want["cost"]), (g, want)
            st, out = j(px.get(f"{q}&group=token", adm))
            keys = {g["key"]: g for g in out["groups"]}
            assert set(keys) == {v1["id"], v2["id"], v3["id"], None}, keys
            assert keys[v1["id"]]["label"] == "a" and keys[v1["id"]]["owner"] == "ops"
            assert keys[v2["id"]]["refused"] == 2, keys[v2["id"]]   # 4 sent, 2 a minute
            st, out = j(px.get(f"{q}&group=user", adm))
            users = {g["key"]: g for g in out["groups"]}
            assert set(users) == {"ops", "adm"}, users
            # the token dimension and filter
            st, out = j(px.get(f"{q}&token={v1['id']}", adm))
            assert out["filters"]["token"] == v1["id"], out["filters"]
            assert out["totals"]["requests"] == ledger_sums(px, "token_id = ?",
                                                            (v1["id"],))["requests"] == 4
            dims = {t["id"]: t for t in out["dimensions"]["tokens"]}
            assert dims[v3["id"]] == {"id": v3["id"], "label": "c", "owner": "adm"}, dims
            st, out = j(px.get(f"/v1/dashboard/requests?token={v2['id']}", adm))
            assert len(out["requests"]) == 4, out
            # a token's own route and an account's: the same sums
            for tok_view, who in ((v1, ops), (v2, ops), (v3, adm)):
                st, use = j(px.get(f"/v1/auth/tokens/{tok_view['id']}/usage?tz=UTC", who))
                want = ledger_sums(px, "token_id = ?", (tok_view["id"],))
                assert (use["totals"]["requests"], use["totals"]["total_tokens"],
                        use["totals"]["cost_micros"] or 0) == \
                    (want["requests"], want["total_tokens"], want["cost"]), (use, want)
                assert sum(d["requests"] for d in use["days"]) == want["requests"]
                assert sum(m["requests"] for m in use["models"]) == want["requests"]
                assert use["token"]["id"] == tok_view["id"] and "token" not in use["token"]
            st, use = j(px.get("/v1/auth/usage?tz=UTC", adm))
            want = ledger_sums(px, "user = ? OR token_id = ?", ("adm", v3["id"]))
            assert use["user"]["username"] == "adm" and use["tokens"] == 1, use
            assert use["totals"]["requests"] == want["requests"] == 4, (use["totals"], want)
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC a

def test_fifty_parallel_requests():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            # requests a minute: exactly 20 of 50 at once, never one more
            tok, _ = new_token(px, ops, label="twenty", scopes=["inference:openai"],
                               limits={"requests_per_minute": 20})
            with cf.ThreadPoolExecutor(50) as ex:
                got = list(ex.map(lambda _: px.post("/v1/chat/completions", SMALL,
                                                    bearer(tok))[0], range(50)))
            assert got.count(200) == 20 and got.count(429) == 30, got
            # requests at once: the backend never sees more than 5 of the token's
            tok, _ = new_token(px, ops, label="five", scopes=["inference:openai"],
                               limits={"concurrent": 5})
            srv.fake.max_streams = 0
            slow = {"model": "small-1", "stream": True,
                    "messages": [{"role": "user", "content": "hi FAKE_SLOW_TTFT 0.4"}]}
            with cf.ThreadPoolExecutor(50) as ex:
                got = list(ex.map(lambda _: px.post("/v1/chat/completions", slow,
                                                    bearer(tok))[0], range(50)))
            assert 5 <= got.count(200) and got.count(200) + got.count(429) == 50, got
            assert srv.fake.max_streams <= 5, srv.fake.max_streams
    finally:
        srv.shutdown()


def test_spend_under_parallel_load():
    """The limiter alone under 50 threads: requests a minute exact; spend passed by at most the
    requests in flight when the limit was reached."""
    pol = authz.Policy(values={})
    lim = tokenlimits.TokenLimits(pol)

    class P:
        id, kind, role = "t1", "token", "operator"
        limits = (("cost_per_day", 1), ("requests_per_minute", 1000))
    cost = 300_000                             # 30 cents a request
    out, inflight, peak = [], [0], [0]
    lock = threading.Lock()
    go = threading.Barrier(50)

    def one(_):
        go.wait()
        t = lim.admit(P(), priced=True)
        if not isinstance(t, tokenlimits.Ticket):
            return t.code
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.01)
        with lock:
            inflight[0] -= 1
        lim.done(t, 10, cost)
        return "ok"
    with cf.ThreadPoolExecutor(50) as ex:
        out = list(ex.map(one, range(50)))
    ok = out.count("ok")
    spent = ok * cost
    st = lim.state("t1", dict(P.limits), "operator")["limits"]["cost_per_day"]
    assert st["used"] == spent and st["reached"], st
    # past the dollar by at most the requests that were in flight
    assert spent - 1_000_000 < peak[0] * cost, (spent, peak[0])
    assert out.count("spend_limit_reached") == 50 - ok


# ---------------------------------------------------------------------------- AC b

def test_refused_requests_are_rows():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            tok, view = new_token(px, ops, label="tight", scopes=["inference:openai"],
                                  limits={"requests_per_minute": 1, "cost_per_day": 0.0001})
            assert px.post("/v1/chat/completions", BIG, bearer(tok))[0] == 200
            assert px.post("/v1/chat/completions", BIG, bearer(tok))[0] == 429
            tok2, view2 = new_token(px, ops, label="money", scopes=["inference:openai"],
                                    limits={"cost_per_day": 0.0001})
            assert px.post("/v1/chat/completions", BIG, bearer(tok2))[0] == 200
            assert px.post("/v1/chat/completions", BIG, bearer(tok2))[0] == 403
            rows = [r for r in px.rows() if r["finish_reason"] == "refused"]
            got = sorted((r["token_id"], r["error_type"], r["status"]) for r in rows)
            assert got == sorted([(view["id"], "rate_limited", 429),
                                  (view2["id"], "spend_limit_reached", 403)]), got
            for r in rows:
                assert r["user"] == "ops" and r["model"] == "big-1" and r["cost_micros"] is None
            # the token's page lists them as its last refusals
            st, use = j(px.get(f"/v1/auth/tokens/{view2['id']}/usage", ops))
            assert [x["code"] for x in use["refusals"]] == ["spend_limit_reached"], use
            assert use["totals"]["refused"] == 1, use["totals"]
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- AC c

def test_the_usage_routes_rights():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            make("view", "viewer")
            make("adm", "admin")
            ops, view, adm = sign_in(px, "ops"), sign_in(px, "view"), sign_in(px, "adm")
            tok, tv = new_token(px, ops, label="a", scopes=["inference:openai"])
            reader, _ = new_token(px, ops, label="r", scopes=["usage:read"])
            px.post("/v1/chat/completions", SMALL, bearer(tok))
            px.rows()
            path = f"/v1/auth/tokens/{tv['id']}/usage"
            assert px.get(path, ops)[0] == 200                       # its owner
            assert px.get(path, adm)[0] == 200                       # the permission users
            assert px.get(path, view)[0] == 404                      # nobody else: not there
            assert px.get(path)[0] == 401
            for t in (tok, reader):                                  # never with a token
                assert px.get(path, bearer(t))[0] == 403
                assert px.get("/v1/auth/usage", bearer(t))[0] == 403
            # usage:read: the ledger views with token=
            st, out = j(px.get(f"/v1/dashboard/usage?token={tv['id']}", bearer(reader)))
            assert st == 200 and out["totals"]["requests"] == 1, (st, out)
            st, out = j(px.get(f"/v1/fleet/ledger/usage?token={tv['id']}", bearer(reader)))
            assert st == 200 and out["totals"]["requests"] == 1, (st, out)
            st, out = j(px.get(f"/v1/dashboard/usage?token={tv['id']}", bearer(tok)))
            assert st == 403 and out["error"]["scope"] == "usage:read", out
            # an account: its own; another's needs users
            me = accounts().store.by_username("ops")["id"]
            assert px.get("/v1/auth/usage", ops)[0] == 200
            assert px.get(f"/v1/auth/usage?user={me}", ops)[0] == 200
            st, out = j(px.get(f"/v1/auth/usage?user={me}", view))
            assert st == 403 and out["error"]["permission"] == "users", out
            st, out = j(px.get(f"/v1/auth/usage?user={me}", adm))
            assert st == 200 and out["totals"]["requests"] == 1, out
            assert px.get("/v1/auth/usage?user=nobody", adm)[0] == 404
            assert px.get(f"{path}?days=0", ops)[0] == 400
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- d

def test_role_defaults_and_caps():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("own", "owner")
            make("adm", "admin")
            make("ops", "operator")
            own, adm, ops = sign_in(px, "own"), sign_in(px, "adm"), sign_in(px, "ops")
            from test_auth_exposure import change
            key = "limits.default.operator.requests_per_minute"
            st, out = change(px, ops, "api", {key: 5})
            assert st == 403 and out["error"]["type"] == "admin_required", out
            assert change(px, adm, "api", {key: 5})[0] == 200
            assert change(px, adm, "api", {"limits.default.operator.cost_per_day": 2.5})[0] \
                == 200
            st, out = change(px, adm, "api", {"limits.default.operator.cost_per_day": -1})
            assert st == 422 and out["error"]["type"] == "bad_value", out
            st, out = change(px, adm, "api", {key: 1.5})
            assert st == 422, out
            # a new token takes its role's values where it names none
            _, v = new_token(px, ops, label="d", scopes=["inference:openai"])
            assert v["limits"] == {"requests_per_minute": 5, "cost_per_day": 2.5}, v
            _, v2 = new_token(px, ops, label="e", scopes=["inference:openai"],
                              limits={"requests_per_minute": 3})
            assert v2["limits"]["requests_per_minute"] == 3, v2
            # never above the role's
            st, out = j(px.post("/v1/auth/tokens", {"label": "f", "scopes": ["inference:openai"],
                                                    "limits": {"requests_per_minute": 50}}, ops))
            assert st == 400 and out["error"]["code"] == "limit_over_role", out
            st, out = j(px.request("PATCH", f"/v1/auth/tokens/{v2['id']}",
                                   {"limits": {"requests_per_minute": 50}}, ops))
            assert st == 400 and out["error"]["code"] == "limit_over_role", out
            st, out = j(px.request("PATCH", f"/v1/auth/tokens/{v2['id']}",
                                   {"limits": {"requests_per_minute": 4}}, ops))
            assert st == 200 and out["limits"]["requests_per_minute"] == 4, out
            # an owner's token has no cap while the owner's role has none
            _, v3 = new_token(px, own, label="g", scopes=["inference:openai"],
                              limits={"requests_per_minute": 500})
            assert v3["limits"] == {"requests_per_minute": 500}, v3
            # a lower role default holds for the tokens made before it, at each request
            tok, v4 = new_token(px, ops, label="h", scopes=["inference:openai"])
            assert change(px, adm, "api", {key: 2})[0] == 200
            got = [px.post("/v1/chat/completions", SMALL, bearer(tok))[0] for _ in range(3)]
            assert got == [200, 200, 429], got
            assert limits().effective(v4["limits"], "operator")["requests_per_minute"] == 2
            # the owner demoted: the role of now
            setting(px, "api.limits.default.viewer.requests_per_minute", 1)
            assert limits().effective({"requests_per_minute": 4}, "viewer") == \
                {"requests_per_minute": 1}
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- e

def test_a_limit_mid_stream():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            tok, view = new_token(px, ops, label="dollar", scopes=["inference:openai",
                                                                   "inference:anthropic"],
                                  limits={"cost_per_day": 1})
            long = {"model": "big-1", "stream": True,
                    "messages": [{"role": "user", "content": "go FAKE_LONG 400"}]}
            st, h, out = px.post("/v1/chat/completions", long, bearer(tok))
            assert st == 200, out
            text = out.decode()
            assert text.rstrip().endswith("data: [DONE]"), text[-300:]
            frames = [ln[6:] for ln in text.splitlines() if ln.startswith("data: {")]
            err = json.loads(frames[-1])
            assert err["error"]["code"] == "spend_limit_reached", err
            assert len(frames) < 120, len(frames)          # cut near 50 frames at 2 cents each
            row = px.rows()[-1]
            assert row["finish_reason"] == "limit" and \
                row["error_type"] == "spend_limit_reached" and row["token_id"] == view["id"], row
            # the Messages API: an error event ends it (the spend is reached now: a new stream is
            # refused at once, so a second token shows the cut in that shape)
            tok2, _ = new_token(px, ops, label="dollar2", scopes=["inference:anthropic"],
                                limits={"cost_per_day": 1})
            body = {"model": "big-1", "max_tokens": 1000, "stream": True,
                    "messages": [{"role": "user", "content": "go FAKE_LONG 400"}]}
            st, _, out = px.post("/v1/messages", body,
                                 {"x-api-key": tok2, "anthropic-version": "2023-06-01"})
            assert st == 200, out
            text = out.decode()
            assert "event: error" in text and '"permission_error"' in text, text[-400:]
            assert "message_stop" not in text.split("event: error")[-1], text[-300:]
            # a token without a spend limit is never watched: its stream runs to the end
            tok3, _ = new_token(px, ops, label="free", scopes=["inference:openai"],
                                limits={"requests_per_minute": 100})
            st, _, out = px.post("/v1/chat/completions", long, bearer(tok3))
            assert st == 200 and b"spend_limit" not in out and b"[DONE]" in out
            row = px.rows()[-1]
            assert row["finish_reason"] == "stop" and row["error_type"] is None, row
            assert out.count(b"data: {") >= 400, out.count(b"data: {")
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- f

def test_the_findings():
    srv, port = engine()
    try:
        with controller(port) as px:
            px.sync()
            make("ops", "operator")
            ops = sign_in(px, "ops")
            tok, view = new_token(px, ops, label="dollar", scopes=["inference:openai"],
                                  limits={"cost_per_day": 1})

            def codes():
                return {f["code"]: f["facts"] for f in accounts().findings()}
            assert "token_limit_near" not in codes() and "token_limit_reached" not in codes()
            # one request at 2 cents a token: 29 tokens, 58 cents; a second passes 80%
            px.post("/v1/chat/completions", BIG, bearer(tok))
            px.post("/v1/chat/completions", BIG, bearer(tok))
            got = codes()
            assert got.get("token_limit_reached") == {"n": 1}, got
            # raised to 1.40 dollars: 1.16 of it used, near, not reached
            accounts().tokens.update(view["id"], {"limits": {"cost_per_day": 1.4}},
                                     actor="test")
            got = codes()
            assert got.get("token_limit_near") == {"n": 1} and \
                "token_limit_reached" not in got, got
            accounts().tokens.revoke(view["id"], actor="test")
            got = codes()
            assert "token_limit_near" not in got and "token_limit_reached" not in got, got
            # the health page carries them with their sentences
            from proxy import enginecodes, fleethealth
            for code in ("token_limit_near", "token_limit_reached"):
                assert code in enginecodes.CATALOG and code in fleethealth.FINDINGS, code
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------- g

def test_counters_from_the_ledger():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("CREATE TABLE requests (ts_ms INTEGER, token_id TEXT, cost_micros INTEGER, "
                "prompt_tokens INTEGER, completion_tokens INTEGER, finish_reason TEXT)")
    now = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))
    rows = [(now - 20, "t", 100, 5, 5, "stop"),              # the minute, the day, the month
            (now - 30, "t", 0, 0, 0, "refused"),             # refused: no request, no cost
            (now - 3 * 3600, "t", 200, 1, 1, "stop"),        # the day, the month
            (now - 5 * 86400, "t", 400, 1, 1, "stop"),       # the month
            (now - 40 * 86400, "t", 800, 1, 1, "stop"),      # before the month
            (now - 10, "u", 1600, 1, 1, "stop")]             # another token
    con.executemany("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                    [(int(t * 1000), tid, c, p, o, f) for t, tid, c, p, o, f in rows])
    clock = [now]
    lim = tokenlimits.TokenLimits(authz.Policy(values={}), reader=lambda: con,
                                  clock=lambda: clock[0])
    lims = {"requests_per_minute": 10, "tokens_per_minute": 100, "cost_per_day": 1,
            "cost_per_month": 1}
    st = lim.state("t", lims, "operator")["limits"]
    assert (st["requests_per_minute"]["used"], st["tokens_per_minute"]["used"],
            st["cost_per_day"]["used"], st["cost_per_month"]["used"]) == (1, 10, 300, 700), st
    # read once: a new row in the ledger is not read again (the limiter counts its own)
    con.execute("INSERT INTO requests VALUES (?, 't', 5000, 1, 1, 'stop')", (int(now * 1000),))
    assert lim.state("t", lims, "operator")["limits"]["cost_per_day"]["used"] == 300
    # the minute slides, the day rolls at local midnight, the month on the 1st
    clock[0] = now + 61
    st = lim.state("t", lims, "operator")["limits"]
    assert st["requests_per_minute"]["used"] == 0 and st["tokens_per_minute"]["used"] == 0
    clock[0] = time.mktime((2026, 11, 17, 0, 0, 1, 0, 0, -1))
    st = lim.state("t", lims, "operator")["limits"]
    assert st["cost_per_day"]["used"] == 0 and st["cost_per_month"]["used"] == 700, st
    # a request that arrived yesterday and ended today counts in yesterday's day
    clock[0] = time.mktime((2026, 11, 17, 23, 59, 59, 0, 0, -1))

    class P:
        id, kind, role, limits = "t", "token", "operator", ()
    t = lim.admit(P())
    clock[0] += 5
    lim.done(t, 10, 999)
    st = lim.state("t", lims, "operator")["limits"]
    assert st["cost_per_day"]["used"] == 0 and st["cost_per_month"]["used"] == 1699, st
    # a 429's Retry-After: the seconds until the oldest request leaves the minute
    clock[0] = now + 3600 * 24 * 3
    P.limits = (("requests_per_minute", 2),)
    assert isinstance(lim.admit(P()), tokenlimits.Ticket)
    clock[0] += 10
    assert isinstance(lim.admit(P()), tokenlimits.Ticket)
    clock[0] += 5
    bad = lim.admit(P())
    assert bad.status == 429 and bad.retry_after == 45, bad


# ---------------------------------------------------------------------------- h

def test_switched_off():
    srv, port = engine()
    try:
        with Proxy(env=env_for(port, **PRICE)) as px:
            px.sync()
            assert px.svc.limits is None
            assert px.post("/v1/chat/completions", BIG)[0] == 200
            st, out = j(px.get("/v1/dashboard/usage"))
            assert st == 200 and "tokens" not in out["dimensions"] and "groups" not in out, out
            for q in ("group=token", "token=abc"):
                assert px.get(f"/v1/dashboard/usage?{q}")[0] == 400, q
            assert px.get("/v1/auth/usage")[0] == 404
    finally:
        srv.shutdown()


if __name__ == "__main__":
    run(globals())
