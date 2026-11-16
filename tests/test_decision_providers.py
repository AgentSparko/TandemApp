"""Decision providers (SRV-851, QA SRV-852): the registry, the calls, health, shadow, A/B, replay,
through a controller-style conductor and the example systemone server.

  1  no provider (none stored, or only disabled ones and ones for other
     questions, or one in shadow): every decision of the conductor's
     fixtures is what it is without providers                    test_no_provider_same_decisions
  2  recorded answers: a replay decides as the recorded run, every time,
     with the provider gone                                       test_replay_is_deterministic
  3  a key and an endpoint per question: the call goes there with that
     key, not to the default; the provider's key never goes to another
     origin; a rotated key is the next call's                     test_key_and_endpoint_per_question
  4  the registry: create, change, delete, validation, one provider a
     question on or ab, keys write-only, audit without keys       test_registry
  5  a hook provider: Runner.ask with the question's shape; its event
     validates; meta carries no text; an http hook adapts another API;
     an unknown id or a slow hook falls back                      test_hook_provider
  6  a provider that does not answer within 400 ms: the built-in answer
     is used, the health view says failing; five failures open the
     breaker (calls skip at once), one probe closes it; a probe that
     fails says down and skips calls                              test_health_fallback_breaker
  7  an unknown label, an HTTP error, a refused key: the built-in    test_unknown_label_and_errors
  8  shadow: asked beside, journaled with agreement and time, the
     decision unchanged                                           test_shadow_journals_changes_nothing
  9  A/B: sticky per session (or user), the split near its share,
     arm b the provider's, arm a the built-in's, both journaled    test_ab_sticky_and_split
 10  the agreement report: per question calls, agreement, p50/p95,
     failures, fallbacks by reason, from the journal              test_agreement_report
 11  the systemone wire: the request the controller sends and every
     answer of the example server against the pinned schema       test_systemone_wire_schema
 12  the judge and the memory writer's gate ask through providers  test_judge_and_memory_gate

Run: python tests/test_decision_providers.py
"""

from __future__ import annotations

import collections
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooks_util import CONTRACT, Clock, builtin, hook_server, make, sleeper  # noqa: E402
from provider_util import (KEYS, PROMPTS, Rig, decision, example, last_option,  # noqa: E402
                           plan, schema_errors)
from proxy_util import run  # noqa: E402

from proxy import providers as P  # noqa: E402
from proxy.providerstore import ProviderError  # noqa: E402
from tandemapp.conductor import classify  # noqa: E402
from tandemapp.conductor import memwriter as mw  # noqa: E402
from tools import contract_check  # noqa: E402

GATE_TEXT = "From now on always use tabs, not spaces, in this project."


def _decisions(cond, n: int = len(PROMPTS)) -> list:
    return [decision(plan(cond, t, sid=f"s-{i}", rid=f"r-{i}"))
            for i, t in enumerate(PROMPTS[:n])]


def _gate(cond, text=GATE_TEXT, conv="s-gate"):
    """The memory writer's question, as MemWriter.gate asks it."""
    _, router_doc = cond.router.store.peek("router")
    _, roles = cond.router.store.peek("roles")
    clf = router_doc["classifier"]
    stages = [dict(st, enabled=True) for st in clf["stages"] if st["kind"] == "decider"]
    doc = dict(router_doc, classifier=dict(clf, stages=stages))
    classify.CONTEXT.set({"request": None, "session": conv, "user": None})
    ans, st = cond.router.cascade(text, mw.GATE_OPTIONS, doc, roles,
                                  question=mw.GATE_QUESTION, accept=0.6, qid="memory_gate")
    return (ans.label if ans else None), [{k: v for k, v in s.items() if k != "ms"} for s in st]


def _judge(cond):
    q = [{"qid": "failure", "question": "Does this tool output show a failure?",
          "options": {"failure": "a traceback, a failed test, an error line or a crash",
                      "clean": "it ran and shows no failure",
                      "unclear": "it cannot be told from this output"},
          "digest": "Traceback (most recent call last): KeyError 'x'\n1 failed, 3 passed",
          "twin": "failure", "deadline_ms": 400}]
    rows = cond.judge.ask("s-judge", "r-judge", q)
    return [(r["decision"], {k: v for k, v in r["detail"].items() if k != "ms"}) for r in rows]


