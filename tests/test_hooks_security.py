"""Hook payload levels and signing (SEC-16, QA SEC-17), and the hooks' admin rights (SRV-849).

  1  a new hook gets the meta payload: no prompt or answer text     test_default_payload_is_meta
  2  redacted runs the conductor's redaction before sending          test_redacted_runs_redaction
  3  a redaction that fails: no payload, the hook's policy applies   test_redaction_failure
  4  full is granted per hook (who, when); outside the LAN a second
     confirm; a new endpoint asks again; leaving full drops it       test_full_grant_and_confirm
  5  a LAN grant checks the address connected to on every call       test_full_checks_destination
  6  every call carries the HMAC; the docs' Python (and Node, where
     node runs) verify it and refuse a tampered, old or replayed
     call                                                           test_docs_examples_verify
  7  a rotation's grace signs with both secrets, then only the new   test_rotation_grace
  8  a secret is never in a log, an answer after its creation, the
     audit, the export, the journal or the database file's bytes     test_secrets_never_leak
  9  the admin API's rights: the admin's, and pairing's for full and
     secrets (403 trust_required without them, SRV-849)            test_admin_rights

Run: python tests/test_hooks_security.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooks_util import CONTRACT, ROOT, hook_server  # noqa: E402
from proxy_util import Proxy, fake, run  # noqa: E402

from proxy import hookcall  # noqa: E402
from tools import contract_check, hooks_gate  # noqa: E402

KEY = hooks_gate.KEY
PROMPT = "the quiet ORCHID-4471 question"
ANSWER = "the loud TULIP-9902 answer"


def _env(port):
    return hooks_gate.env_for(port)


def _chat(text, **kw):
    return dict({"model": "fake-a", "messages": [{"role": "user", "content": text}]}, **kw)


def _contract(name):
    with open(os.path.join(CONTRACT, name), encoding="utf-8") as f:
        return json.load(f)


def _hook(px, doc, status=201, headers=None):
    st, _, out = px.request("POST", "/v1/fleet/hooks", doc, headers)
    got = json.loads(out)
    assert st == status, (st, got)
    return got


def test_default_payload_is_meta():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            got = _hook(px, {"name": "watch", "endpoint": f"http://127.0.0.1:{hport}/",
                             "role": "observer", "stages": ["request.received",
                                                            "response.after_engine",
                                                            "request.finished"]})
            assert got["hook"]["payload"] == "meta" and got["hook"]["grant"] is None
            st, _, out = px.post("/v1/chat/completions", _chat(f"{PROMPT} FAKE_SAY {ANSWER}"))
            assert st == 200 and ANSWER.encode() in out
            px.rows()
            px.svc.hooks.bg.drain(5)
            assert len(hs.calls) == 3, len(hs.calls)
            for c in hs.calls:
                assert b"ORCHID" not in c["body"] and b"TULIP" not in c["body"], c["body"]
                assert "content" not in c["env"]["event"] and c["env"]["payload"] == "meta"
            ev = next(c["env"]["event"] for c in hs.calls
                      if c["env"]["stage"] == "request.received")
            assert ev["sizes"]["chars"]["user"] == len(f"{PROMPT} FAKE_SAY {ANSWER}")
    finally:
        srv.shutdown()
        hs.shutdown()


def test_redacted_runs_redaction():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            _hook(px, {"name": "r", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.before_engine"],
                       "payload": "redacted"})
            secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX12"
            st, _, _ = px.post("/v1/chat/completions", _chat(f"{PROMPT} uses {secret}"))
            assert st == 200
            body = hs.calls[0]["body"]
            assert secret.encode() not in body and b"ORCHID-4471" in body
            msg = hs.calls[0]["env"]["event"]["content"]["messages"][0]["content"]
            assert "[redacted:api_key]" in msg, msg
            assert secret in srv.fake.last["raw"]                  # the request itself as sent
    finally:
        srv.shutdown()
        hs.shutdown()


def test_redaction_failure():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()

            def broken(v):
                raise ValueError("no")
            px.svc.hooks.redactor = broken
            _hook(px, {"name": "r", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.received"],
                       "payload": "redacted"})
            st, _, _ = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 200 and not hs.calls                     # open: flows, nothing sent
            st, _, out = px.get("/v1/fleet/hooks/r/calls")
            c = json.loads(out)["calls"][0]
            assert c["outcome"] == "redaction_failed" and "no payload" in c["error"]
            px.request("PATCH", "/v1/fleet/hooks/r", {"policy": "closed"})
            st, _, out = px.post("/v1/chat/completions", _chat("hi"))
            assert st == 503 and json.loads(out)["error"]["code"] == "hook_unavailable"
            assert not hs.calls
            st, _, out = px.get("/v1/fleet/hooks/r")
            assert json.loads(out)["hook"]["metrics"]["breaker"]["state"] == "closed"
    finally:
        srv.shutdown()
        hs.shutdown()


def test_full_grant_and_confirm():
    assert hookcall.host_is_lan("127.0.0.1") and hookcall.host_is_lan("10.1.2.3")
    assert hookcall.host_is_lan("192.168.1.20") and hookcall.host_is_lan("100.101.1.2")
    assert hookcall.host_is_lan("hooks.lan") and hookcall.host_is_lan("box.local")
    assert hookcall.host_is_lan("fd00::1") and hookcall.host_is_lan("localhost")
    assert not hookcall.host_is_lan("203.0.113.7") and not hookcall.host_is_lan("8.8.8.8")
    assert not hookcall.host_is_lan("hooks.example.com", resolve=lambda h, p: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    assert hookcall.host_is_lan("in.example.com", resolve=lambda h, p: [
        (0, 0, 0, "", ("10.0.0.5", 0))])
    with Proxy({}) as px:
        got = _hook(px, {"name": "lan", "endpoint": "http://127.0.0.1:9300/",
                         "role": "observer", "stages": ["request.finished"], "payload": "full"})
        g = got["hook"]["grant"]
        assert g["lan"] is True and g["by"] == "local" and g["at"] and g["confirmed_by"] is None
        doc = {"name": "far", "endpoint": "http://203.0.113.7/h", "role": "observer",
               "stages": ["request.finished"], "payload": "full"}
        err = _hook(px, doc, 409)["error"]
        assert err["type"] == "confirm_needed" and err["destination"] == "203.0.113.7"
        assert _hook(px, dict(doc, confirm="0" * 32), 409)["error"]["type"] == "confirm_needed"
        got = _hook(px, dict(doc, confirm=err["confirm"]))
        g = got["hook"]["grant"]
        assert g["lan"] is False and g["confirmed_by"] == "local" and g["confirmed_at"]
        # a new destination outside the LAN asks again
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/far",
                                {"endpoint": "http://198.51.100.9/h"})
        err = json.loads(out)["error"]
        assert st == 409 and err["type"] == "confirm_needed", err
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/far",
                                {"endpoint": "http://198.51.100.9/h", "confirm": err["confirm"]})
        assert st == 200 and json.loads(out)["hook"]["grant"]["destination"] == "198.51.100.9"
        # a confirm made for an older version does not apply
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/far",
                                {"endpoint": "http://203.0.113.8/h", "confirm": err["confirm"]})
        assert st == 409
        # into the LAN: no confirm; out of full: no grant
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/far",
                                {"endpoint": "http://10.1.2.3/h"})
        assert st == 200 and json.loads(out)["hook"]["grant"]["lan"] is True
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/far", {"payload": "meta"})
        assert st == 200 and json.loads(out)["hook"]["grant"] is None
        # a meta hook may point anywhere without a confirm
        st, _, _ = px.request("PATCH", "/v1/fleet/hooks/far", {"endpoint": "http://203.0.113.9/"})
        assert st == 200
        st, _, out = px.get("/v1/config/audit")
        rows = [r for r in json.loads(out)["audit"] if r["object"] == "hook"]
        grants = [r["detail"] for r in rows if r["detail"]]
        assert {"payload": "full", "destination": "203.0.113.7", "lan": False,
                "confirmed": True} in grants, grants


def test_full_checks_destination():
    hs, hport = hook_server()
    try:
        with Proxy({}) as px:
            _hook(px, {"name": "lan", "endpoint": f"http://127.0.0.1:{hport}/",
                       "role": "transform", "stages": ["request.received"],
                       "payload": "full"})
            st, _, out = px.post("/v1/fleet/hooks/lan/test", {})
            assert st == 200 and json.loads(out)["outcome"] == "ok" and len(hs.calls) == 1
            real = hookcall.ip_is_lan
            hookcall.ip_is_lan = lambda a: False                   # the name moved outside
            try:
                st, _, out = px.post("/v1/fleet/hooks/lan/test", {})
            finally:
                hookcall.ip_is_lan = real
            got = json.loads(out)
            assert got["outcome"] == "error" and "outside_lan" in got["error"], got
            assert len(hs.calls) == 1                              # nothing was sent
    finally:
        hs.shutdown()


_EX = re.compile(r"<!-- example: (\S+) -->\n```\w+\n(.*?)```", re.S)


def _examples() -> dict:
    with open(os.path.join(ROOT, "docs", "hooks.md"), encoding="utf-8") as f:
        return dict(_EX.findall(f.read()))


def test_docs_examples_verify():
    ex = _examples()
    assert set(ex) == {"verify.py", "verify.js"}, set(ex)
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            got = _hook(px, {"name": "signed", "endpoint": f"http://127.0.0.1:{hport}/",
                             "role": "transform", "stages": ["request.received"]})
            secret = got["secret"]
            hs.set_secrets([secret])
            for i in range(2):
                assert px.post("/v1/chat/completions", _chat(f"hi {i}"))[0] == 200
            assert [c["verdict"] for c in hs.calls] == ["ok", "ok"]
            calls = hs.calls
    finally:
        srv.shutdown()
        hs.shutdown()
    ns: dict = {}
    exec(compile(ex["verify.py"], "docs/hooks.md:verify.py", "exec"), ns)
    verify = ns["verify"]
    h0, b0 = calls[0]["headers"], calls[0]["body"]
    assert all(k in h0 for k in ("X-Tandem-Hook", "X-Tandem-Delivery", "X-Tandem-Timestamp",
                                 "X-Tandem-Signature"))
    assert h0["X-Tandem-Delivery"] == json.loads(b0)["id"]
    assert verify(secret, h0, b0) is True
    assert verify(secret, h0, b0) is False                         # replayed
    h1, b1 = calls[1]["headers"], calls[1]["body"]
    tampered = b1.replace(b'"mode":"on"', b'"mode":"no"')
    assert tampered != b1 and verify(secret, h1, tampered) is False
    assert verify("hks_wrong", h1, b1) is False
    old = int(time.time()) - 600
    h_old = dict(h1, **{"X-Tandem-Timestamp": str(old),
                        "X-Tandem-Signature": hookcall.sign([secret], old, b1)})
    assert verify(secret, h_old, b1) is False                      # outside the window
    assert verify(secret, h1, b1) is True
    node = shutil.which("node")
    if node is None:
        return "python verified; node not on PATH, the Node example not run"
    d = tempfile.mkdtemp(prefix="hooks-node-")
    try:
        with open(os.path.join(d, "verify.js"), "w") as f:
            f.write(ex["verify.js"])
        cases = [(secret, calls[0], calls[0]["body"]), (secret, calls[0], calls[0]["body"]),
                 (secret, calls[1], tampered),
                 (secret, calls[1], calls[1]["body"])]
        with open(os.path.join(d, "cases.json"), "w") as f:
            json.dump([{"secret": s, "headers": {k.lower(): v for k, v in c["headers"].items()},
                        "body": b.decode()} for s, c, b in cases], f)
        drive = ("const {verify} = require('./verify.js');"
                 "const cs = require('./cases.json');"
                 "console.log(cs.map(c => verify(c.secret, c.headers, Buffer.from(c.body, "
                 "'utf8'))).join(' '));")
        out = subprocess.run([node, "-e", drive], cwd=d, capture_output=True, text=True,
                             timeout=60)
        assert out.stdout.strip() == "true false false true", (out.stdout, out.stderr)
        return "python and node verified"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rotation_grace():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            old = _hook(px, {"name": "rot", "endpoint": f"http://127.0.0.1:{hport}/",
                             "role": "observer", "stages": ["request.received"],
                             "timeout_ms": 140})["secret"]
            hs.set_secrets([old])                                  # a receiver not updated yet
            st, _, out = px.post("/v1/fleet/hooks/rot/secret", {"grace_s": 60})
            new = json.loads(out)["secret"]
            assert st == 200 and new != old
            px.post("/v1/chat/completions", _chat("hi"))
            px.svc.hooks.bg.drain(5)
            c = hs.calls[-1]
            assert c["verdict"] == "ok" and c["headers"]["X-Tandem-Signature"].count("v1=") == 2
            st, _, out = px.post("/v1/fleet/hooks/rot/secret", {})
            newer = json.loads(out)["secret"]
            px.post("/v1/chat/completions", _chat("hi again"))
            px.svc.hooks.bg.drain(5)
            c = hs.calls[-1]
            assert c["verdict"] == "bad_signature", c["verdict"]   # grace 0: the old one goes
            assert c["headers"]["X-Tandem-Signature"].count("v1=") == 1
            hs.set_secrets([newer])
            px.post("/v1/chat/completions", _chat("third"))
            px.svc.hooks.bg.drain(5)
            assert hs.calls[-1]["verdict"] == "ok"
    finally:
        srv.shutdown()
        hs.shutdown()


def test_secrets_never_leak():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    hs, hport = hook_server()
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            first = _hook(px, {"name": "s", "endpoint": f"http://127.0.0.1:{hport}/",
                               "role": "transform", "stages": ["request.received",
                                                               "request.before_engine"]})
            s1 = first["secret"]
            assert first["hook"]["secret"]["last4"] == s1[-4:]
            st, _, out = px.post("/v1/fleet/hooks/s/secret", {"grace_s": 30})
            s2 = json.loads(out)["secret"]
            for i in range(3):
                px.post("/v1/chat/completions", _chat(f"hi {i}"))
            px.post("/v1/fleet/hooks/s/test", {"stage": "request.before_engine"})
            px.rows()
            px.svc.hooks.bg.drain(5)
            answers = []
            for path in ("/v1/fleet/hooks", "/v1/fleet/hooks/s", "/v1/fleet/hooks/s/calls",
                         "/v1/config/audit", "/v1/config", "/v1/fleet/app/export",
                         "/metrics"):
                st, _, out = px.get(path)
                assert st == 200, (path, st)
                answers.append(out)
            st, _, out = px.request("PATCH", "/v1/fleet/hooks/s", {"priority": 7})
            answers.append(out)
            blobs = answers + [m.encode() for m in px.logs] + [c["body"] for c in hs.calls]
            for f in px.files():
                with open(f, "rb") as fh:
                    blobs.append(fh.read())
            for s in (s1, s2):
                for b in blobs:
                    assert s.encode() not in b, s[:8]
                # nor any long piece of it
                assert not any(s[4:24].encode() in b for b in blobs)
            assert all(c["headers"].get("X-Tandem-Signature") for c in hs.calls)
    finally:
        srv.shutdown()
        hs.shutdown()


def test_admin_rights():
    away = {"X-Forwarded-For": "203.0.113.50"}                    # not this machine
    meta = {"name": "m", "endpoint": "http://127.0.0.1:9300/", "role": "observer",
            "stages": ["request.finished"]}
    with Proxy({}) as px:                                          # no admin password
        _hook(px, meta, headers=away)
        # SRV-849: the writes are there, pairing's rights are not: 403 trust_required, as the
        # settings interface answers the same grant
        err = _hook(px, dict(meta, name="f", payload="full"), 403, headers=away)
        assert err["error"]["type"] == "trust_required" and "pairing" in err["error"]["message"]
        assert not contract_check.validate(err, _contract("error.schema.json"), root=CONTRACT)
        st, _, out = px.request("POST", "/v1/fleet/hooks/m/secret", {}, away)
        assert st == 403 and json.loads(out)["error"]["type"] == "trust_required"
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/m", {"payload": "full"}, away)
        assert st == 403 and json.loads(out)["error"]["type"] == "trust_required"
        _hook(px, dict(meta, name="f", payload="full"))           # from this machine
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/f", {"endpoint": "http://10.0.0.9/"},
                                away)
        assert st == 403 and json.loads(out)["error"]["type"] == "trust_required"
        st, _, out = px.request("PATCH", "/v1/fleet/hooks/f", {"priority": 3}, away)
        assert st == 200                                           # a switch: the writes only
        # the settings interface refuses the same grant the same way
        st, _, out = px.request("POST", "/v1/settings/hooks/plan",
                                {"set": {"hooks.m.payload": "full"}}, away)
        assert st == 403 and json.loads(out)["error"]["type"] == "trust_required", out
        st, _, _ = px.request("POST", "/v1/fleet/hooks", meta,
                              {"Origin": "http://evil.example", "Host": "127.0.0.1"})
        assert st == 403                                           # another origin
    token = "adm-" + "x" * 40
    with Proxy({"TANDEM_ADMIN_TOKEN": token}) as px:
        for method, path, body in (("GET", "/v1/fleet/hooks", None),
                                   ("POST", "/v1/fleet/hooks", meta),
                                   ("GET", "/v1/fleet/hooks/stages", None)):
            st, _, _ = px.request(method, path, body, away)
            assert st == 401, (method, path, st)
        st, _, out = px.request("POST", "/v1/fleet/hooks", dict(meta, payload="full"), away)
        assert st == 401 and json.loads(out)["error"]["type"] == "unauthorized"  # no writes
        bearer = dict(away, Authorization=f"Bearer {token}")
        _hook(px, meta, headers=bearer)
        _hook(px, dict(meta, name="f2", payload="full"), headers=bearer)
        st, _, _ = px.request("GET", "/v1/fleet/hooks/m/calls", None, bearer)
        assert st == 200


if __name__ == "__main__":
    run(globals())
