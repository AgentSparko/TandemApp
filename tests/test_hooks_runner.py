"""The hook runner's rules (SRV-849, QA SRV-850), on a config database and builtin hooks, no relay.

  1  order: priority, ties by id                                    test_order_by_priority_then_id
  2  transforms chain; a patch's null removes                       test_transforms_chain
  3  guards run after the transforms; the first deny wins           test_guard_after_transforms_deny_wins
  4  observers see the final value, off the path, one retry         test_observers_see_final_value
  5  deciders: the first decide wins                                test_first_decider_wins
  6  a stage's budget caps a hook; the 800 ms cap before the first
     token (a hook with no time left is not called)                 test_budget_and_cap
  7  timeouts per policy: open goes on, closed refuses; a guard is
     closed by default                                              test_timeout_open_and_closed
  8  breaker: 5 in a row open it for 30 s, then one probe (success
     closes, failure opens again); requests flow meanwhile          test_breaker_consecutive
  9  breaker: p95 at the timeout over 20 calls in 60 s              test_breaker_p95
 10  half-open lets one probe through at a time                     test_breaker_one_probe
 11  shadow: result and diff journaled, nothing applied             test_shadow_journals_applies_nothing
 12  scope: models, backends, profiles, users, sessions, kinds      test_scope
 13  invalid results refused and journaled, counted as failures     test_invalid_results
 14  mode off and disabled: never called; the runner idles          test_off_and_disabled_idle
 15  the registry refuses plugin, mcp and inproc for now, and
     roles a stage does not take                                    test_registry_refusals
 16  every stage's event and a decision.ask seam                    test_unwired_stages_callable
 17  the first requests at once on a fresh runner never deadlock
     with the config database's lock                                test_first_use_concurrent

Run: python tests/test_hooks_runner.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooks_util import Clock, Store, builtin, ctx, make, sleeper  # noqa: E402

from proxy import hooks as H  # noqa: E402
from proxy import hookstages as S  # noqa: E402
from proxy.hookstore import HookError  # noqa: E402

BODY = {"model": "m-a", "temperature": 0.9, "messages": [{"role": "user", "content": "hi"}]}


def meta(v):
    return {"params": {k: v[k] for k in ("temperature", "top_p") if k in v}} \
        if isinstance(v, dict) else {}


def _run(runner, stage, value=None, turn=None, **kw):
    return runner.run(stage, kw.pop("c", ctx()), BODY if value is None else value, meta,
                      turn=turn, **kw)


def test_order_by_priority_then_id():
    with Store() as (cfg, r):
        order = []
        for name in ("a", "b", "c"):
            builtin(r, f"rec_{name}", lambda env, o, n=name: order.append(n) or
                    {"action": "continue"})
        make(r, id="zz", target="rec_a", role="transform", stages=["request.received"],
             priority=5)
        make(r, id="aa", target="rec_b", role="transform", stages=["request.received"],
             priority=10)
        make(r, id="ab", target="rec_c", role="transform", stages=["request.received"],
             priority=10)
        _run(r, "request.received")
        assert order == ["a", "b", "c"], order


def test_transforms_chain():
    with Store() as (cfg, r):
        seen = builtin(r, "second", lambda env, o: {"action": "modify",
                                                    "patch": {"top_p": 0.5,
                                                              "temperature": None}})
        builtin(r, "first", lambda env, o: {"action": "modify", "patch": {"temperature": 0.1}})
        make(r, id="t1", target="first", role="transform", stages=["request.received"],
             priority=1)
        make(r, id="t2", target="second", role="transform", stages=["request.received"],
             priority=2)
        out = _run(r, "request.received")
        assert seen[0]["event"]["params"] == {"temperature": 0.1}, seen[0]["event"]
        assert out.changed and "temperature" not in out.value and out.value["top_p"] == 0.5
        assert BODY["temperature"] == 0.9                          # the input is never mutated


def test_guard_after_transforms_deny_wins():
    with Store() as (cfg, r):
        seen = builtin(r, "g", lambda env, o: {"action": "deny", "reason": "too hot"}
                       if env["event"]["params"].get("temperature") == 0.1
                       else {"action": "continue"})
        late = builtin(r, "g2", lambda env, o: {"action": "continue"})
        builtin(r, "t", lambda env, o: {"action": "modify", "patch": {"temperature": 0.1}})
        # the guard's priority is lower than the transform's: guards still run after it
        make(r, id="guard", target="g", role="guard", stages=["request.before_engine"],
             priority=-5)
        make(r, id="guard2", target="g2", role="guard", stages=["request.before_engine"],
             priority=50)
        make(r, id="tr", target="t", role="transform", stages=["request.before_engine"],
             priority=10)
        out = _run(r, "request.before_engine")
        assert out.deny == {"hook": "guard", "status": 403, "code": "hook_denied",
                            "message": "too hot"}, out.deny
        assert len(seen) == 1 and not late                         # the first deny wins


def test_observers_see_final_value():
    with Store() as (cfg, r):
        seen = builtin(r, "obs", lambda env, o: {"action": "continue"})
        tries = []

        def flaky(env, o):
            tries.append(1)
            raise RuntimeError("down")
        r.builtin("flaky", flaky)
        builtin(r, "t", lambda env, o: {"action": "modify", "patch": {"temperature": 0.2}})
        make(r, id="o1", target="obs", role="observer", stages=["request.received"],
             priority=-100, payload="full")
        make(r, id="o2", target="flaky", role="observer", stages=["request.received"])
        make(r, id="t", target="t", role="transform", stages=["request.received"], priority=50)
        out = _run(r, "request.received")
        assert out.value["temperature"] == 0.2
        r.bg.drain(5)
        assert seen[0]["event"]["content"]["temperature"] == 0.2, seen
        assert len(tries) == 2                                     # one retry off the path


def test_first_decider_wins():
    with Store() as (cfg, r):
        make(r, id="d1", target="route", role="decider", stages=["route.select"], priority=1,
             options={"backend": "b2"})
        make(r, id="d2", target="route", role="decider", stages=["route.select"], priority=2,
             options={"backend": "b3"})
        out = r.run("route.select", ctx(), None, lambda v: {"route": {}})
        assert out.decision == {"backend": "b2"} and out.decided_by == "d1"
        assert r.calls("d2") == []                                 # not asked


def test_budget_and_cap():
    with Store() as (cfg, r):
        calls = builtin(r, "slow", sleeper(0.4))
        # a 2 s timeout is capped at the stage's 150 ms budget
        make(r, id="s", target="slow", role="transform", stages=["request.received"],
             timeout_ms=2000)
        turn = H.Turn(r, ctx())
        t0 = time.monotonic()
        out = _run(r, "request.received", turn=turn)
        took = time.monotonic() - t0
        assert out.deny is None and not out.changed and took < 0.35, took
        assert r.calls("s")[0]["outcome"] == "timeout"
        assert 140 <= turn.spent <= 320, turn.spent
        # the 800 ms cap: with 700 ms spent, a hook of response.after_engine (1 s budget) gets 100
        make(r, id="late", target="slow", role="transform", stages=["response.after_engine"])
        turn = H.Turn(r, ctx())
        turn.spend("request.received", 700)
        out = r.run("response.after_engine", ctx(), {"choices": []}, lambda v: {}, turn=turn)
        assert out.deny is None
        c = r.calls("late")[0]
        assert c["outcome"] == "cap" and c["duration_ms"] < 300, c
        # nothing left: the hook is not called at all
        n = len(calls)
        turn.spend("request.before_engine", 200)
        assert turn.left("response.after_engine") <= 0
        r.run("response.after_engine", ctx(), {"choices": []}, lambda v: {}, turn=turn)
        assert len(calls) == n and r.calls("late")[0]["outcome"] == "cap"
        # a cap is not the hook's failure: its breaker stays closed
        assert r.metrics("late")["breaker"]["state"] == "closed"


def test_timeout_open_and_closed():
    with Store() as (cfg, r):
        builtin(r, "slow", sleeper(0.3))
        make(r, id="open", target="slow", role="transform", stages=["request.received"],
             policy="open")
        assert _run(r, "request.received").deny is None
        r.store.update("open", {"policy": "closed"}, "test")
        d = _run(r, "request.received").deny
        assert d["status"] == 503 and d["code"] == "hook_unavailable" and d["hook"] == "open"
        r.store.delete("open", "test")
        # a guard with no policy is closed
        builtin(r, "boom", lambda env, o: 1 / 0)
        make(r, id="g", target="boom", role="guard", stages=["request.received"])
        assert r.policy(r.store.get("g"), "request.received") == "closed"
        d = _run(r, "request.received").deny
        assert d["code"] == "hook_unavailable", d
        assert r.calls("g")[0]["outcome"] == "error"


def test_breaker_consecutive():
    with Store() as (cfg, r):
        clock = Clock()
        r.clock = clock
        calls = builtin(r, "slow", sleeper(0.2))
        make(r, id="b", target="slow", role="transform", stages=["request.received"],
             timeout_ms=150, policy="open")
        for _ in range(5):
            assert _run(r, "request.received").deny is None
        assert len(calls) == 5 and r.metrics("b")["breaker"]["state"] == "open"
        assert r.metrics("b")["timeouts"] == 5
        # open: the request flows without it, the hook is not called
        assert _run(r, "request.received").deny is None
        assert len(calls) == 5 and r.calls("b")[0]["outcome"] == "breaker_open"
        clock.advance(29)
        _run(r, "request.received")
        assert len(calls) == 5
        clock.advance(2)                                           # 31 s: one probe
        assert r.metrics("b")["breaker"]["state"] == "half_open"
        _run(r, "request.received")
        assert len(calls) == 6 and r.metrics("b")["breaker"]["state"] == "open"
        clock.advance(31)
        r.builtin("slow", lambda env, o: calls.append(env) or {"action": "continue"})
        _run(r, "request.received")                                # the probe succeeds
        assert len(calls) == 7 and r.metrics("b")["breaker"]["state"] == "closed"
        _run(r, "request.received")
        assert len(calls) == 8


def test_breaker_p95():
    with Store() as (cfg, r):
        r.clock = Clock()
        n = [0]

        def mostly_fast(env, o):
            n[0] += 1
            if n[0] % 10 == 0:
                time.sleep(0.08)
            return {"action": "continue"}
        r.builtin("mf", mostly_fast)
        make(r, id="p", target="mf", role="observer", stages=["request.finished"],
             timeout_ms=60)
        hk = r.store.get("p")
        for i in range(19):
            r.call(hk, "request.finished", ctx(), {}, None, timeout_ms=60, mode="on")
        # 19 calls, one at the timeout and never two failures in a row: still closed
        assert r.metrics("p")["breaker"]["state"] == "closed"
        r.call(hk, "request.finished", ctx(), {}, None, timeout_ms=60, mode="on")
        # 20 calls, two (10 %) at the timeout: the p95 is at the timeout
        assert r.metrics("p")["breaker"]["state"] == "open", r.metrics("p")


def test_breaker_one_probe():
    b = H.Breaker(Clock())
    for _ in range(5):
        b.record(False, 1, 100, False)
    assert b.allow() is None
    b.clock.advance(31)
    got = []
    ts = [threading.Thread(target=lambda: got.append(b.allow())) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got, key=str) == [None] * 7 + ["probe"], got
    b.record(True, 1, 100, True)
    assert b.allow() == "call"


def test_shadow_journals_applies_nothing():
    with Store() as (cfg, r):
        builtin(r, "t", lambda env, o: {"action": "modify",
                                        "patch": {"temperature": 0.0, "seed": 7}})
        builtin(r, "g", lambda env, o: {"action": "deny", "reason": "would refuse"})
        make(r, id="ts", target="t", role="transform", stages=["request.received"],
             mode="shadow")
        make(r, id="gs", target="g", role="guard", stages=["request.received"], mode="shadow")
        out = _run(r, "request.received", c=ctx(request_id="r-shadow"))
        assert not out.changed and out.deny is None and out.value is BODY
        r.bg.drain(5)
        e = r.calls("ts")[0]
        assert e["mode"] == "shadow" and e["action"] == "modify" and \
            e["request"] == "r-shadow", e
        assert e["diff"] == {"temperature": {"op": "replace", "bytes": 3},
                             "seed": {"op": "add", "bytes": 1}}, e["diff"]
        assert r.calls("gs")[0]["diff"] == {"deny": True}
        assert all("value" not in d for d in e["diff"].values())  # sizes, never a value


def test_scope():
    with Store() as (cfg, r):
        seen = builtin(r, "s", lambda env, o: {"action": "continue"})
        make(r, id="sc", target="s", role="transform", stages=["request.received"],
             scope={"models": ["qwen*"], "backends": ["b1"], "users": ["team-*"],
                    "client_kinds": ["opencode"], "sessions": ["s-*"],
                    "profiles": ["coding"]})
        ok = ctx(model="qwen-3", backend="b1", user="team-a", client_kind="opencode",
                 profile="coding")
        _run(r, "request.received", c=ok)
        assert len(seen) == 1
        for k, v in (("model", "llama"), ("backend", "b2"), ("user", "guest"),
                     ("client_kind", "owui"), ("session", "x-1"), ("profile", None)):
            _run(r, "request.received", c=dict(ok, **{k: v}, upstream_model=(
                v if k == "model" else ok["upstream_model"])))
            assert len(seen) == 1, k
        # the backend's own model id matches too
        _run(r, "request.received", c=dict(ok, model="alias", upstream_model="qwen-3"))
        assert len(seen) == 2


def test_invalid_results():
    with Store() as (cfg, r):
        cases = {
            "field": {"action": "modify", "patch": {"stream": False}},
            "content": {"action": "modify", "patch": {"messages": []}},
            "role": {"action": "deny"},
            "shape": {"action": "maybe"},
            "extra": {"action": "continue", "why": 1},
            "notdict": ["continue"],
        }
        for name, res in cases.items():
            r.builtin(name, lambda env, o, res=res: res)
            make(r, id=name, target=name, role="transform", stages=["request.received"])
        out = _run(r, "request.received")
        assert not out.changed and out.deny is None
        for name in cases:
            e = r.calls(name)[0]
            assert e["outcome"] == "invalid", (name, e)
            assert r.metrics(name)["failures"] == 1
        assert "content" in r.calls("content")[0]["error"]
        # a decision key the stage does not take
        r.builtin("dk", lambda env, o: {"action": "decide", "decision": {"retry": True}})
        make(r, id="dk", target="dk", role="decider", stages=["route.select"])
        assert r.run("route.select", ctx(), None, lambda v: {}).decision is None
        assert r.calls("dk")[0]["outcome"] == "invalid"
        # an observer may only continue
        r.builtin("om", lambda env, o: {"action": "modify", "patch": {"temperature": 1}})
        make(r, id="om", target="om", role="observer", stages=["request.received"])
        _run(r, "request.received")
        r.bg.drain(5)
        assert r.calls("om")[0]["outcome"] == "invalid"


def test_off_and_disabled_idle():
    with Store() as (cfg, r):
        assert r.idle() and r.turn(ctx()) is None
        seen = builtin(r, "s", lambda env, o: {"action": "continue"})
        make(r, id="off", target="s", role="transform", stages=["request.received"],
             mode="off")
        make(r, id="dis", target="s", role="transform", stages=["request.received"],
             enabled=False)
        assert r.idle() and r.turn(ctx()) is None
        _run(r, "request.received")
        assert not seen
        r.store.update("dis", {"enabled": True}, "test")
        assert not r.idle() and r.turn(ctx()) is not None


def test_registry_refusals():
    with Store() as (cfg, r):
        for kind in ("plugin", "mcp", "inproc"):
            try:
                r.store.create({"name": kind, "kind": kind, "role": "observer",
                                "stages": ["request.finished"]}, "test")
                raise AssertionError(kind)
            except HookError as exc:
                assert exc.code == "kind_unavailable" and "not available" in exc.message
        for doc, field in (({"role": "guard", "stages": ["stream.chunk"]}, "stages"),
                           ({"role": "decider", "stages": ["request.received"]}, "stages"),
                           ({"role": "transform", "stages": ["nope"]}, "stages"),
                           ({"role": "transform", "stages": ["request.received"],
                             "timeout_ms": 0}, "timeout_ms"),
                           ({"role": "transform", "stages": ["request.received"],
                             "target": "missing"}, "target")):
            try:
                make(r, **{"target": "noop", **doc})
                raise AssertionError(doc)
            except HookError as exc:
                assert exc.field == field, (doc, exc.field)
        # the guard's default policy, the stage table's role words
        assert S.default_policy("request.received", "guard") == "closed"
        assert S.default_policy("request.received", "transform") == "open"
        assert S.STAGES["route.select"]["actions"] == ["continue", "decide"]


def test_unwired_stages_callable():
    with Store() as (cfg, r):
        seen = builtin(r, "ans", lambda env, o: {"action": "decide",
                                                 "decision": {"answer": "code"}})
        make(r, id="ask", target="ans", role="decider", stages=["decision.ask"])
        assert r.ask(ctx(), {"state": "class", "options": ["chat", "code"]}) == \
            {"answer": "code"}
        assert seen[0]["event"] == {"question": {"state": "class",
                                                 "options": ["chat", "code"]}}
        lc = builtin(r, "lc", lambda env, o: {"action": "continue"})
        make(r, id="life", target="lc", role="observer", stages=["engine.lifecycle"])
        r.notify("engine.lifecycle", {}, {"event": "start", "host": "e1", "detail": {}})
        r.bg.drain(5)
        assert lc[0]["event"]["event"] == "start"
        g = builtin(r, "tg", lambda env, o: {"action": "deny", "reason": "no shell"})
        make(r, id="tool", target="tg", role="guard", stages=["tool.before_exec"])
        out = r.run("tool.before_exec", ctx(), {"cmd": "ls"},
                    lambda v: {"tool": "sh", "call_id": "c1", "args_bytes": 12})
        assert out.deny["message"] == "no shell" and "content" not in g[0]["event"]


def test_first_use_concurrent():
    for _ in range(30):
        st = Store()
        cfg, r = st.__enter__()
        if True:
            start = threading.Barrier(24)

            def first(i):
                start.wait()
                if i % 3 == 0:
                    r.store.data_version()
                elif i % 3 == 1:
                    r.hooks()
                else:
                    cfg.key_for("none")                            # the relay's own read
                r.idle()
            ts = [threading.Thread(target=first, args=(i,), daemon=True) for i in range(24)]
            for t in ts:
                t.start()
            end = time.monotonic() + 10
            for t in ts:
                t.join(max(0.0, end - time.monotonic()))
            # a hung thread holds the config lock: fail without closing the database
            assert not any(t.is_alive() for t in ts), "a first request hung (a lock cycle)"
        st.__exit__(None, None, None)


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
