"""SRV-889: an API token names the backends and models it may run (proxy/tokens.py may_run,
proxy/service.py restrict and visible_models), with TANDEM_AUTH=accounts.

Two fake backends: `engine` (kind tandemllm, small-1 and big-1) and `other` (kind generic,
other-1); the controller is the standalone proxy in this process.

  Gherkin "A key for some backends and models":
  1  A key for the backend `other`: other-1 answers 200; small-1 (on `engine`) is 403 naming the
     backend; its view lists the backends                          test_a_key_for_one_backend
  2  A key for `engine/small-*`: small-1 200, big-1 and other-1 403 naming the model
                                                                   test_a_key_for_backend_models
  3  /v1/models lists a key only what it may run; a key with no list sees every model; a card
     the key may not run is 404                                    test_the_model_list_is_the_keys
  4  A route's fallbacks keep only the allowed backends: the first allowed one serves; none left
     is 403; a deployment's member the key may not use is refused  test_fallbacks_are_cut
  5  PATCH changes the backends; a bad id is 400                  test_patch_and_bad_ids
  6  A token table of an older release gets the column; its rows run every backend
                                                                   test_an_older_table

    python tests/test_token_backends.py
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy import app as proxy_app  # noqa: E402
from proxy import deployments  # noqa: E402
from proxy import route as route_mod  # noqa: E402
from proxy import tokens as tokens_mod  # noqa: E402
from proxy_util import Proxy as _Proxy, fake, run  # noqa: E402

Proxy = functools.partial(_Proxy, local=False)
ON = {"TANDEM_AUTH": "accounts"}
PW = "the password of ops here"
CHAT = {"messages": [{"role": "user", "content": "hi"}]}


def j(res):
    st, _h, out = res
    return st, (json.loads(out) if out else None)


def two():
    a, pa = fake(models=["small-1", "big-1"], cache=False)
    b, pb = fake(models=["other-1"], cache=False)
    env = {**ON, "TANDEM_BACKENDS": "engine,other",
           "TANDEM_BACKEND_ENGINE_KIND": "tandemllm", "TANDEM_BACKEND_ENGINE_URL": f"http://127.0.0.1:{pa}/v1",
           "TANDEM_BACKEND_OTHER_KIND": "generic", "TANDEM_BACKEND_OTHER_URL": f"http://127.0.0.1:{pb}/v1",
           "TANDEM_LOCAL_NODE": "off"}
    return a, b, env


def ops(px) -> dict:
    proxy_app.STATE["accounts"].store.create("ops", "operator", PW, actor="test")
    st, h, out = px.post("/v1/auth/session", {"username": "ops", "password": PW})
    assert st == 200, out
    return {"Cookie": h["Set-Cookie"].split(";", 1)[0], "X-Tandem-CSRF": json.loads(out)["csrf"]}


def key(px, who, **body) -> tuple[str, dict]:
    st, _h, out = px.post("/v1/auth/tokens", {"scopes": ["inference:openai"], **body}, who)
    doc = json.loads(out)
    assert st == 201, (st, doc)
    return doc.pop("token"), doc


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def chat(px, t, model):
    return j(px.post("/v1/chat/completions", {"model": model, **CHAT}, bearer(t)))


def test_a_key_for_one_backend():
    a, b, env = two()
    try:
        with Proxy(env=env) as px:
            px.sync()
            who = ops(px)
            t, view = key(px, who, label="other only", backends=["other"])
            assert view["backends"] == ["other"] and view["models"] == [], view
            assert chat(px, t, "other-1")[0] == 200
            st, out = chat(px, t, "small-1")
            assert st == 403 and "'engine'" in out["error"]["message"], (st, out)
            assert out["error"]["code"] == "permission_denied", out
    finally:
        a.shutdown(); b.shutdown()
    return "other-1 200, small-1 403 naming the backend"


def test_a_key_for_backend_models():
    a, b, env = two()
    try:
        with Proxy(env=env) as px:
            px.sync()
            t, _ = key(px, ops(px), label="small", models=["engine/small-*"])
            assert chat(px, t, "small-1")[0] == 200
            for m in ("big-1", "other-1"):
                st, out = chat(px, t, m)
                assert st == 403 and m in out["error"]["message"], (m, st, out)
    finally:
        a.shutdown(); b.shutdown()
    return "engine/small-* runs small-1 only"


def test_the_model_list_is_the_keys():
    a, b, env = two()
    try:
        with Proxy(env=env) as px:
            px.sync()
            who = ops(px)
            t, _ = key(px, who, label="other", backends=["other"])
            st, out = j(px.get("/v1/models", bearer(t)))
            assert st == 200 and [m["id"] for m in out["data"]] == ["other-1"], out
            assert px.get("/v1/models/small-1", bearer(t))[0] == 404
            assert px.get("/v1/models/other-1", bearer(t))[0] == 200
            every, _ = key(px, who, label="every")
            st, out = j(px.get("/v1/models", bearer(every)))
            assert {"small-1", "big-1", "other-1"} <= {m["id"] for m in out["data"]}, out
            small, _ = key(px, who, label="small", models=["engine/small-*"])
            st, out = j(px.get("/v1/models", bearer(small)))
            assert [m["id"] for m in out["data"]] == ["small-1"], out
    finally:
        a.shutdown(); b.shutdown()
    return "other-1 alone; every model without a list; small-1 for engine/small-*"


class _P:
    via = "token"

    def __init__(self, backends=(), models=()):
        self.backends, self.models = tuple(backends), tuple(models)


def test_fallbacks_are_cut():
    a, b, env = two()
    try:
        with Proxy(env=env) as px:
            svc = px.svc
            r = route_mod.Route("engine", "m", "m", fallbacks=[("other", "m2"), ("third", "m3")])
            svc.principal = lambda handler=None: _P(backends=["other"])
            got = svc.restrict(r)
            assert (got.backend, got.model, got.fallbacks) == ("other", "m2", []), vars
            r = route_mod.Route("engine", "m", "m", fallbacks=[("third", "m3")])
            try:
                svc.restrict(r)
                raise AssertionError("no allowed backend: refused")
            except deployments.Refused as exc:
                assert exc.status == 403 and "'engine'" in exc.message, exc.message
            r = route_mod.Route("engine", "m", "m", fallbacks=[("other", "m2")])
            r.deployment = "dep"
            try:
                svc.restrict(r)
                raise AssertionError("a deployment's member the key may not use: refused")
            except deployments.Refused as exc:
                assert exc.status == 403
            # a token without a list and a signed-in person: the route as it was
            svc.principal = lambda handler=None: _P()
            r = route_mod.Route("engine", "m", "m", fallbacks=[("other", "m2")])
            assert svc.restrict(r).fallbacks == [("other", "m2")]
    finally:
        a.shutdown(); b.shutdown()
    return "the first allowed fallback serves; none left 403; a deployment keeps its member or refuses"


def test_patch_and_bad_ids():
    a, b, env = two()
    try:
        with Proxy(env=env) as px:
            px.sync()
            who = ops(px)
            t, view = key(px, who, label="k")
            st, out = j(px.request("PATCH", f"/v1/auth/tokens/{view['id']}", {"backends": ["engine"]}, who))
            assert st == 200 and out["backends"] == ["engine"], (st, out)
            assert chat(px, t, "other-1")[0] == 403
            assert chat(px, t, "small-1")[0] == 200
            st, out = j(px.post("/v1/auth/tokens", {"label": "bad", "scopes": ["inference:openai"],
                                                    "backends": ["Not An Id"]}, who))
            assert st == 400 and "backends" in out["error"]["message"], (st, out)
    finally:
        a.shutdown(); b.shutdown()
    return "PATCH backends; a bad id 400"


def test_an_older_table():
    d = tempfile.mkdtemp(prefix="tok-old-")
    db = os.path.join(d, "config.sqlite3")
    con = sqlite3.connect(db)
    ddl = tokens_mod.DDL.replace(", backends TEXT NOT NULL DEFAULT '[]'", "")
    assert "backends" not in ddl
    con.executescript(ddl)
    cols = [c for c in tokens_mod.COLS if c != "backends"]
    con.execute(f"INSERT INTO api_tokens({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                ("abcd1234", "user", None, "old", "h" * 64, "wxyz", '["inference:openai"]', "[]", "{}",
                 "old", "created", None, 0, 1, "test", None, None, None, None, None, None, None))
    con.commit()
    con.close()
    from proxy import accounts as accounts_mod
    from proxy import config as config_mod
    cfg = config_mod.ConfigDB(db, env={}, use_keychain=False).open()
    ts = accounts_mod.AccountStore(cfg).tokens
    row = ts.get("abcd1234")
    assert row["backends"] == [] and ts.view(row)["backends"] == [], row
    assert tokens_mod.may_run([], [], "x", "any", "x")
    return "the column added; the old row runs every backend"


def test_may_run():
    assert tokens_mod.may_run([], ["a"], "m", "a", "m") and not tokens_mod.may_run([], ["a"], "m", "b", "m")
    assert tokens_mod.may_run(["a/*"], [], "pub", "a", "up")
    assert tokens_mod.may_run(["openrouter/anthropic/*"], [], "anthropic/claude", "openrouter", "anthropic/claude")
    assert not tokens_mod.may_run(["a/small-*"], [], "big", "a", "big")
    assert tokens_mod.may_run(["fast"], ["a"], "fast", "a", "qwen-4b")
    assert tokens_mod.model_allowed(["a/*"], "anything")          # checked again on the backend
    return "backends, backend/model globs, public ids"


if __name__ == "__main__":
    run(globals())
