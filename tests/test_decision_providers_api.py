"""Decision providers through the controller (SRV-851, QA SRV-852): the admin API, the relay with
the conductor's harness, keys, the byte gate.

  1  the admin API: list, create, change, mode, enable, test, health,
     calls, report, delete; its answers, refusals included, validate
     against contracts/conductor-v1; the list carries each
     provider's health from its last probes, probing nothing; the
     rights are the configuration's                                 test_api_routes_and_rights
  2  a provider on for classify: a relayed chat's class is its answer
     (with its key, at its endpoint); in shadow the class is the
     conductor's and the report counts the shadow calls; down: the
     built-in answer, the health view failing                      test_relay_with_a_provider
  3  no provider key in any answer, log line, file of the controller,
     audit row, journal row or the controller move's export        test_key_never_leaves
  4  no provider: the relay byte for byte (tools/hooks_gate.py's cases)
     with providers stored for every question; with the conductor on
     the decisions recorded are those of a controller without any  test_relay_unchanged_without_provider

Run: python tests/test_decision_providers_api.py
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provider_util import KEYS, PROMPTS, example, last_option  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

from proxy import app as proxy_app  # noqa: E402
from proxy import ctlmove  # noqa: E402
from tools import contract_check, hooks_gate  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CV = os.path.join(ROOT, "contracts", "conductor-v1")
BASE = "/v1/fleet/decision-providers"
UA = {"User-Agent": "opencode/1.18.34 ai-sdk/provider-utils/3"}
SYSTEM = {"role": "system", "content": "You are a coding agent. Working directory: /tmp/proj"}
AWAY = {"X-Forwarded-For": "203.0.113.50"}


def _ok(doc, name):
    errs = contract_check.validate(doc, contract_check.load(f"{name}.schema.json", CV), root=CV)
    assert not errs, (name, errs[:5])


def _j(px, method, path, body=None, want=200, headers=None):
    st, _, out = px.request(method, path, body, headers)
    doc = json.loads(out or b"{}")
    assert st == want, (method, path, st, doc)
    if want >= 400 and path.startswith(BASE):
        _ok(doc, "error")                     # SRV-851: the refusals hold to the contract
        assert doc["error"]["code"] == doc["error"]["type"], doc
    return doc


def _env(port, bid="v"):
    up = bid.upper()
    return {"TANDEM_BACKENDS": bid, f"TANDEM_BACKEND_{up}_KIND": "vllm",
            f"TANDEM_BACKEND_{up}_URL": f"http://127.0.0.1:{port}",
            f"TANDEM_BACKEND_{up}_HARNESS": "on"}


def _decider_stage_on(px, decider: str = "self"):
    """The controller's router asks its decider stage for the class (the shipped router leaves it
    off where the served model classifies), the decider role at `decider`."""
    cond = proxy_app.STATE["conductor"]
    v, router = cond.store.get("router")
    router = json.loads(json.dumps(router))
    for st in router["classifier"]["stages"]:
        st["enabled"] = True
    cond.store.put("router", None, router, note="test", base=v, author="test")
    v, roles = cond.store.get("roles")
    roles = json.loads(json.dumps(roles))
    roles["decider"] = dict(roles["decider"], mode="on", endpoint=decider)
    cond.store.put("roles", None, roles, note="test", base=v, author="test")
    return cond


def _chat(px, text, sid):
    st, _, out = px.post("/v1/chat/completions", {
        "model": "m1", "messages": [SYSTEM, {"role": "user", "content": text}],
        "stream": False}, headers=dict(UA, **{"x-session-id": sid}))
    assert st == 200, out
    return out


def _routes(cond, n=50):
    cond.journal.flush()
    return [r for r in cond.journal.rows(limit=n) if r.get("kind") == "route"]


# ---------------------------------------------------------------------------------- 1
def test_api_routes_and_rights():
    ex, eport = example(key=KEYS[0])
    token = "adm-" + "y" * 40
    try:
        with Proxy({"TANDEM_ADMIN_TOKEN": token}) as px:
            for method, path, body in (("GET", BASE, None), ("POST", BASE, {"name": "x"}),
                                       ("GET", BASE + "/report", None)):
                st, _, _ = px.request(method, path, body, AWAY)
                assert st == 401, (method, path, st)
            st, _, _ = px.request("POST", BASE, {"name": "x"},
                                  {"Origin": "http://evil.example", "Host": "127.0.0.1"})
            assert st == 403                                       # another origin
            h = dict(AWAY, Authorization=f"Bearer {token}")
            doc = _j(px, "GET", BASE, headers=h)
            _ok(doc, "decision-providers")
            assert doc["providers"] == [] and "classify" in doc["questions"]
            ep = f"http://127.0.0.1:{eport}"
            made = _j(px, "POST", BASE, {"name": "Jev", "endpoint": ep, "api_key": KEYS[0],
                                         "mode": "shadow", "questions": {
                                             "classify": {"model": "example-decider"},
                                             "judge": {"timeout_ms": 100}}}, 201, h)
            _ok(made, "decision-provider")
            p = made["provider"]
            assert p["id"] == "jev" and p["key"]["last4"] == KEYS[0][-4:]
            assert KEYS[0] not in json.dumps(made)
            # the list carries each provider's health from its last probes: none yet, so
            # unknown, and the endpoint saw no probe for it
            seen = len(ex.calls) if hasattr(ex, "calls") else None
            doc = _j(px, "GET", BASE, headers=h)
            _ok(doc, "decision-providers")
            hl = doc["providers"][0]["health"]
            assert hl["status"] == "unknown" and all(e["probe"] is None
                                                     for e in hl["endpoints"]), hl
            if seen is not None:
                assert len(ex.calls) == seen
            _ok(_j(px, "GET", BASE + "/jev?health=1", headers=h), "decision-provider")
            hl = _j(px, "GET", BASE, headers=h)["providers"][0]["health"]
            assert hl["status"] == "ok" and hl["endpoints"][0]["probe"]["up"] and \
                hl["endpoints"][0]["probe"]["age_s"] >= 0, hl
            got = _j(px, "PATCH", BASE + "/jev", {"name": "Jev 2"}, headers=h)
            assert got["provider"]["name"] == "Jev 2" and got["provider"]["version"] == 2
            got = _j(px, "PUT", BASE + "/jev/mode", {"mode": "ab", "ab_percent": 20,
                                                     "sticky": "user"}, headers=h)
            assert (got["provider"]["mode"], got["provider"]["ab_percent"],
                    got["provider"]["sticky"]) == ("ab", 20, "user")
            _j(px, "PUT", BASE + "/jev/mode", {"mode": "sometimes"}, 422, h)
            _j(px, "PUT", BASE + "/jev/mode", {"mode": "on", "name": "x"}, 422, h)
            got = _j(px, "POST", BASE + "/jev/disable", {}, headers=h)
            assert got["provider"]["enabled"] is False
            _j(px, "POST", BASE + "/jev/enable", {}, headers=h)
            t = _j(px, "POST", BASE + "/jev/test", {}, headers=h)
            _ok(t, "decision-provider-test")
            assert t["ok"] and t["question"] == "classify" and t["answer"]["label"], t
            t = _j(px, "POST", BASE + "/jev/test", {"question": "judge",
                                                    "text": "Traceback: boom"}, headers=h)
            assert t["ok"] and t["answer"]["label"] in ("failure", "clean", "unclear"), t
            _j(px, "POST", BASE + "/jev/test", {"question": "packages"}, 422, h)
            hl = _j(px, "GET", BASE + "/jev/health?force=1", headers=h)
            _ok(hl, "decision-provider-health")
            assert hl["status"] == "ok" and hl["endpoints"][0]["probe"]["up"], hl
            _ok(_j(px, "GET", BASE + "/jev/calls?limit=5", headers=h), "decision-provider-calls")
            rep = _j(px, "GET", BASE + "/report?hours=1", headers=h)
            _ok(rep, "decision-provider-report")
            assert rep["source"] == "memory"                   # no conductor on this controller
            _j(px, "GET", BASE + "/report?hours=0", None, 400, h)
            _j(px, "GET", BASE + "/report?provider=nope", None, 404, h)
            _j(px, "POST", BASE, {"name": "y", "endpoint": "ftp://x",
                                  "questions": {"classify": {}}}, 422, h)
            _j(px, "POST", BASE, {"name": "y", "endpoint": ep, "mode": "on",
                                  "questions": {"classify": {}}}, 409, h)       # ab holds it
            _j(px, "PATCH", BASE + "/jev", {"kind": "hook"}, 422, h)
            _j(px, "GET", BASE + "/nope", None, 404, h)
            _j(px, "DELETE", BASE + "/jev", None, 200, h)
            assert _j(px, "GET", BASE, headers=h)["providers"] == []
    finally:
        ex.shutdown()
    return "every route, its schema, 401/403/404/409/422"


# ---------------------------------------------------------------------------------- 2
def test_relay_with_a_provider():
    srv, port = fake(models=["m1"])
    ex, eport = example(key=KEYS[0], model="jev-decider", answer=last_option)
    try:
        with Proxy(_env(port), conductor=True) as px:
            px.sync()
            cond = _decider_stage_on(px)
            _j(px, "POST", BASE, {"name": "Jev", "endpoint": f"http://127.0.0.1:{eport}",
                                  "api_key": KEYS[0], "mode": "on",
                                  "questions": {"classify": {}}}, 201)
            _chat(px, PROMPTS[0], "s-on")
            row = _routes(cond)[0]
            s0 = row["stages"][0]
            assert s0["provider"] == {"id": "jev", "mode": "on"} and s0["model"] == \
                "jev-decider", row["stages"]
            assert row["class_id"] == s0["label"] and row["source"] == "decider", row
            asked = [c for c in ex.calls if c["path"] == "/v1/systemone"]
            assert len(asked) == 1 and asked[0]["auth"] == f"Bearer {KEYS[0]}"
            # no class question went to the backend: the provider decided (other questions,
            # the packages', may still go there)
            classes = sorted(c["id"] for c in cond.store.peek("router")[1]["classes"])
            assert not [b for b in srv.fake.bodies if sorted(
                (b.get("structured_outputs") or {}).get("choice") or []) == classes]
            # shadow: the class is the conductor's own; the provider is asked beside
            _j(px, "PUT", BASE + "/jev/mode", {"mode": "shadow"})
            _chat(px, PROMPTS[1], "s-shadow")
            row = _routes(cond)[0]
            assert all("provider" not in s for s in row["stages"]), row["stages"]
            px.svc.providers.bg.drain(10)
            cond.journal.flush()
            rep = _j(px, "GET", BASE + "/report?provider=jev")
            assert rep["source"] == "journal"
            b = rep["providers"]["jev"]["classify"]
            assert b["on"]["calls"] == 1 and b["on"]["answered"] == 1 and \
                b["shadow"]["calls"] == 1, b
            # down: the conductor's own stages answer, the health view says so
            _j(px, "PUT", BASE + "/jev/mode", {"mode": "on"})
            ex.shutdown()
            _chat(px, PROMPTS[2], "s-down")
            row = _routes(cond)[0]
            s0 = row["stages"][0]
            assert s0["provider"]["fallback"] in ("unreachable", "timeout"), s0
            assert s0["error"] == "decider_no_endpoint"           # the controller's `self`
            assert row["class_id"] and row["stages"][1]["stage"] == "qwen", row["stages"]
            hl = _j(px, "GET", BASE + "/jev/health?force=1")
            assert hl["status"] == "down", hl
    finally:
        srv.shutdown()
    return "on, shadow, down through the harness"


# ---------------------------------------------------------------------------------- 3
def test_key_never_leaves():
    srv, port = fake(models=["m1"])
    ex, eport = example(key=KEYS[0])
    qx, qport = example(key=KEYS[1])
    try:
        with Proxy(_env(port), conductor=True) as px:
            px.sync()
            cond = _decider_stage_on(px)
            answers = []

            def j(method, path, body=None, want=200):
                st, _, out = px.request(method, path, body)
                answers.append(out)
                assert st == want, (path, st, out)
                return json.loads(out)
            j("POST", BASE, {"name": "Jev", "endpoint": f"http://127.0.0.1:{eport}",
                             "api_key": KEYS[0], "mode": "on",
                             "questions": {"classify": {"endpoint": f"http://127.0.0.1:{qport}",
                                                        "api_key": KEYS[1]},
                                           "memory_gate": {}}}, 201)
            for path in (BASE, BASE + "/jev", BASE + "/jev?health=1", BASE + "/jev/health",
                         BASE + "/report", BASE + "/jev/calls"):
                j("GET", path)
            j("POST", BASE + "/jev/test", {})
            j("POST", BASE + "/jev/test", {"question": "memory_gate"})
            for i, t in enumerate(PROMPTS[:3]):
                answers.append(_chat(px, t, f"s-{i}"))
            # a bad key: the call fails, the error words carry no key
            j("PATCH", BASE + "/jev", {"api_key": KEYS[2]})
            answers.append(_chat(px, PROMPTS[3], "s-bad"))
            j("GET", BASE + "/jev/calls")
            cond.journal.flush()
            exp = ctlmove.export(px.svc.cfg)
            db = base64.b64decode(exp["db"])
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", dir=px.dir, delete=False) as f:
                f.write(db)
            con = sqlite3.connect(f.name)
            assert con.execute("SELECT COUNT(*) FROM decision_provider_keys").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM decision_providers").fetchone()[0] == 1
            con.close()
            blobs = answers + [m.encode() for m in px.logs] + \
                [json.dumps(exp).encode(), db]
            blobs.append(json.dumps(_routes(cond, 200), default=str).encode())
            blobs.append(json.dumps(cond.journal.rows(kind="provider", limit=200),
                                    default=str).encode())
            for fn in px.files():
                with open(fn, "rb") as fh:
                    blobs.append(fh.read())
            for k in KEYS:
                for b in blobs:
                    assert k.encode() not in b, k
                    assert k[6:20].encode() not in b, k
            # the providers did get their keys
            assert [c["auth"] for c in qx.calls if c["path"] == "/v1/systemone"][0] == \
                f"Bearer {KEYS[1]}"
            assert any(c["auth"] == f"Bearer {KEYS[0]}" for c in ex.calls)
    finally:
        srv.shutdown()
        ex.shutdown()
        qx.shutdown()
    return "answers, logs, files, journal, export"


# ---------------------------------------------------------------------------------- 4
def test_relay_unchanged_without_provider():
    srv, port = fake(models=["fake-a"], key=hooks_gate.KEY, cache=False)
    ex, eport = example()
    try:
        with Proxy(hooks_gate.env_for(port)) as px:
            px.sync()
            plain = hooks_gate.run(px, srv)
            ep = f"http://127.0.0.1:{eport}"
            px.svc.providers.store.create({"name": "all", "endpoint": ep, "mode": "on",
                                           "questions": {q: {} for q in (
                                               "classify", "packages", "deliverable",
                                               "memory_intent", "memory_gate", "judge")}}, "t")
            px.svc.providers.store.create({"name": "watch", "endpoint": ep, "mode": "shadow",
                                           "questions": {"classify": {}}}, "t")
            again = hooks_gate.run(px, srv)
        for (n1, d1, doc1), (n2, d2, doc2) in zip(plain, again):
            assert n1 == n2 and d1 == d2, (n1, doc1, doc2)
        assert not [c for c in ex.calls if c["path"] == "/v1/systemone"]
    finally:
        srv.shutdown()
    # with the conductor on: a controller with no provider, with disabled ones and ones for
    # other questions decides as a controller before providers
    fk, fport = fake(models=["m1"])
    try:
        got = []
        for setup in ("none", "idle"):
            with Proxy(_env(fport), conductor=True) as px:
                px.sync()
                cond = _decider_stage_on(px, f"http://127.0.0.1:{eport}")
                if setup == "idle":
                    st = px.svc.providers.store
                    st.create({"name": "off", "endpoint": f"http://127.0.0.1:{eport}",
                               "mode": "on", "enabled": False,
                               "questions": {"classify": {}}}, "t")
                    st.create({"name": "other", "endpoint": f"http://127.0.0.1:{eport}",
                               "mode": "on", "questions": {"deliverable": {}}}, "t")
                else:
                    cond.router.providers = None          # as before this change
                    cond.judge.providers = None
                for i, t in enumerate(PROMPTS):
                    _chat(px, t, f"s-{i}")
                rows = sorted(_routes(cond, 200), key=lambda r: r["session_key"] or "")
                got.append([(r["class_id"], r["source"], r["effort"],
                             [{k: v for k, v in s.items() if k not in ("ms", "eta_ms")}
                              for s in r["stages"]]) for r in rows])
        assert len(got[0]) == len(PROMPTS) and got[0] == got[1], got
    finally:
        fk.shutdown()
        ex.shutdown()
    return f"{len(hooks_gate.CASES)} relay cases byte for byte; {len(PROMPTS)} decisions equal"


if __name__ == "__main__":
    run(globals())