def _wait(pred, s=10.0):
    end = time.time() + s
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------------- 1
def test_no_provider_same_decisions():
    base_srv, bport = example()
    other, oport = example(answer=last_option)
    try:
        url = f"http://127.0.0.1:{bport}"
        with Rig() as rig:
            ref = rig.conductor(decider=url, tester=url, providers=False)
            want = (_decisions(ref), _gate(ref), _judge(ref))
            assert want[0][0]["class"] and want[0][0]["stages"][0]["stage"] == "decider", want[0]
            assert len({d["class"] for d in want[0]}) >= 3, [d["class"] for d in want[0]]
            assert "provider" not in json.dumps(want)
            # an empty registry
            assert _decisions_all(rig, url) == want
            # disabled providers and providers of other questions only
            ep = f"http://127.0.0.1:{oport}"
            rig.add(id="off", endpoint=ep, questions={"classify": {}, "memory_gate": {},
                                                       "judge": {}}, mode="on", enabled=False)
            rig.add(id="elsewhere", endpoint=ep, questions={"deliverable": {}}, mode="on")
            assert _decisions_all(rig, url) == want
            assert not [c for c in other.calls if c["path"] == "/v1/systemone"]
            # a provider in shadow: asked beside, the decisions as they were
            rig.add(id="watch", endpoint=ep, mode="shadow",
                    questions={"classify": {}, "memory_gate": {}, "judge": {}})
            assert _decisions_all(rig, url) == want
            rig.ps.bg.drain(10)
            asked = [c for c in other.calls if c["path"] == "/v1/systemone"]
            assert len(asked) == len(PROMPTS) + 2, len(asked)
    finally:
        base_srv.shutdown()
        other.shutdown()
    return f"{len(PROMPTS)} prompts, the gate, the judge: equal 4 ways"


def _decisions_all(rig, url):
    c = rig.conductor(decider=url, tester=url)
    return (_decisions(c), _gate(c), _judge(c))


# ---------------------------------------------------------------------------------- 2
def test_replay_is_deterministic():
    base_srv, bport = example()
    prov, pport = example(answer=last_option, model="other-decider")
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            rig.add(id="jev", endpoint=f"http://127.0.0.1:{pport}", mode="on",
                    questions={"classify": {}, "memory_gate": {}, "judge": {}})
            c = rig.conductor(decider=url, tester=url)
            recorded = (_decisions(c), _gate(c), _judge(c))
            served = [s for d in recorded[0] for s in d["stages"] if s.get("provider")]
            assert len(served) == len(PROMPTS), recorded[0]
            assert all(s["provider"] == {"id": "jev", "mode": "on"} for s in served)
            assert all(s["model"] == "other-decider" for s in served)
            replay = P.Recorded.from_records(list(rig.ps.ring))
            assert len(replay.answers) == len(PROMPTS) + 2, len(replay.answers)
            doc = replay.dump()
            assert json.loads(json.dumps(doc)) == doc
    finally:
        prov.shutdown()
    try:
        for i in range(3):                    # the provider is gone: the answers are the record's
            with Rig() as rig:
                rig.add(id="jev", endpoint=f"http://127.0.0.1:{pport}", mode="on",
                        questions={"classify": {}, "memory_gate": {}, "judge": {}})
                rig.ps.replay = P.Recorded(doc["answers"], doc["limits"])
                c = rig.conductor(decider=url, tester=url)
                got = (_decisions(c), _gate(c), _judge(c))
                assert got == recorded, (i, got, recorded)
                assert not [r for r in rig.ps.ring if (r["answer"] or {}).get("error")]
        # a question the record does not hold falls back to the built-in, as a failure would
        with Rig() as rig:
            rig.add(id="jev", endpoint=f"http://127.0.0.1:{pport}", mode="on",
                    questions={"classify": {}})
            rig.ps.replay = P.Recorded({}, {})
            c = rig.conductor(decider=url)
            d = decision(plan(c, PROMPTS[0]))
            assert d["stages"][0]["provider"]["fallback"] == "replay_miss", d
    finally:
        base_srv.shutdown()
    return "3 replays equal to the record"


