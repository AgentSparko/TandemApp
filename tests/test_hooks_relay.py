"""Hooks through the relay (SRV-849, QA SRV-850): the standalone proxy, fake backends and the
contract's fake hook server, end to end.

  1  no hook enabled: every answer and every byte the backend got are what they are without the
     runner; so with hooks that are off, disabled or out of scope, and with hooks in scope at every
     wired stage that answer continue (tools/hooks_gate.py's cases)   test_no_hook_byte_for_byte
  2  a transform and a guard on request.before_engine: the guard's deny
     wins, the patch is never sent, the backend gets nothing          test_deny_wins
  3  a 150 ms hook, policy open, slow five times: the breaker opens,
     requests flow without it for 30 s, then one probe               test_timeout_and_breaker
  4  a shadow hook's modify: journaled with its diff, the backend gets
     the client's bytes                                               test_shadow
  5  route.select picks a backend; request.error picks a fallback      test_route_select_and_fallback
  6  request.received rewrites the model: the request is routed again  test_model_rewrite_routes_again
  7  a whole answer: response.after_engine modifies, retries once,
     denies; tool_call.detected denies a call                         test_answer_stages
  8  a stream: stream.chunk observes every event, the bytes are the
     stream's, a modify after the stream is journaled refused         test_stream_observers
  9  request.finished sees the row and the hooks' labels              test_finished_sees_row_and_labels
 10  a closed guard that fails refuses with 503, a ledger row says so  test_closed_failure_refuses
 11  with the conductor's harness on: request.before_engine sees the
     harness's final body and its patch goes with the harness's note  test_with_the_harness
 12  a hook stage that fails inside the controller never fails the
     request: the stream and the whole answer go out as they are      test_runner_fault_relays_as_is

Run: python tests/test_hooks_relay.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooks_util import Clock, hook_server  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

from tools import hooks_gate  # noqa: E402

KEY = hooks_gate.KEY


def _env(*backs):
    env = {"TANDEM_BACKENDS": ",".join(b for b, _ in backs)}
    for bid, port in backs:
        up = bid.upper()
        env[f"TANDEM_BACKEND_{up}_KIND"] = "generic"
        env[f"TANDEM_BACKEND_{up}_URL"] = f"http://127.0.0.1:{port}"
        env[f"TANDEM_BACKEND_{up}_KEY"] = KEY
    return env


def _chat(text, model="fake-a", stream=False, **kw):
    b = {"model": model, "messages": [{"role": "user", "content": text}], "stream": stream}
    b.update(kw)
    return b


def _hook(px, doc, status=201) -> dict:
    st, _, out = px.post("/v1/fleet/hooks", doc)
    got = json.loads(out)
    assert st == status, (st, got)
    return got


def _calls(px, hid) -> list:
    px.svc.hooks.bg.drain(5)
    st, _, out = px.get(f"/v1/fleet/hooks/{hid}/calls")
    assert st == 200, out
    return json.loads(out)["calls"]


def test_no_hook_byte_for_byte():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    hs.respond = lambda env: None                                  # 204: continue
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            runner = px.svc.hooks
            px.svc.hooks = None                                    # the build without hooks
            base = {n: (d, doc) for n, d, doc in hooks_gate.run(px, srv)}
            px.svc.hooks = runner                                  # present, no hook
            assert runner.idle()
            empty = {n: d for n, d, _ in hooks_gate.run(px, srv)}
            ep = f"http://127.0.0.1:{hport}/"
            _hook(px, {"name": "off", "endpoint": ep, "role": "transform",
                       "stages": ["request.before_engine"], "mode": "off"})
            _hook(px, {"name": "disabled", "endpoint": ep, "role": "guard",
                       "stages": ["request.received"], "enabled": False})
            _hook(px, {"name": "elsewhere", "endpoint": ep, "role": "transform",
                       "stages": ["request.before_engine", "response.after_engine"],
                       "scope": {"models": ["other-*"]}})
            assert not runner.idle()
            unused = {n: d for n, d, _ in hooks_gate.run(px, srv)}
            assert not hs.calls
            for name, role, stages in (
                    ("t", "transform", ["request.received", "request.before_engine",
                                        "tool_call.detected", "response.after_engine"]),
                    ("g", "guard", ["request.received", "request.before_engine",
                                    "tool_call.detected", "response.after_engine"]),
                    ("d", "decider", ["route.select", "request.error",
                                      "response.after_engine"]),
                    ("o", "observer", ["request.received", "stream.chunk",
                                       "request.finished"])):
                _hook(px, {"name": f"continue-{name}", "endpoint": ep, "role": role,
                           "stages": stages, "payload": "full" if name == "o" else "meta"})
            idle_continue = {n: d for n, d, _ in hooks_gate.run(px, srv)}
            runner.bg.drain(10)
            assert len(hs.calls) > len(hooks_gate.CASES), len(hs.calls)
            for name, (d, doc) in base.items():
                for label, got in (("empty", empty), ("unused", unused),
                                   ("continue", idle_continue)):
                    assert got[name] == d, (label, name, doc["status"], doc["body"][:200])
            assert len(base) == len(hooks_gate.CASES) >= 18
            return f"{len(base)} cases x 4 builds equal"
    finally:
        srv.shutdown()
        hs.shutdown()


def test_deny_wins():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    hs.respond = lambda env: {"action": "modify", "patch": {"temperature": 0.01}}
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            _hook(px, {"name": "cool", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.before_engine"], "priority": 1})
            _hook(px, {"name": "stop", "kind": "builtin", "target": "deny", "role": "guard",
                       "stages": ["request.before_engine"], "priority": 100,
                       "options": {"reason": "not today"}})
            n = srv.fake.requests
            st, hdrs, out = px.post("/v1/chat/completions", _chat("hi", temperature=0.9))
            assert st == 403, out
            assert json.loads(out)["error"] == {"message": "not today", "type":
                                                "invalid_request_error", "code": "hook_denied"}
            assert hdrs.get("X-Tandem-Hook") == "stop"
            assert srv.fake.requests == n                          # nothing went upstream
            assert len(hs.calls) == 1                              # the transform ran first
            row = px.rows()[-1]
            assert row["status"] == 403 and row["finish_reason"] == "refused" and \
                row["error_type"] == "hook_denied", row
            # without the guard the patch is sent
            st, _, _ = px.request("DELETE", "/v1/fleet/hooks/stop")
            assert st == 200
            st, _, _ = px.post("/v1/chat/completions", _chat("hi", temperature=0.9))
            assert st == 200 and srv.fake.last["body"]["temperature"] == 0.01
    finally:
        srv.shutdown()
        hs.shutdown()


def test_timeout_and_breaker():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    hs.delay_s = 0.3
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            clock = Clock()
            px.svc.hooks.clock = clock
            _hook(px, {"name": "slow", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.received"], "policy": "open",
                       "timeout_ms": 150})
            for i in range(5):
                t0 = time.monotonic()
                st, _, _ = px.post("/v1/chat/completions", _chat(f"hi {i}"))
                assert st == 200 and time.monotonic() - t0 < 1.0
            assert len(hs.calls) == 5
            st, _, out = px.get("/v1/fleet/hooks/slow")
            h = json.loads(out)["hook"]
            assert h["metrics"]["timeouts"] == 5 and h["metrics"]["breaker"]["state"] == "open"
            assert 29 <= h["metrics"]["breaker"]["open_for_s"] <= 30
            for i in range(3):                                     # open: flows without it
                st, _, _ = px.post("/v1/chat/completions", _chat(f"open {i}"))
                assert st == 200
            assert len(hs.calls) == 5
            assert _calls(px, "slow")[0]["outcome"] == "breaker_open"
            clock.advance(30.5)
            hs.delay_s = 0.0
            st, _, _ = px.post("/v1/chat/completions", _chat("probe"))
            assert st == 200 and len(hs.calls) == 6
            st, _, out = px.get("/v1/fleet/hooks/slow")
            assert json.loads(out)["hook"]["metrics"]["breaker"]["state"] == "closed"
            st, _, out = px.get("/metrics")
            text = out.decode()
            assert 'qse_hook_timeouts_total{hook="slow"} 5' in text, text[-2000:]
            assert 'qse_hook_breaker{hook="slow",state="closed"} 1' in text
    finally:
        srv.shutdown()
        hs.shutdown()


def test_shadow():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    hs.respond = lambda env: {"action": "modify", "patch": {"temperature": 0.0, "seed": 1}}
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            _hook(px, {"name": "try", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.before_engine"],
                       "mode": "shadow"})
            raw = json.dumps(_chat("hi", temperature=0.9)).encode()
            st, _, _ = px.post("/v1/chat/completions", raw)
            assert st == 200 and srv.fake.last["raw"] == raw.decode()
            calls = _calls(px, "try")
            assert len(hs.calls) == 1 and hs.calls[0]["env"]["mode"] == "shadow"
            e = calls[0]
            assert e["mode"] == "shadow" and e["action"] == "modify" and e["outcome"] == "ok"
            assert e["diff"] == {"temperature": {"op": "replace", "bytes": 3},
                                 "seed": {"op": "add", "bytes": 1}}, e
    finally:
        srv.shutdown()
        hs.shutdown()


def test_route_select_and_fallback():
    s1, p1 = fake(models=["fake-a"], key=KEY, cache=False, name="f1")
    s2, p2 = fake(models=["fake-b"], key=KEY, cache=False, name="f2")
    try:
        with Proxy(_env(("f1", p1), ("f2", p2))) as px:
            px.sync()
            _hook(px, {"name": "to f2", "kind": "builtin", "target": "route", "role": "decider",
                       "stages": ["route.select"], "scope": {"users": ["owner"]},
                       "options": {"backend": "f2", "model": "fake-b"}})
            st, hdrs, _ = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 200 and hdrs["X-Tandem-Backend"] == "f2"
            assert s2.fake.last["body"]["model"] == "fake-b"
            assert px.rows()[-1]["backend"] == "f2"
            px.request("DELETE", "/v1/fleet/hooks/to-f2")
            # f1 fails before a byte: request.error chooses f2
            s1.fake.script = lambda body, headers: {"status": 503, "message": "busy"}
            _hook(px, {"name": "fb", "kind": "builtin", "target": "fallback",
                       "role": "decider", "stages": ["request.error"],
                       "options": {"backend": "f2", "model": "fake-b"}})
            st, hdrs, out = px.post("/v1/chat/completions", _chat("hi again"))
            assert st == 200 and hdrs["X-Tandem-Backend"] == "f2", out
            row = px.rows()[-1]
            assert row["fallback_from"] == "f1" and row["backend"] == "f2", row
            assert _calls(px, "fb")[0]["decision"] == {"fallback": {"backend": "f2",
                                                                    "model": "fake-b"}}
    finally:
        s1.shutdown()
        s2.shutdown()


def test_model_rewrite_routes_again():
    s1, p1 = fake(models=["fake-a"], key=KEY, cache=False, name="f1")
    s2, p2 = fake(models=["fake-b"], key=KEY, cache=False, name="f2")
    try:
        with Proxy(_env(("f1", p1), ("f2", p2))) as px:
            px.sync()
            _hook(px, {"name": "rename", "kind": "builtin", "target": "set",
                       "role": "transform", "stages": ["request.received"],
                       "scope": {"models": ["fake-a"]}, "options": {"set": {"model": "fake-b"}}})
            st, hdrs, _ = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 200 and hdrs["X-Tandem-Backend"] == "f2"
            assert px.rows()[-1]["model"] == "fake-b"
            _hook(px, {"name": "ghost", "kind": "builtin", "target": "set", "role": "transform",
                       "stages": ["request.received"], "priority": 200,
                       "options": {"set": {"model": "nobody-serves-this"}}})
            st, _, out = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 404 and json.loads(out)["error"]["code"] == "model_not_found"
    finally:
        s1.shutdown()
        s2.shutdown()


def test_answer_stages():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()

            def shout(env, o):
                ans = env["event"]["content"]
                ch = [dict(c, message=dict(c["message"], content=c["message"]["content"]
                                           .upper())) for c in ans["choices"]]
                return {"action": "modify", "patch": {"choices": ch}}
            px.svc.hooks.builtin("shout", shout)
            _hook(px, {"name": "shout", "kind": "builtin", "target": "shout",
                       "role": "transform", "stages": ["response.after_engine"],
                       "payload": "full"})
            st, _, out = px.post("/v1/chat/completions", _chat("FAKE_SAY quiet words"))
            assert st == 200, out
            got = json.loads(out)
            assert got["choices"][0]["message"]["content"] == "QUIET WORDS", got
            row = px.rows()[-1]
            assert row["completion_tokens"] and row["status"] == 200
            px.request("DELETE", "/v1/fleet/hooks/shout")
            # one retry, never two
            px.svc.hooks.builtin("again", lambda env, o: {"action": "decide",
                                                          "decision": {"retry": True}})
            _hook(px, {"name": "again", "kind": "builtin", "target": "again",
                       "role": "decider", "stages": ["response.after_engine"]})
            n = srv.fake.requests
            st, _, _ = px.post("/v1/chat/completions", _chat("FAKE_SAY once more"))
            assert st == 200 and srv.fake.requests == n + 2
            assert _calls(px, "again")[0]["outcome"] == "refused"
            px.request("DELETE", "/v1/fleet/hooks/again")
            # a deny of the whole answer
            _hook(px, {"name": "no", "kind": "builtin", "target": "deny", "role": "guard",
                       "stages": ["response.after_engine"], "options": {"reason": "held back"}})
            st, _, out = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 403 and json.loads(out)["error"]["message"] == "held back"
            row = px.rows()[-1]
            assert row["error_type"] == "hook_denied" and row["completion_tokens"], row
            px.request("DELETE", "/v1/fleet/hooks/no")
            # tool_call.detected
            seen = []
            px.svc.hooks.builtin("calls", lambda env, o: seen.append(env["event"]) or {
                "action": "deny", "reason": "no search"})
            _hook(px, {"name": "calls", "kind": "builtin", "target": "calls", "role": "guard",
                       "stages": ["tool_call.detected"]})
            st, _, out = px.post("/v1/chat/completions", _chat(
                'go FAKE_CALLS [{"name": "search", "arguments": "{\\"q\\": \\"x\\"}"}]',
                tools=hooks_gate.TOOLS))
            assert st == 403 and json.loads(out)["error"]["message"] == "no search"
            # the fake's whole answer carries its own call (`read`)
            assert seen[0]["calls"][0]["name"] == "read" and "content" not in seen[0], seen
            assert seen[0]["calls"][0]["args_bytes"] > 0 and seen[0]["streamed"] is False
            st, _, _ = px.post("/v1/chat/completions", _chat("no calls here"))
            assert st == 200 and len(seen) == 1
    finally:
        srv.shutdown()


def test_stream_observers():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            body = _chat("go FAKE_TOOL", stream=True, tools=hooks_gate.TOOLS)
            st, _, plain = px.post("/v1/chat/completions", body)
            assert st == 200
            _hook(px, {"name": "chunks", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "observer", "stages": ["stream.chunk"]})
            hs.respond = lambda env: None if env["stage"] == "stream.chunk" else {
                "action": "modify", "patch": {"choices": []}}
            _hook(px, {"name": "late", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["response.after_engine"],
                       "payload": "full"})
            st, _, got = px.post("/v1/chat/completions", body)
            assert st == 200 and got == plain
            px.svc.hooks.bg.drain(10)
            chunks = [c["env"] for c in hs.calls if c["env"]["stage"] == "stream.chunk"]
            events = [e for e in plain.split(b"\n\n") if e.strip()]
            assert len(chunks) >= len(events) - 1, (len(chunks), len(events))
            kinds = {c["event"]["kind"] for c in chunks}
            assert {"tool", "done"} <= kinds, kinds
            assert all("content" not in c["event"] for c in chunks)
            late = _calls(px, "late")[0]
            assert late["outcome"] == "refused" and late["action"] == "modify", late
            ev = next(c["env"]["event"] for c in hs.calls
                      if c["env"]["stage"] == "response.after_engine")
            assert ev["streamed"] is True and ev["tool_names"], ev
    finally:
        srv.shutdown()
        hs.shutdown()


def test_finished_sees_row_and_labels():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            px.svc.hooks.builtin("tag", lambda env, o: {"action": "continue",
                                                        "labels": {"team": "blue"}})
            seen = []
            px.svc.hooks.builtin("fin", lambda env, o: seen.append(env) or {"action":
                                                                            "continue"})
            _hook(px, {"name": "tag", "kind": "builtin", "target": "tag", "role": "transform",
                       "stages": ["request.received"]})
            _hook(px, {"name": "fin", "kind": "builtin", "target": "fin", "role": "observer",
                       "stages": ["request.finished"]})
            st, _, _ = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 200
            px.rows()
            px.svc.hooks.bg.drain(5)
            ev = seen[0]["event"]
            assert ev["labels"] == {"team": "blue"} and ev["row"]["status"] == 200
            assert ev["row"]["tokens"]["completion"] and ev["row"]["backend"] == "f1"
            assert seen[0]["request"]["model"] == "fake-a"
    finally:
        srv.shutdown()


def test_closed_failure_refuses():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            # nothing listens there: the guard fails, and a guard is closed by default
            _hook(px, {"name": "gone", "endpoint": "http://127.0.0.1:9/", "role": "guard",
                       "stages": ["request.received"]})
            n = srv.fake.requests
            st, hdrs, out = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 503, out
            assert json.loads(out)["error"]["code"] == "hook_unavailable"
            assert hdrs.get("X-Tandem-Hook") == "gone" and srv.fake.requests == n
            assert px.rows()[-1]["error_type"] == "hook_unavailable"
            st, _, _ = px.request("PATCH", "/v1/fleet/hooks/gone", {"policy": "open"})
            assert st == 200
            st, _, _ = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 200
    finally:
        srv.shutdown()


def test_with_the_harness():
    srv, port = fake(models=["m1"])
    env = {"TANDEM_BACKENDS": "v", "TANDEM_BACKEND_V_KIND": "vllm",
           "TANDEM_BACKEND_V_URL": f"http://127.0.0.1:{port}", "TANDEM_BACKEND_V_HARNESS": "on"}
    try:
        with Proxy(env=env, conductor=True) as px:
            px.sync()
            seen = []
            px.svc.hooks.builtin("see", lambda e, o: seen.append(e["event"]["content"]) or {
                "action": "modify", "patch": {"temperature": 0.05}})
            _hook(px, {"name": "see", "kind": "builtin", "target": "see", "role": "transform",
                       "stages": ["request.before_engine"], "payload": "full"})
            u = {"role": "user", "content": "Fix the failing test in utils.py FAKE_CLASS "
                                            "code_edit"}
            st, _, _ = px.post("/v1/chat/completions", {
                "model": "m1", "messages": [{"role": "system", "content": "You code."}, u],
                "stream": True}, headers={"User-Agent": "opencode/1.0 ai-sdk/5"})
            assert st == 200
            main = [b for b in srv.fake.bodies if not b.get("structured_outputs")][-1]
            assert main["temperature"] == 0.05
            assert "<runtime-note>" in main["messages"][1]["content"], main["messages"][1]
            assert "<runtime-note>" in seen[0]["messages"][1]["content"]
    finally:
        srv.shutdown()


def test_runner_fault_relays_as_is():
    from proxy import hooks as hooks_mod
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            body = _chat("hello FAKE_REASONING", stream=True)
            plain = px.post("/v1/chat/completions", body)[2]
            whole = px.post("/v1/chat/completions", _chat("hi"))[2]
            _hook(px, {"name": "o", "kind": "builtin", "target": "noop", "role": "observer",
                       "stages": ["stream.chunk", "response.after_engine",
                                  "request.before_engine"]})

            def boom(*a, **k):
                raise RuntimeError("inside the runner")
            saved = {n: getattr(hooks_mod.Turn, n) for n in ("see", "answer", "before_engine")}
            for n in saved:
                setattr(hooks_mod.Turn, n, boom)
            try:
                st, _, got = px.post("/v1/chat/completions", body)
                assert st == 200 and got == plain
                st, _, got = px.post("/v1/chat/completions", _chat("hi"))
                assert st == 200 and got == whole
            finally:
                for n, f in saved.items():
                    setattr(hooks_mod.Turn, n, f)
            assert any("[hooks]" in m and "failed" in m for m in px.logs)
    finally:
        srv.shutdown()


if __name__ == "__main__":
    run(globals())
