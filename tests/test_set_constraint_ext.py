"""SET_CONSTRAINT, TandemApp's side of engine-ext-v1 3.2 (ENG-458's pin).

  1  the act's shape: `policy.Action(SET_CONSTRAINT, constraint=...)` fills the defaults and refuses
     what the reference host (contracts/engine-ext-v1/fake_host.py `constraint_of`) takes as a wrong
     shape, case for case; SP4 allows it, no other safe point does; no other act takes a constraint
                                                                test_the_shape_agrees_with_the_host
  2  a task at SP4 that returns the act: the supervisor hands it out at SP4, journals it with its
     constraint, takes the engine's outcomes and puts them in the request's outcome row
                                                                test_the_supervisor_journals_it
  3  the reference host carries the act out and its outcomes reach the supervisor through the
     extension's Watch (`constraint_outcomes`); the extension speaks 3.2, as the pin says
                                                                test_the_host_carries_it_out

    python tests/test_set_constraint_ext.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proxy_util import run  # noqa: E402
from tandemapp import engine_ext  # noqa: E402
from tandemapp.conductor.monitor import policy as P  # noqa: E402
from tandemapp.conductor.monitor import tier0  # noqa: E402
from tandemapp.conductor.monitor.executor import Monitor  # noqa: E402
from tandemapp.conductor.monitor.observe import RequestEvent  # noqa: E402

DIR = os.path.join(ROOT, "contracts", "engine-ext-v1")
RULE = "test_set_constraint"


def _host_module():
    spec = importlib.util.spec_from_file_location("engine_ext_ref_host32",
                                                  os.path.join(DIR, "fake_host.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _host_module()


class Tok:
    def __call__(self, text, add_special_tokens=False):
        return types.SimpleNamespace(input_ids=[ord(c) for c in text])


def _act_dict(a: P.Action) -> dict:
    return {"kind": a.kind, "task": a.task, "text": a.text, "ids": a.ids, "visible": a.visible,
            "budget": a.budget, "detail": a.detail, "constraint": a.constraint}


# ---------------------------------------------------------------------------- 1
def test_the_shape_agrees_with_the_host():
    good = [{"kind": "choice", "value": ["yes", "no"]},
            {"kind": "regex", "value": "[0-9]+", "strict": True},
            {"kind": "json_schema", "value": {"type": "object"}, "scope": "answer"},
            {"kind": "choice", "value": ["f"], "scope": "tool_args"},
            {"kind": "logit_bias", "value": {"5": -100}}]
    bad = [None, {"value": "x"}, {"kind": "grammar", "value": "x"},
           {"kind": "regex", "value": "x", "scope": "everything"},
           {"kind": "regex", "value": "x", "strict": "yes"}]
    for c in good:
        a = P.Action(P.SET_CONSTRAINT, constraint=c)
        assert a.constraint["scope"] in P.CONSTRAINT_SCOPES and \
            isinstance(a.constraint["strict"], bool), a.constraint
        assert H.constraint_of(_act_dict(a))["kind"] == c["kind"]
    for c in bad:
        try:
            P.Action(P.SET_CONSTRAINT, constraint=c)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {c!r}")
        try:
            H.constraint_of({"kind": "SET_CONSTRAINT", "constraint": c})
        except ValueError:
            pass
        else:
            raise AssertionError(f"the host takes {c!r}, TandemApp refuses it")
    assert P.SET_CONSTRAINT in P.SAFE_POINTS["SP4"][1]
    assert all(P.SET_CONSTRAINT not in acts for sp, (_, acts) in P.SAFE_POINTS.items()
               if sp != "SP4")
    assert P.SET_CONSTRAINT in H.ALLOWED["SP4"], H.ALLOWED
    try:
        P.Action(P.PROMPT_TAIL_NOTE, constraint={"kind": "regex", "value": "x"})
    except ValueError:
        pass
    else:
        raise AssertionError("another act took a constraint")
    return f"{len(good)} shapes taken, {len(bad)} refused, by both"


# ---------------------------------------------------------------------------- 2
def _with_rule(fn):
    """A policy whose one SP4 task acts with SET_CONSTRAINT, while `fn` runs."""
    def rule(ev, task, sup):
        return P.Action(P.SET_CONSTRAINT, reason="test",
                        constraint={"kind": "choice", "value": ["yes", "no"]})
    tier0.RULES[RULE] = tier0.Rule(RULE, "SP4", rule, {}, (), "a test's")
    try:
        pol = P.load_default()
        for t in pol.tasks.values():
            t.mode = "off"
        pol.tasks["T_SC"] = P.Task("T_SC", {"rule": RULE, "mode": "act"})
        return fn(pol)
    finally:
        tier0.RULES.pop(RULE, None)


def test_the_supervisor_journals_it():
    def check(pol):
        mon = Monitor("on", pol, Tok())
        req = RequestEvent(request_id="r1", messages=[{"role": "user", "content": "hi"}])
        sup = mon.begin("on", req, [1, 2, 3], "c1")
        assert sup is not None
        (act,) = sup.at_request(req)
        assert act.kind == P.SET_CONSTRAINT and act.task == "T_SC", act
        assert act.constraint == {"kind": "choice", "value": ["yes", "no"], "scope": "answer",
                                  "strict": False}
        row = sup.events[-1]
        assert row["decision"] == P.SET_CONSTRAINT and row["sp"] == "SP4" and \
            row["detail"]["constraint"]["kind"] == "choice", row
        out = [{"kind": "choice", "scope": "answer", "strict": False, "applied": True,
                "why": None}]
        sup.constraint_outcomes(out)
        sup.finish("text")
        end = [e for e in mon.journal.events("r1") if e.get("decision") == "OUTCOME"][-1]
        assert end["detail"]["constraints"] == out, end
        # in shadow it is journaled and not handed out
        sup2 = mon.begin("shadow", req, [1, 2, 3], "c2")
        assert sup2.at_request(req) == [], "shadow handed the act out"
        return "handed out at SP4, journaled, its outcome in the outcome row"
    return _with_rule(check)


# ---------------------------------------------------------------------------- 3
def test_the_host_carries_it_out():
    def check(pol):
        mon = Monitor("on", pol, Tok())
        req = RequestEvent(request_id="r1", messages=[{"role": "user", "content": "hi"}])
        sup = mon.begin("on", req, [1, 2, 3], "c1")
        watch = engine_ext.Watch(sup, req, {})
        acts = [_act_dict(a) for a in watch.at_request(req)]
        host = H.FakeHost()
        rec = {"trace": [], "errors": [], "acts": [], "raw": False, "detached": {}, "status": 200}
        body = {"model": "TandemLLM", "messages": req.messages}
        assert host.constrain(rec, watch, acts, body) is None, rec
        assert rec["constraints"] == [{"kind": "choice", "scope": "answer", "strict": False,
                                       "applied": True, "why": None}], rec
        assert sup.constraints == rec["constraints"] and not rec["errors"], rec
        # the request's own constraint wins: refused, uncounted, and the supervisor hears it
        rec2 = dict(rec, errors=[])
        own = dict(body, structured_outputs={"choice": ["a"]})
        assert host.constrain(rec2, watch, acts, own) is None
        assert rec2["constraints"][0]["applied"] is False and not rec2["errors"], rec2
        assert sup.constraints[0]["applied"] is False
        assert engine_ext.EXT_API == "3.2"
        with open(os.path.join(ROOT, "contracts", "engine-api-v1", "PIN")) as f:
            pin = dict(line.split(" ", 1) for line in f.read().splitlines()
                       if line and not line.startswith("#"))
        assert pin["ext_api"].strip() == engine_ext.EXT_API, pin
        return "applied, refused for the request's own, both outcomes at the supervisor"
    return _with_rule(check)


if __name__ == "__main__":
    run(globals())