# ---------------------------------------------------------------------------------- 3
def test_key_and_endpoint_per_question():
    dflt, dport = example(key=KEYS[0], model="default-decider")
    qsrv, qport = example(key=KEYS[1], model="classify-decider")
    far, fport = example(model="far-decider")
    try:
        with Rig() as rig:
            dep = f"http://127.0.0.1:{dport}"
            rig.add(id="jev", endpoint=dep, mode="on", api_key=KEYS[0],
                    questions={"classify": {"endpoint": f"http://127.0.0.1:{qport}",
                                            "api_key": KEYS[1], "model": "classify-decider"},
                               "memory_gate": {},
                               "judge": {"endpoint": f"http://localhost:{fport}"}})
            c = rig.conductor()
            d = decision(plan(c, PROMPTS[0]))
            assert d["stages"][0]["model"] == "classify-decider", d
            one = [x for x in qsrv.calls if x["path"] == "/v1/systemone"]
            assert len(one) == 1 and one[0]["auth"] == f"Bearer {KEYS[1]}", one
            assert one[0]["body"]["model"] == "classify-decider"
            assert not [x for x in dflt.calls if x["path"] == "/v1/systemone"], dflt.calls
            # a question with no endpoint of its own: the default endpoint and key
            label, st = _gate(c)
            assert st[0]["model"] == "default-decider", st
            two = [x for x in dflt.calls if x["path"] == "/v1/systemone"]
            assert len(two) == 1 and two[0]["auth"] == f"Bearer {KEYS[0]}"
            # another origin and no key of its own: the provider's key does not go there
            _judge(c)
            three = [x for x in far.calls if x["path"] == "/v1/systemone"]
            assert len(three) == 1 and three[0]["auth"] is None, three
            # a key rotated mid-run is the next call's
            rig.ps.store.update("jev", {"api_key": KEYS[2]}, "test")
            dflt.key = KEYS[2]
            _gate(c, conv="s-gate-2")
            last = [x for x in dflt.calls if x["path"] == "/v1/systemone"][-1]
            assert last["auth"] == f"Bearer {KEYS[2]}", last
            assert rig.ps.ring[-1]["answer"]["error"] is None, rig.ps.ring[-1]
            for k in KEYS:
                assert k not in json.dumps(list(rig.ps.ring)), k
                assert k not in "\n".join(rig.logs), k
    finally:
        dflt.shutdown()
        qsrv.shutdown()
        far.shutdown()
    return "per-question endpoint and key, origin rule, rotation"


# ---------------------------------------------------------------------------------- 4
def test_registry():
    with Rig() as rig:
        st = rig.ps.store

        def bad(code, field, **doc):
            doc.setdefault("name", "x")
            try:
                st.create(doc, "test")
            except ProviderError as exc:
                assert exc.status == code and (field is None or exc.field == field), \
                    (exc.status, exc.field, exc.message)
                return exc
            raise AssertionError(f"accepted {doc}")
        ep = "http://127.0.0.1:9"
        bad(422, "kind", kind="grpc", endpoint=ep, questions={"classify": {}})
        bad(422, "endpoint", endpoint="ftp://x", questions={"classify": {}})
        bad(422, "endpoint", endpoint="http://user:pw@host/", questions={"classify": {}})
        bad(422, "questions", endpoint=ep, questions={})
        bad(422, "questions.nope", endpoint=ep, questions={"nope": {}})
        bad(422, "questions.classify", endpoint=ep, questions={"classify": {"x": 1}})
        bad(422, "mode", endpoint=ep, questions={"classify": {}}, mode="always")
        bad(422, "ab_percent", endpoint=ep, questions={"classify": {}}, ab_percent=101)
        bad(422, "sticky", endpoint=ep, questions={"classify": {}}, sticky="ip")
        bad(422, "timeout_ms", endpoint=ep, questions={"classify": {}}, timeout_ms=0)
        bad(422, "api_key", endpoint=ep, questions={"classify": {}}, api_key="a b")
        bad(422, "hook_id", kind="hook", hook_id="none", questions={"classify": {}})
        bad(422, "api_key", kind="builtin", questions={"classify": {}}, api_key="k")
        bad(422, "questions.classify.endpoint", kind="builtin",
            questions={"classify": {"endpoint": ep}})
        p = st.create({"name": "My Jev", "endpoint": ep, "api_key": KEYS[0], "mode": "on",
                       "questions": {"classify": {"api_key": KEYS[1], "model": "m-1"},
                                     "packages": {}}}, "owner")
        assert p["id"] == "my-jev" and p["questions"] == {"classify": {"model": "m-1"},
                                                          "packages": {}}, p
        v = rig.ps.view(p)
        assert v["key"]["key_set"] and v["key"]["last4"] == KEYS[0][-4:]
        assert v["questions"]["classify"]["key"]["last4"] == KEYS[1][-4:]
        assert not v["questions"]["packages"]["key"]["key_set"]
        # one provider a question on or ab; shadows are free
        exc = bad(409, "questions", endpoint=ep, questions={"packages": {}}, mode="ab",
                  ab_percent=10)
        assert exc.code == "question_taken"
        st.create({"name": "s1", "endpoint": ep, "questions": {"packages": {}},
                   "mode": "shadow"}, "t")
        st.create({"name": "s2", "endpoint": ep, "questions": {"packages": {}},
                   "mode": "shadow"}, "t")
        try:
            st.update("s1", {"mode": "on"}, "t")
            raise AssertionError("two on")
        except ProviderError as exc:
            assert exc.status == 409
        try:
            st.update("my-jev", {"kind": "hook"}, "t")
            raise AssertionError("kind changed")
        except ProviderError as exc:
            assert exc.field == "kind"
        # a question dropped takes its key; null clears the provider's key
        p = st.update("my-jev", {"questions": {"packages": {}}, "api_key": None}, "owner")
        assert st.key_hints("my-jev") == {}, st.key_hints("my-jev")
        assert p["version"] == 2
        p = st.update("my-jev", {"questions": {"classify": {"api_key": KEYS[2]},
                                               "packages": {}}}, "owner")
        assert st.key_for("my-jev", "classify") == KEYS[2]
        assert st.update("my-jev", {"name": "My Jev"}, "owner")["version"] == p["version"]
        st.delete("s2", "owner")
        assert st.get("s2") is None and [x["id"] for x in st.list()] == ["my-jev", "s1"]
        # the audit names fields, never a key; the database holds ciphertext only
        rows = rig.cfg._conn.execute("SELECT * FROM audit WHERE object = "
                                     "'decision_provider'").fetchall()
        assert len(rows) >= 6 and not any(k in json.dumps(rows, default=str) for k in KEYS)
        dump = "\n".join(rig.cfg._conn.iterdump())
        assert not any(k in dump for k in KEYS)
        assert "api_key" in json.dumps(rows, default=str)
        # a hook provider names a decider hook at decision.ask
        make(rig.runner, id="adapter", target="noop", role="decider", stages=["decision.ask"])
        make(rig.runner, id="obs", target="noop", role="observer", stages=["decision.ask"])
        bad(422, "hook_id", kind="hook", hook_id="obs", questions={"judge": {}})
        h = st.create({"name": "h", "kind": "hook", "hook_id": "adapter",
                       "questions": {"judge": {}}, "mode": "on"}, "t")
        assert rig.ps.view(h)["key"] is None
    return "validation, the one-provider rule, keys write-only"


