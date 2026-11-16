"""contracts/hooks-v1 is what the controller does (SRV-849, SEC-16; QA SRV-850, SEC-17).

  1  stages.json is proxy/hookstages.py's table; every stage has its event schema
     and examples                                                   test_stages_json_is_the_table
  2  every example validates; a meta example has no `content`; the
     stages route serves each stage's sample as the examples hold it test_examples_validate
  3  every call the runner makes at every wired stage, through the
     relay, validates against call.schema and its stage's event      test_real_calls_validate
  4  results: the schema's shape and the runner's check agree        test_results_validate
  5  the admin API's answers validate against their schemas          test_api_answers_validate
  5b one request's trace across hooks and stages, in the path's order test_real_calls_validate
  6  the fake hook server refuses a wrong signature, an old timestamp
     and a replay, and signs as proxy/hookcall.py does              test_fake_server_refuses
  7  the README names every file of the folder                      test_readme_names_every_file

Run: python tests/test_hooks_contract.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooks_util import CONTRACT, fake_server, hook_server  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

from proxy import hookcall, hooksample  # noqa: E402
from proxy import hooks as H  # noqa: E402
from proxy import hookstages as S  # noqa: E402
from tools import contract_check, hooks_gate  # noqa: E402

KEY = hooks_gate.KEY


def _load(name):
    with open(os.path.join(CONTRACT, name), encoding="utf-8") as f:
        return json.load(f)


def _check(value, name) -> list:
    return contract_check.validate(value, _load(name), root=CONTRACT)


def _call_ok(env) -> None:
    errs = _check(env, "call.schema.json") + _check(env["event"],
                                                    f"events/{env['stage']}.schema.json")
    assert not errs, (env["stage"], errs)


def test_stages_json_is_the_table():
    assert _load("stages.json") == S.export()
    for st in S.STAGES:
        assert os.path.isfile(os.path.join(CONTRACT, "events", f"{st}.schema.json")), st
        assert os.path.isfile(os.path.join(CONTRACT, "examples", f"{st}.meta.json")), st
    assert sorted(f[:-12] for f in os.listdir(os.path.join(CONTRACT, "events"))) == \
        sorted(S.STAGES)
    # the design's budgets and defaults
    want = {"request.received": 150, "route.select": 150, "request.before_engine": 250,
            "decision.ask": 400, "stream.chunk": None, "tool_call.detected": 500,
            "tool.before_exec": 500, "tool.after_exec": 500, "response.after_engine": 1000,
            "request.error": 250, "request.finished": None, "memory.write": 500,
            "session.start": 250, "session.compact": 250, "engine.lifecycle": None}
    assert {k: v["budget_ms"] for k, v in S.STAGES.items()} == want
    assert S.CAP_MS == 800 and set(S.KINDS) == {"http", "plugin", "mcp", "builtin", "inproc"}
    for st, spec in S.STAGES.items():
        assert set(spec["content"]) <= set(spec["fields"]), st
        assert spec["on_failure"] == ("drop" if not spec["sync"] else "open"), st
    return f"{len(S.STAGES)} stages"


def test_examples_validate():
    n = 0
    for f in sorted(os.listdir(os.path.join(CONTRACT, "examples"))):
        env = _load(os.path.join("examples", f))
        _call_ok(env)
        stage, level = f[:-5].rsplit(".", 1)
        assert env["stage"] == stage and env["payload"] == level
        assert ("content" in env["event"]) == (level != "meta"), f
        assert env == hooksample.envelope(stage, level), f        # the files are the samples
        # SRV-849: what the stages route serves is the same: the meta event, and the full
        # example's content where the stage has one
        served = hooksample.samples()[stage]
        if level == "meta":
            assert served["event"] == env["event"], f
            if not os.path.isfile(os.path.join(CONTRACT, "examples", f"{stage}.full.json")):
                assert served["content"] is None, f
        else:
            assert served["content"] == env["event"]["content"], f
            assert served["event"] == {k: v for k, v in env["event"].items()
                                       if k != "content"}, f
        n += 1
    # a redacted envelope validates too, its strings through the redaction
    s = hooksample.sample("request.received")
    body = dict(s["content"], messages=[{"role": "user", "content":
                                         "key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX please"}])
    ev = hookcall.event_at("redacted", s["meta"], body)
    assert "sk-proj" not in json.dumps(ev) and "[redacted:api_key]" in json.dumps(ev)
    assert not _check(ev, "events/request.received.schema.json")
    return f"{n} examples"


def test_real_calls_validate():
    s1, p1 = fake(models=["fake-a"], key=KEY, cache=False, name="f1")
    s2, p2 = fake(models=["fake-b"], key=KEY, cache=False, name="f2")
    hs, hport = hook_server()
    hs.respond = lambda env: None
    env = {"TANDEM_BACKENDS": "f1,f2"}
    for bid, port in (("f1", p1), ("f2", p2)):
        env[f"TANDEM_BACKEND_{bid.upper()}_KIND"] = "generic"
        env[f"TANDEM_BACKEND_{bid.upper()}_URL"] = f"http://127.0.0.1:{port}"
        env[f"TANDEM_BACKEND_{bid.upper()}_KEY"] = KEY
    try:
        with Proxy(env) as px:
            px.sync()
            ep = f"http://127.0.0.1:{hport}/"
            # decision.ask is a hook decision provider's call, not the relay's
            # (tests/test_decision_providers.py validates its calls)
            wired = [s for s, v in S.STAGES.items() if v["wired"] and s != "decision.ask"]
            for level in ("meta", "full"):
                for st in wired:
                    role = next(r for r in ("transform", "decider", "observer")
                                if r in S.STAGES[st]["roles"])
                    code, _, out = px.post("/v1/fleet/hooks", {
                        "name": f"{st} {level}", "endpoint": ep, "role": role, "stages": [st],
                        "payload": level})
                    assert code == 201, out
            s1.fake.script = lambda body, h: {"status": 503, "message": "busy"} \
                if "FAIL" in json.dumps(body) else None
            for body in (hooks_gate._chat("hi", tools=hooks_gate.TOOLS),
                         hooks_gate._chat("go FAKE_TOOL", True, tools=hooks_gate.TOOLS),
                         hooks_gate._chat("hi FAIL")):
                code, _, _ = px.post("/v1/chat/completions", body)
            px.rows()
            px.svc.hooks.bg.drain(10)
            seen = {}
            for c in hs.calls:
                e = c["env"]
                _call_ok(e)
                if e["payload"] == "meta":
                    assert "content" not in e["event"], e         # never text at meta
                elif e["stage"] in ("request.received", "request.before_engine"):
                    assert e["event"]["content"]["messages"], e
                seen[e["stage"]] = seen.get(e["stage"], 0) + 1
            assert set(seen) == set(wired), set(wired) - set(seen)
            # SRV-849: one request's trace holds every call of it, across hooks and stages
            per: dict = {}
            for c in hs.calls:
                per.setdefault(c["env"]["request"]["request_id"], []).append(c["env"])
            rid, envs = max(per.items(), key=lambda kv: len({e["stage"] for e in kv[1]}))
            from urllib.parse import quote
            code, _, out = px.get(f"/v1/fleet/hooks/calls?request={quote(rid)}")
            got = json.loads(out)
            assert code == 200 and not _check(got, "hook-trace.schema.json"), got
            assert got["request"] == rid and len(got["calls"]) == len(envs), (got, len(envs))
            assert sorted((c["hook"], c["stage"]) for c in got["calls"]) == \
                sorted((e["hook"], e["stage"]) for e in envs)
            names = {h["id"]: h["name"] for h in json.loads(px.get("/v1/fleet/hooks")[2])["hooks"]}
            assert all(c["name"] == names[c["hook"]] for c in got["calls"])
            order = list(S.STAGES)
            keys = [(c["ts"], order.index(c["stage"])) for c in got["calls"]]
            assert keys == sorted(keys), keys                   # the order the request met them
            assert len({c["stage"] for c in got["calls"]}) > 1
            code, _, out = px.get("/v1/fleet/hooks/calls?request=nope-0")
            assert code == 200 and json.loads(out) == {"request": "nope-0", "calls": []}
            return f"{len(hs.calls)} calls at {len(seen)} stages, a trace of {len(envs)}"
    finally:
        s1.shutdown()
        s2.shutdown()
        hs.shutdown()


def test_results_validate():
    good = [("request.received", "transform", "meta", {"action": "modify",
                                                       "patch": {"temperature": 0.1}}),
            ("request.received", "guard", "meta", {"action": "deny", "reason": "no"}),
            ("route.select", "decider", "meta", {"action": "decide",
                                                 "decision": {"backend": "b", "model": "m"}}),
            ("request.error", "decider", "meta", {"action": "decide", "decision": {
                "fallback": {"backend": "b"}}}),
            ("response.after_engine", "decider", "meta", {"action": "decide",
                                                          "decision": {"retry": True}}),
            ("response.after_engine", "transform", "full", {"action": "modify",
                                                            "patch": {"choices": []},
                                                            "labels": {"a": "b"}}),
            ("request.finished", "observer", "meta", {"action": "continue"})]
    for st, role, level, res in good:
        assert H.check_result(st, role, level, res)[1] is None, (st, res)
        assert not _check(res, "result.schema.json"), res
    bad = [{"action": "continue", "extra": 1}, {"action": "go"}, {"patch": {}},
           {"action": "decide", "decision": {"nope": 1}}]
    for res in bad:
        assert _check(res, "result.schema.json"), res
        assert H.check_result("route.select", "decider", "meta", res)[1] is not None, res


def test_api_answers_validate():
    with Proxy({}) as px:
        hs, hport = hook_server()
        try:
            st, _, out = px.post("/v1/fleet/hooks", {
                "name": "Temperature cap", "endpoint": f"http://127.0.0.1:{hport}/x",
                "role": "transform", "stages": ["request.before_engine"], "priority": 3,
                "scope": {"models": ["q*"]}})
            got = json.loads(out)
            assert st == 201 and not _check(got, "hook-created.schema.json"), got
            hid = got["hook"]["id"]
            assert hid == "temperature-cap" and got["secret"].startswith("hks_")
            st, _, out = px.get("/v1/fleet/hooks")
            assert st == 200 and not _check(json.loads(out), "hooks.schema.json")
            for method, path, body in (("GET", f"/v1/fleet/hooks/{hid}", None),
                                       ("PATCH", f"/v1/fleet/hooks/{hid}", {"priority": 4}),
                                       ("POST", f"/v1/fleet/hooks/{hid}/disable", None),
                                       ("POST", f"/v1/fleet/hooks/{hid}/enable", None),
                                       ("PUT", f"/v1/fleet/hooks/{hid}/mode",
                                        {"mode": "shadow"})):
                st, _, out = px.request(method, path, body)
                assert st == 200 and not _check(json.loads(out), "hook-one.schema.json"), out
            st, _, out = px.post(f"/v1/fleet/hooks/{hid}/test", {})
            got = json.loads(out)
            assert st == 200 and got["outcome"] == "ok", got
            assert not _check(got, "hook-test.schema.json"), got
            # the event it was sent: the stage's sample at its payload level (meta)
            assert got["event"] == _load("examples/request.before_engine.meta.json")["event"]
            assert hs.calls[-1]["env"]["event"] == got["event"]
            st, _, out = px.post(f"/v1/fleet/hooks/{hid}/test", {"event": {"params": {
                "temperature": 1.5}}})
            got2 = json.loads(out)
            assert st == 200 and got2["event"]["params"] == {"temperature": 1.5}, got2
            assert hs.calls[-1]["env"]["event"] == got2["event"]
            st, _, out = px.get(f"/v1/fleet/hooks/{hid}/calls")
            got = json.loads(out)
            assert st == 200 and got["calls"][0]["mode"] == "test", got
            assert not _check(got, "hook-calls.schema.json"), got
            rid = got["calls"][0]["request"]
            st, _, out = px.get(f"/v1/fleet/hooks/calls?request={rid}")
            tr = json.loads(out)
            assert st == 200 and not _check(tr, "hook-trace.schema.json"), tr
            assert [(c["hook"], c["name"], c["mode"]) for c in tr["calls"]] == \
                [(hid, "Temperature cap", "test")], tr
            st, _, out = px.post(f"/v1/fleet/hooks/{hid}/secret", {"grace_s": 60})
            got = json.loads(out)
            assert st == 200 and not _check(got, "hook-created.schema.json"), got
            assert got["hook"]["secret"]["previous_until"] is not None
            st, _, out = px.get("/v1/fleet/hooks/stages")
            got = json.loads(out)
            samples = got.pop("samples")
            assert st == 200 and got == S.export()
            assert samples == hooksample.samples() and set(samples) == set(S.STAGES)
            for method, path, body, code in (
                    ("POST", "/v1/fleet/hooks", {"name": "p", "kind": "plugin",
                                                 "role": "observer",
                                                 "stages": ["request.finished"]}, 422),
                    ("POST", "/v1/fleet/hooks", {"name": "Temperature cap",
                                                 "id": "temperature-cap", "endpoint":
                                                 "http://127.0.0.1:1/", "role": "observer",
                                                 "stages": ["request.finished"]}, 409),
                    ("GET", "/v1/fleet/hooks/nope", None, 404),
                    ("GET", "/v1/fleet/hooks/calls", None, 400),
                    ("POST", "/v1/fleet/hooks", {"name": "x", "id": "calls", "endpoint":
                                                 "http://127.0.0.1:1/", "role": "observer",
                                                 "stages": ["request.finished"]}, 422),
                    ("POST", "/v1/fleet/hooks", {"name": "x", "endpoint":
                                                 "http://u:p@127.0.0.1:1/", "role": "observer",
                                                 "stages": ["request.finished"]}, 422),
                    ("POST", "/v1/fleet/hooks", {"name": "x", "endpoint":
                                                 "http://203.0.113.7/h", "role": "observer",
                                                 "stages": ["request.finished"],
                                                 "payload": "full"}, 409)):
                st, _, out = px.request(method, path, body)
                got = json.loads(out)
                assert st == code and not _check(got, "error.schema.json"), (st, got)
            st, _, out = px.request("DELETE", f"/v1/fleet/hooks/{hid}")
            assert st == 200 and json.loads(out) == {"deleted": hid}
        finally:
            hs.shutdown()


def test_fake_server_refuses():
    secret = hookcall.new_secret()
    assert fake_server.signature(secret, 5, b"x") == hookcall.signature(secret, 5, b"x")
    now = [1_000_000.0]
    chk = fake_server.Check([secret], clock=lambda: now[0])
    body = json.dumps({"id": "hd-1", "x": 1}).encode()
    ts = int(now[0])
    good = {"X-Tandem-Timestamp": str(ts),
            "X-Tandem-Signature": hookcall.sign([secret], ts, body)}
    assert chk(good, body) == (True, "ok")
    assert chk(good, body) == (False, "replayed")
    body2 = json.dumps({"id": "hd-2"}).encode()
    assert chk(good, body2) == (False, "bad_signature")            # another body
    wrong = {"X-Tandem-Timestamp": str(ts),
             "X-Tandem-Signature": hookcall.sign(["hks_wrong"], ts, body2)}
    assert chk(wrong, body2) == (False, "bad_signature")
    old_ts = ts - 301
    old = {"X-Tandem-Timestamp": str(old_ts),
           "X-Tandem-Signature": hookcall.sign([secret], old_ts, body2)}
    assert chk(old, body2) == (False, "stale")
    assert chk({}, body2) == (False, "no_signature")
    two = {"X-Tandem-Timestamp": str(ts),
           "X-Tandem-Signature": hookcall.sign(["hks_new", secret], ts, body2)}
    assert chk(two, body2) == (True, "ok")                         # a rotation's grace
    # the controller's own Verifier keeps the same rule
    v = hookcall.Verifier([secret], clock=lambda: now[0])
    b3 = json.dumps({"id": "hd-3"}).encode()
    h3 = {"X-Tandem-Timestamp": str(ts), "X-Tandem-Signature": hookcall.sign([secret], ts, b3)}
    assert v.check(h3, b3) == (True, "ok") and v.check(h3, b3) == (False, "replayed")
    # over HTTP: a call signed with another secret answers 401
    srv, port = hook_server(secrets=[secret])
    try:
        import http.client
        for sec, code in (("hks_other", 401), (secret, 200)):
            b = json.dumps({"id": f"hd-{sec}"}).encode()
            t = int(time.time())
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/", b, {"X-Tandem-Timestamp": str(t),
                                       "X-Tandem-Signature": hookcall.sign([sec], t, b),
                                       "Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            c.close()
            assert r.status == code, (sec, r.status)
        assert [x["verdict"] for x in srv.calls] == ["bad_signature", "ok"]
    finally:
        srv.shutdown()


def test_readme_names_every_file():
    with open(os.path.join(CONTRACT, "README.md"), encoding="utf-8") as f:
        text = f.read()
    for name in os.listdir(CONTRACT):
        if name in ("README.md", "__pycache__", "events", "examples"):
            continue
        assert f"`{name}`" in text, name
    assert "`events/<stage>.schema.json`" in text and "`examples/" in text


if __name__ == "__main__":
    run(globals())