# ---------------------------------------------------------------------------------- 5
def test_hook_provider():
    base_srv, bport = example()
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            seen = builtin(rig.runner, "laya2", lambda env, o: {
                "action": "decide", "decision": {"answer": {
                    "choice": sorted(env["event"]["question"]["options"])[0],
                    "confidence": 0.95}}})
            make(rig.runner, id="adapter", target="laya2", role="decider",
                 stages=["decision.ask"], payload="full")
            rig.add(id="hp", kind="hook", hook_id="adapter", mode="on", model="laya-2",
                    questions={"classify": {}, "memory_gate": {"model": "laya-gate"}})
            c = rig.conductor(decider=url)
            d = decision(plan(c, PROMPTS[0]))
            s0 = d["stages"][0]
            assert s0["provider"] == {"id": "hp", "mode": "on"} and s0["model"] == \
                "hook:adapter", s0
            ev = seen[0]
            assert not contract_check.validate(ev, contract_check.load("call.schema.json",
                                                                       CONTRACT), root=CONTRACT)
            assert not contract_check.validate(
                ev["event"], contract_check.load("events/decision.ask.schema.json", CONTRACT),
                root=CONTRACT), ev["event"]
            q = ev["event"]["question"]
            assert q["id"] == "classify" and q["model"] == "laya-2" and q["deadline_ms"] == 400
            assert ev["event"]["content"]["state"].startswith("Rename the variable")
            assert ev["request"]["request_id"] == "r-1" and ev["request"]["session"]
            _gate(c)
            assert seen[1]["event"]["question"]["model"] == "laya-gate"
            # meta: the question without the text
            rig.runner.store.update("adapter", {"payload": "meta"}, "t")
            decision(plan(c, PROMPTS[1], sid="s-meta"))
            assert "content" not in seen[-1]["event"], seen[-1]["event"]
            # an id that is not an option, and a bare id
            builtin(rig.runner, "laya2", lambda env, o: {"action": "decide",
                                                         "decision": {"answer": "nope"}})
            d = decision(plan(c, PROMPTS[2], sid="s-x"))
            assert d["stages"][0]["provider"]["fallback"] == "unknown_label", d["stages"]
            assert d["stages"][0]["model"] == "example-decider"           # the built-in's
            builtin(rig.runner, "laya2", lambda env, o: {"action": "decide",
                                                         "decision": {"answer": "chat"}})
            d = decision(plan(c, PROMPTS[2] + " Again.", sid="s-y"))
            assert d["stages"][0]["label"] == "chat" and d["stages"][0]["confidence"] == 1.0
            # a slow hook: the stage's 400 ms, then the built-in
            builtin(rig.runner, "laya2", sleeper(0.8, {"action": "decide",
                                                       "decision": {"answer": "chat"}}))
            t0 = time.time()
            d = decision(plan(c, PROMPTS[3], sid="s-slow"))
            assert d["stages"][0]["provider"]["fallback"] in ("timeout", "no_answer"), d
            assert time.time() - t0 < 1.5
            # a hook switched off: the built-in, said so
            rig.runner.store.update("adapter", {"mode": "off"}, "t")
            d = decision(plan(c, PROMPTS[4], sid="s-off"))
            assert d["stages"][0]["provider"]["fallback"] == "hook_off", d
        # an http hook adapting another API's answer shape
        hs, hport = hook_server()

        def foreign(env):
            q = env["event"]["question"]
            other = {"verdict": {"top": list(q["options"])[1], "p": 0.88}}      # their shape
            return {"action": "decide", "decision": {"answer": {
                "choice": other["verdict"]["top"], "confidence": other["verdict"]["p"]}}}
        hs.respond = foreign
        with Rig() as rig:
            h, _ = rig.runner.store.create({"name": "foreign", "endpoint":
                                            f"http://127.0.0.1:{hport}/", "role": "decider",
                                            "stages": ["decision.ask"]}, "t")
            rig.add(id="fx", kind="hook", hook_id=h["id"], mode="on",
                    questions={"classify": {}})
            c = rig.conductor(decider=url)
            d = decision(plan(c, PROMPTS[0]))
            assert d["stages"][0]["confidence"] == 0.88 and \
                d["stages"][0]["model"] == f"hook:{h['id']}", d["stages"]
            assert hs.calls and hs.calls[0]["env"]["stage"] == "decision.ask"
            assert "content" not in hs.calls[0]["env"]["event"]           # meta by default
        hs.shutdown()
    finally:
        base_srv.shutdown()
    return "builtin and http hooks, meta and full, fallbacks"


# ---------------------------------------------------------------------------------- 6
def test_health_fallback_breaker():
    base_srv, bport = example()
    slow, sport = example(model="slow-decider")
    slow.delay_ms = 700
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            clock = Clock()
            rig.ps.clock = clock
            rig.add(id="slow", endpoint=f"http://127.0.0.1:{sport}", mode="on",
                    questions={"classify": {}})
            c = rig.conductor(decider=url)
            ref = rig.conductor(decider=url, providers=False)
            want = decision(plan(ref, PROMPTS[0], sid="s-0"))
            t0 = time.time()
            d = decision(plan(c, PROMPTS[0], sid="s-0"))
            took = time.time() - t0
            s0 = d["stages"][0]
            assert s0["provider"] == {"id": "slow", "mode": "on", "fallback": "timeout"}, s0
            assert s0["label"] == want["stages"][0]["label"] and d["class"] == want["class"]
            assert took < 2.0, took
            h = rig.ps.health("slow")
            assert h["status"] == "failing" and h["endpoints"][0]["probe"]["up"], h
            assert not contract_check.validate(h, contract_check.load(
                "decision-provider-health.schema.json", _CV), root=_CV), h
            for i in range(4):
                decision(plan(c, PROMPTS[i + 1], sid=f"s-{i + 1}"))
            assert rig.ps.health("slow")["endpoints"][0]["breaker"]["state"] == "open"
            n = len([x for x in slow.calls if x["path"] == "/v1/systemone"])
            t0 = time.time()
            d = decision(plan(c, PROMPTS[5], sid="s-5"))
            assert time.time() - t0 < 0.5
            assert d["stages"][0]["provider"]["fallback"] == "breaker_open"
            assert len([x for x in slow.calls if x["path"] == "/v1/systemone"]) == n
            # after 30 s one probe; a good answer closes the breaker
            slow.delay_ms = 0
            clock.advance(31)
            d = decision(plan(c, PROMPTS[6], sid="s-6"))
            assert "fallback" not in d["stages"][0]["provider"], d["stages"][0]
            assert rig.ps.health("slow")["endpoints"][0]["breaker"]["state"] == "closed"
            # a provider that stops answering: its probe says down, calls skip it
            slow.shutdown()
            h = rig.ps.health("slow", force=True)
            assert h["status"] == "down" and not h["endpoints"][0]["probe"]["up"], h
            t0 = time.time()
            d = decision(plan(c, PROMPTS[7], sid="s-7"))
            assert d["stages"][0]["provider"]["fallback"] == "provider_down", d["stages"]
            assert time.time() - t0 < 0.5
            assert d["class"] == decision(plan(ref, PROMPTS[7], sid="s-7"))["class"]
    finally:
        base_srv.shutdown()
    return "timeout -> built-in, failing, breaker, probe, down"


_CV = os.path.join(os.path.dirname(CONTRACT), "conductor-v1")


# ---------------------------------------------------------------------------------- 7
def test_unknown_label_and_errors():
    base_srv, bport = example()
    odd, oport = example(answer=lambda s, q: ("banana", {"banana": 0.99}))
    keyed, kport = example(key=KEYS[0])
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            rig.add(id="odd", endpoint=f"http://127.0.0.1:{oport}", mode="on",
                    questions={"classify": {}})
            rig.add(id="nokey", endpoint=f"http://127.0.0.1:{kport}", mode="on",
                    questions={"memory_gate": {}})
            c = rig.conductor(decider=url)
            d = decision(plan(c, PROMPTS[0]))
            assert d["stages"][0]["provider"]["fallback"] == "unknown_label"
            assert d["stages"][0]["model"] == "example-decider", d       # the built-in's
            label, st = _gate(c)
            assert st[0]["provider"]["fallback"] == "http_401", st
            odd.status = 503
            d = decision(plan(c, PROMPTS[1], sid="s-503"))
            assert d["stages"][0]["provider"]["fallback"] == "http_503", d["stages"]
            # no built-in at all (the controller's `self`): the stage answers as it would have
            c2 = rig.conductor(decider=None)
            ref = rig.conductor(decider=None, providers=False)
            got = decision(plan(c2, PROMPTS[2], sid="s-n"))
            want = decision(plan(ref, PROMPTS[2], sid="s-n"))
            assert got["stages"][0]["error"] == want["stages"][0]["error"] == \
                "decider_no_endpoint", (got, want)
            assert got["class"] == want["class"]
    finally:
        base_srv.shutdown()
        odd.shutdown()
        keyed.shutdown()
    return "unknown label, 401, 503, no built-in"


# ---------------------------------------------------------------------------------- 8
def test_shadow_journals_changes_nothing():
    base_srv, bport = example()
    other, oport = example(answer=last_option, model="other-decider")
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            rig.add(id="watch", endpoint=f"http://127.0.0.1:{oport}", mode="shadow",
                    questions={"classify": {}})
            rig.add(id="same", endpoint=f"http://127.0.0.1:{bport}", mode="shadow",
                    questions={"classify": {}})
            ref = rig.conductor(decider=url, providers=False)
            c = rig.conductor(decider=url)
            for i, t in enumerate(PROMPTS):
                assert decision(plan(c, t, sid=f"s-{i}")) == decision(plan(ref, t, sid=f"s-{i}"))
            assert _wait(lambda: len(rig.ps.ring) == 2 * len(PROMPTS)), len(rig.ps.ring)
            same = [r for r in rig.ps.ring if r["provider"] == "same"]
            watch = [r for r in rig.ps.ring if r["provider"] == "watch"]
            # the same scorer agrees wherever the conductor took the decider's answer
            assert all(r["mode"] == "shadow" and r["used"] == "builtin" for r in same + watch)
            assert sum(1 for r in same if r["agree"]) == sum(
                1 for r in same if r["builtin"]["label"] == r["answer"]["label"])
            assert any(r["agree"] is True for r in same)
            assert any(r["agree"] is False for r in watch)
            assert all(r["answer"]["ms"] > 0 and r["key"] for r in watch)
            assert all(r["session"] and r["request"] for r in watch)
    finally:
        base_srv.shutdown()
        other.shutdown()
    return f"{len(PROMPTS)} decisions unchanged, {2 * len(PROMPTS)} shadow rows"


# ---------------------------------------------------------------------------------- 9
def test_ab_sticky_and_split():
    p = {"id": "jev", "ab_percent": 30, "sticky": "session"}
    arms = [P.Providers.arm(p, {"session": f"s-{i}", "request": f"r-{i}"}) for i in range(2000)]
    share = arms.count("b") / len(arms)
    assert 0.26 <= share <= 0.34, share
    for i in range(50):                           # sticky: the same session, the same arm
        ctx = {"session": f"s-{i}", "request": f"r-{i}-{time.time()}"}
        assert P.Providers.arm(p, ctx) == arms[i]
    assert P.Providers.arm(dict(p, ab_percent=0), {"session": "x"}) == "a"
    assert P.Providers.arm(dict(p, ab_percent=100), {"session": "x"}) == "b"
    assert P.Providers.arm(p, {}) == "a"
    # by user: every session of a user in one arm; a missing user takes the session
    u = dict(p, sticky="user", ab_percent=50)
    for n in range(20):
        got = {P.Providers.arm(u, {"user": f"u-{n}", "session": f"s-{k}"}) for k in range(10)}
        assert len(got) == 1, got
    # another provider id splits the sessions anew
    other = [P.Providers.arm(dict(p, id="laya-2"), {"session": f"s-{i}"}) for i in range(2000)]
    assert other != arms
    base_srv, bport = example()
    prov, pport = example(answer=last_option, model="other-decider")
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            rig.add(id="jev", endpoint=f"http://127.0.0.1:{pport}", mode="ab", ab_percent=50,
                    questions={"classify": {}})
            c = rig.conductor(decider=url)
            seen = collections.Counter()
            for i in range(24):
                sid = f"s-{i}"
                for turn in range(2):
                    pl = plan(c, PROMPTS[i % len(PROMPTS)] + f" ({i}.{turn})", sid=sid,
                              rid=f"r-{i}-{turn}")
                    arm = P.Providers.arm(rig.ps.store.get("jev"), {"session": pl.session.key})
                    s0 = decision(pl)["stages"][0]
                    if arm == "b":
                        assert s0["provider"] == {"id": "jev", "mode": "ab", "arm": "b"}, s0
                        assert s0["model"] == "other-decider"
                    else:
                        assert "provider" not in s0 and s0["model"] == "example-decider", s0
                    seen[arm] += 1
            assert seen["a"] and seen["b"], seen
            # the class cache keeps the arms apart: a text answered in arm b is asked again in a
            ga, gb = [], []
            for i in range(60):
                pl = plan(c, "Rename the helper in utils.py.", sid=f"t-{i}", rid=f"q-{i}")
                arm = P.Providers.arm(rig.ps.store.get("jev"), {"session": pl.session.key})
                (gb if arm == "b" else ga).append(decision(pl)["stages"][0]["model"])
            assert set(ga) == {"example-decider"} and set(gb) == {"other-decider"}, (ga, gb)
            seen["a"] += 1          # the first of each arm asked, the rest cached
            seen["b"] += 1
            arms_rows = collections.Counter(r["arm"] for r in rig.ps.ring)
            assert arms_rows == seen, (arms_rows, seen)
            rep = rig.ps.report("jev")["providers"]["jev"]["classify"]
            assert rep["ab_a"]["calls"] == seen["a"] and rep["ab_b"]["calls"] == seen["b"]
            assert rep["ab_b"]["answered"] == seen["b"] and rep["ab_b"]["p95_ms"] is not None
            assert rep["ab_a"]["p50_ms"] is not None
    finally:
        base_srv.shutdown()
        prov.shutdown()
    return f"share {share:.3f} at 30 %, sticky, both arms journaled"


# ---------------------------------------------------------------------------------- 10
def test_agreement_report():
    base_srv, bport = example()
    other, oport = example(answer=last_option, model="other-decider")
    slow, sport = example(model="slow")
    slow.delay_ms = 600
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig(journal=True) as rig:
            rig.add(id="same", endpoint=url, mode="shadow", questions={"classify": {}})
            rig.add(id="watch", endpoint=f"http://127.0.0.1:{oport}", mode="shadow",
                    questions={"classify": {}, "memory_gate": {}})
            rig.add(id="slow", endpoint=f"http://127.0.0.1:{sport}", mode="on",
                    questions={"judge": {}})
            c = rig.conductor(decider=url, tester=url)
            for i, t in enumerate(PROMPTS):
                plan(c, t, sid=f"s-{i}", rid=f"r-{i}")
            _gate(c)
            _judge(c)
            assert _wait(lambda: len(rig.ps.ring) == 2 * len(PROMPTS) + 2), len(rig.ps.ring)
            c.journal.flush()
            rep = rig.ps.report()
            assert rep["source"] == "journal", rep["source"]
            assert not contract_check.validate(dict(rep, contract_version="1.0"),
                                               contract_check.load(
                                                   "decision-provider-report.schema.json", _CV),
                                               root=_CV)
            s = rep["providers"]["same"]["classify"]["shadow"]
            w = rep["providers"]["watch"]["classify"]["shadow"]
            assert s["calls"] == w["calls"] == len(PROMPTS)
            assert s["agreement_pct"] is not None and s["agreement_pct"] > w["agreement_pct"]
            assert s["p50_ms"] is not None and s["p95_ms"] >= s["p50_ms"]
            assert rep["providers"]["watch"]["memory_gate"]["shadow"]["calls"] == 1
            j = rep["providers"]["slow"]["judge"]["on"]
            assert j["calls"] == 1 and j["failures"] == 1 and j["fallbacks"] == 1, j
            # SRV-851: the fallbacks counted by reason, adding up to the fallbacks
            assert j["fallback_reasons"] == {"timeout": 1}, j
            assert s["fallback_reasons"] == {} and s["fallbacks"] == 0, s
            bucket = contract_check.load("decision-provider-report.schema.json", _CV)
            bucket = dict(bucket["$defs"]["bucket"], **{"$defs": bucket["$defs"]})
            for qs in rep["providers"].values():
                for arms in qs.values():
                    for b in arms.values():
                        assert not contract_check.validate(b, bucket, root=_CV), b
                        assert sum(b["fallback_reasons"].values()) == b["fallbacks"], b
            one = rig.ps.report("watch")
            assert set(one["providers"]) == {"watch"}
            mem = dict(rig.ps.records(0)[0][0])
            assert "Rename" not in json.dumps(mem) and mem["key"]          # never the text
            rows = c.journal.rows(kind="provider", limit=1000)
            assert len(rows) == 2 * len(PROMPTS) + 2
            assert all(r["task"] in ("classify", "memory_gate", "judge") for r in rows)
    finally:
        base_srv.shutdown()
        other.shutdown()
        slow.shutdown()
    return "report from the journal, agreement, p50/p95, failures, fallback reasons"


# ---------------------------------------------------------------------------------- 11
def test_systemone_wire_schema():
    srv, port = example(key=KEYS[0])
    try:
        base = f"http://127.0.0.1:{port}"
        c = classify.JevHttp(base, "auto", 2000, api_key=KEYS[0])
        lim = c.limits()
        assert lim == {"context_tokens": 512, "question_tokens": 192, "max_options": 20}
        h = c.health()
        assert h["up"] and h["detail"]["engine"] == "example"
        a = c.choice("Fix the failing test in the parser.", "What kind of work?",
                     {"debug": "find and fix a failure", "chat": "talk"})
        assert a.error is None and a.label == "debug", a.as_dict()
        call = [x for x in srv.calls if x["path"] == "/v1/systemone"][0]
        assert call["auth"] == f"Bearer {KEYS[0]}"
        assert set(call["body"]) == {"state", "model", "questions"}
        assert call["body"]["questions"]["q"] == {"type": "choice", "instructions":
                                                  "What kind of work?", "criteria": {
                                                      "debug": "find and fix a failure",
                                                      "chat": "talk"}}
        assert all(x["auth"] == f"Bearer {KEYS[0]}" for x in srv.calls)  # probes carry it too
        for text in PROMPTS:
            req = urllib.request.Request(base + "/v1/systemone", data=json.dumps({
                "state": text, "model": "auto", "questions": {
                    "a": {"type": "choice", "instructions": "x", "criteria": {
                        "code_edit": "change code", "chat": "talk", "research": "sources"}},
                    "b": {"type": "choice", "instructions": "y", "criteria": {
                        "yes": "yes", "no": "no"}}}}).encode(), method="POST",
                headers={"Authorization": f"Bearer {KEYS[0]}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ans = json.loads(r.read())
            assert not schema_errors(ans), schema_errors(ans)
        bad = classify.JevHttp(base, "auto", 2000, api_key="wrong")
        assert bad.choice("x", "q", {"a": "a"}).error == "decider_http_401"
        assert not bad.health()["up"]
    finally:
        srv.shutdown()
    return "request shape, bearer on every call, answers per the pinned schema"


# ---------------------------------------------------------------------------------- 12
def test_judge_and_memory_gate():
    base_srv, bport = example()
    prov, pport = example(answer=last_option, model="other-decider")
    url = f"http://127.0.0.1:{bport}"
    try:
        with Rig() as rig:
            rig.add(id="jev", endpoint=f"http://127.0.0.1:{pport}", mode="on",
                    questions={"judge": {}, "memory_gate": {}})
            c = rig.conductor(decider=url, tester=url)
            (label, detail), = _judge(c)
            assert label == "unclear" and detail["provider"] == {"id": "jev", "mode": "on"}
            assert detail["model"] == "other-decider"
            # the writer's gate through MemWriter.gate itself: its context is the session
            w = mw.Writer.__new__(mw.Writer)
            w.digest = lambda conv: GATE_TEXT
            ans, how = w.gate(c.router, "conv-1")
            assert ans == "no" and how == "decider", (ans, how)
            rec = rig.ps.ring[-1]
            assert rec["question"] == "memory_gate" and rec["session"] == "conv-1"
            # the engine's conductor (no providers) asks as before
            e = rig.conductor(decider=url, tester=url, providers=False)
            (label2, detail2), = _judge(e)
            assert "provider" not in detail2 and label2 == "failure", detail2
    finally:
        base_srv.shutdown()
        prov.shutdown()
    return "judge and gate through the provider"


if __name__ == "__main__":
    run(globals())
