"""SRV-881: a Messages API client on a Messages API backend passes through; /v1/models in both
shapes; the thinking stash; hooks-v1 1.1's `protocol` (proxy/passthrough.py, proxy/stash.py,
proxy/messages.py, proxy/relay.py). Fake servers only (tests/fake_anthropic.py,
tests/fake_backend.py), no network.

  Gherkin "Bytes pass"                      test_bytes_pass
  Gherkin "A renamed model keeps its prefix" test_a_renamed_model_keeps_its_prefix
  Gherkin "Models in two shapes"            test_models_in_two_shapes
  Gherkin "A hook that wants to rewrite"    test_a_hook_that_wants_to_rewrite
  Gherkin "The stash"                       test_the_stash_keeps_thinking_in_a_tool_loop,
                                            test_the_stash_an_evicted_turn_marks_the_row
  AC no translation code without hooks      test_no_hook_runs_no_translation_code
  AC the stash's bounds, per token          test_the_stash_is_bounded_and_per_token,
                                            test_the_stash_is_per_token_over_http
  AC hooks-v1 with and without protocol     tests/test_hooks_contract.py test_examples_validate
                                            (compat/1.0), test_hooks_name_the_protocols here
  also: errors of our own in the Messages shape, upstream errors as sent, count_tokens passes,
  a mixed route is translated, an early close ends with an error event, the 1 h cache write

Run: python tests/test_proxy_passthrough.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_util import FIXTURE_KEYS, Proxy as _Proxy, run  # noqa: E402


class Proxy(_Proxy):
    """SEC-28: the keys of TANDEM_API_KEYS come in as API tokens of the OpenAI API (the import);
    these tests use them on the Messages API too, as if an admin had given them that scope."""

    def __enter__(self):
        px = super().__enter__()
        cfg = self.svc.cfg
        with cfg._lock:
            cfg._conn.execute("UPDATE api_tokens SET scopes = ? WHERE source = 'env'",
                              (json.dumps(["inference:openai", "inference:anthropic"]),))
            cfg._conn.commit()
        return px

import fake_anthropic  # noqa: E402
import fake_backend  # noqa: E402
from hooks_util import Clock, hook_server  # noqa: E402
from proxy import passthrough  # noqa: E402
from proxy import stash as stash_mod  # noqa: E402
from proxy.adapters.base import Adapter  # noqa: E402
from proxy.translate import anthropic as tr  # noqa: E402

KEY = "user-key-0123456789abcdef"
KEY2 = "user-key-fedcba9876543210"
BACKEND_KEY = FIXTURE_KEYS[0]
OUTSIDE = {"X-Real-IP": "192.0.2.7", "X-Forwarded-For": "192.0.2.7"}   # not this machine
TOOL = {"name": "get_weather", "description": "Weather of a city",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
FN = {"type": "function", "function": {"name": "get_weather", "description": "Weather of a city",
                                       "parameters": TOOL["input_schema"]}}


def _env(port, extra=None):
    env = {"TANDEM_BACKENDS": "anth", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
           "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}",
           "TANDEM_BACKEND_ANTH_KEY": BACKEND_KEY,
           "TANDEM_API_KEYS": f"alice:{KEY},bob:{KEY2}"}
    env.update(extra or {})
    return env


def _hdr(key=KEY, **kw):
    h = {"x-api-key": key, "anthropic-version": "2026-10-21",
         "anthropic-beta": "some-beta-2026-01-01"}
    h.update(kw)
    return {k: v for k, v in h.items() if v is not None}


# a request with what a translation would lose: thinking blocks and their signatures in the
# history, cache_control on a system block and a message, a top-level breakpoint, a field this
# server does not know, odd spacing, an escaped character, non-ASCII text and a float
RAW = ('{"model":  "model-a" ,"max_tokens":300, "temperature" :0.50,'
       '"system":[{"type":"text","text":"Be brief \\u00e9","cache_control":{"type":"ephemeral",'
       '"ttl":"1h"}}],"messages":[{"role":"user","content":"Weather in Zürich?"},'
       '{"role":"assistant","content":[{"type":"thinking","thinking":"Look it up.",'
       '"signature":"sig-client-1"},{"type":"redacted_thinking","data":"opaque=="},'
       '{"type":"tool_use","id":"toolu_1","name":"get_weather","input":{"city":"Zürich"}}]},'
       '{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1",'
       '"content":"12 C","cache_control":{"type":"ephemeral"}}]}],'
       '"tools":[{"name":"get_weather","input_schema":{"type":"object"}},'
       '{"type":"web_search_20260209","name":"web_search"}],'
       '"cache_control":{"type":"ephemeral"},"future_field":{"x":1}}').encode()
ANSWER = {"content": [{"type": "thinking", "thinking": "It is mild.", "signature": "sig-up-1"},
                      {"type": "redacted_thinking", "data": "abc=="},
                      {"type": "text", "text": "Mild, 12 C.", "citations": None}],
          "stop_reason": "end_turn", "container": {"id": "c1"},
          "usage": {"input_tokens": 100, "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 500, "output_tokens": 7,
                    "cache_creation": {"ephemeral_5m_input_tokens": 200,
                                       "ephemeral_1h_input_tokens": 300}}}


def _stream_raw(stream: bool) -> bytes:
    return RAW.replace(b'"max_tokens":300', b'"max_tokens":300,"stream":true') if stream \
        else RAW


# ------------------------------------------------------------------------- pass-through
def test_bytes_pass():
    """Gherkin: the upstream body equals the client's, byte for byte; only the key header
    differs; the answer (whole and streamed, unknown events included) comes back as sent; the
    ledger reads its usage."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        for stream in (False, True):
            srv.fake.queue.append({"message": ANSWER, "extra_events": [
                ("future_event", {"type": "future_event", "x": 1})]})
            raw = _stream_raw(stream)
            st, h, data = px.post("/v1/messages", raw, headers=_hdr())
            assert st == 200, data
            up = srv.fake.last
            assert srv.fake.raws[-1] == raw, "the client's bytes, unchanged"
            assert up["path"] == "/v1/messages"
            got = {k.lower(): v for k, v in up["headers"].items()}
            assert got["x-api-key"] == BACKEND_KEY and "authorization" not in got
            assert got["anthropic-version"] == "2026-10-21"
            assert got["anthropic-beta"] == "some-beta-2026-01-01"
            assert got["content-type"] == "application/json"
            assert KEY not in json.dumps(up["headers"]), "the client's key never goes on"
            assert data == bytes(srv.fake.answers[-1]), "the answer's bytes, unchanged"
            assert h["request-id"] == "req_fake_1", "the backend's request-id, once"
            if stream:
                assert b"event: future_event" in data and b"signature_delta" in data
        rows = [r for r in px.rows() if r["endpoint"] == "messages"]
        assert len(rows) == 2
        for r in rows:
            assert (r["status"], r["user"], r["backend"]) == (200, "alice", "anth"), r
            assert (r["prompt_tokens"], r["cached_tokens"], r["cache_write_tokens"],
                    r["completion_tokens"]) == (2600, 2000, 500, 7), r
            assert r["finish_reason"] == "stop" and r["thinking"] == 1, r
    srv.shutdown()


def test_a_renamed_model_keeps_its_prefix():
    """Gherkin: with a public id that differs from the upstream one, the upstream body differs
    in the model value only: every other byte (key order, spacing, escapes) stays."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    with Proxy(_env(port)) as px:
        px.sync()
        px.svc.cfg.update_model("anth", "model-a", {"public_id": "house-model"})
        raw = RAW.replace(b'"model":  "model-a" ,', b'"model":  "house-model" ,')
        st, _, data = px.post("/v1/messages", raw, headers=_hdr())
        assert st == 200, data
        assert srv.fake.raws[-1] == RAW, (srv.fake.raws[-1][:80], RAW[:80])
    # the byte rewrite alone: one value, escapes and nesting kept
    src = b'{ "a":1.50, "model" : "x\\u00e9", "n":{"model":"keep"},"z":"\xc3\xa9"}'
    assert passthrough.set_model(src, 'up/m"1') == \
        b'{ "a":1.50, "model" : "up/m\\"1", "n":{"model":"keep"},"z":"\xc3\xa9"}'
    assert passthrough.set_model(b'{"x":1}', "m") == b'{"x":1}'
    srv.shutdown()


def test_no_hook_runs_no_translation_code():
    """AC: with no hook enabled a request that passes through (whole, streamed, count_tokens)
    calls none of the translation's functions (a spy on each)."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.count = 42
    names = ("from_messages", "to_messages", "from_message", "to_message", "StreamToChat",
             "ChatToMessages", "message_events", "usage_in", "usage_openai", "error_out",
             "error_in", "dropped", "thinking_of", "messages_error")
    calls: list = []
    saved = {n: getattr(tr, n) for n in names}

    def spy(n, fn):
        def wrapped(*a, **k):
            calls.append(n)
            return fn(*a, **k)
        return wrapped
    try:
        with Proxy(_env(port)) as px:
            px.sync()
            for n in names:
                setattr(tr, n, spy(n, saved[n]))
            for stream in (False, True):
                st, _, data = px.post("/v1/messages", _stream_raw(stream), headers=_hdr())
                assert st == 200, data
            st, _, data = px.post("/v1/messages/count_tokens", {
                "model": "model-a", "messages": [{"role": "user", "content": "x"}]},
                headers=_hdr())
            assert st == 200 and json.loads(data) == {"input_tokens": 42}, data
            assert calls == [], calls
            # the spy works: a translated request (an OpenAI client) calls them
            px.post("/v1/chat/completions", {"model": "model-a", "messages": [
                {"role": "user", "content": "hi"}]}, headers={"Authorization": f"Bearer {KEY}"})
            assert "to_messages" in calls and "from_message" in calls, calls
    finally:
        for n, fn in saved.items():
            setattr(tr, n, fn)
    srv.shutdown()


def test_errors_ours_in_shape_and_the_backends_as_sent():
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    body = {"model": "model-a", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
    with Proxy(_env(port)) as px:
        px.sync()
        # the backend's own 400: as it sent it
        srv.fake.queue.append({"status": 400, "body": {
            "type": "error", "error": {"type": "invalid_request_error", "message": "nope"},
            "request_id": "req_up"}})
        st, h, data = px.post("/v1/messages", body, headers=_hdr())
        assert st == 400 and json.loads(data)["request_id"] == "req_up", data
        assert h["request-id"] == "req_fake_1"
        # a 529 three times: tried again, then the backend's 529 as it sent it
        for _ in range(3):
            srv.fake.queue.append({"status": 529, "body": {
                "type": "error", "error": {"type": "overloaded_error", "message": "busy"}}})
        st, h, data = px.post("/v1/messages", body, headers=_hdr())
        assert st == 529 and json.loads(data)["error"]["type"] == "overloaded_error", data
        # ours: a drained backend is 529 overloaded_error in the Messages shape
        px.svc.drain_of = lambda bid: "node-1"
        st, h, data = px.post("/v1/messages", body, headers=_hdr())
        doc = json.loads(data)
        assert st == 529 and doc["type"] == "error" and \
            doc["error"]["type"] == "overloaded_error", data
        assert h["request-id"] == doc["request_id"] and h["Retry-After"] == "30"
        px.svc.drain_of = lambda bid: None
        # a missing key: 401 authentication_error
        st, _, data = px.post("/v1/messages", body, headers=_hdr(key=None, **OUTSIDE))
        assert st == 401 and json.loads(data)["error"]["type"] == "authentication_error"
        # a stream the backend cuts: an `error` event at the end, never an OpenAI frame
        srv.fake.queue.append({"message": {"content": [{"type": "text", "text": "abcdef"}],
                                           "stop_reason": "end_turn"}, "error_after": 1})
        st, _, data = px.post("/v1/messages", dict(body, stream=True), headers=_hdr())
        assert st == 200 and data.rstrip().split(b"\n\n")[-1].startswith(b"event: error")
        assert b"[DONE]" not in data
        rows = [r for r in px.rows() if r["endpoint"] == "messages"]
        assert rows[-1]["finish_reason"] == "error", rows[-1]
        # a stream that ends without message_stop: the relay's own `error` event
        srv.fake.queue.append({"message": {"content": [{"type": "text", "text": "abcdef"}],
                                           "stop_reason": "end_turn"}, "cut_after": 4})
        st, _, data = px.post("/v1/messages", dict(body, stream=True), headers=_hdr())
        last = data.rstrip().split(b"\n\n")[-1]
        assert st == 200 and last.startswith(b"event: error") and b"api_error" in last, data
        assert data.rstrip()[:-len(last)].rstrip() == bytes(srv.fake.answers[-1]).rstrip()
        r = [r for r in px.rows() if r["endpoint"] == "messages"][-1]
        assert (r["finish_reason"], r["error_type"]) == ("error", "upstream_closed"), r
    srv.shutdown()
    # a backend that takes a bearer gets one, and no x-api-key
    srv, port = fake_anthropic.start(key=BACKEND_KEY, auth="bearer")
    with Proxy(_env(port, {"TANDEM_BACKEND_ANTH_ANTHROPIC_AUTH": "bearer"})) as px:
        px.sync()
        st, _, data = px.post("/v1/messages", body, headers=_hdr())
        got = {k.lower(): v for k, v in srv.fake.last["headers"].items()}
        assert st == 200 and got["authorization"] == f"Bearer {BACKEND_KEY}" and \
            "x-api-key" not in got, (data, got)
    srv.shutdown()


def test_count_tokens_passes_and_a_mixed_route_is_translated():
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.count = 7
    gen, gport = fake_backend.start(models=["model-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        raw = b'{"model":"model-a", "messages":[{"role":"user","content":"x"}],"z":1}'
        st, h, data = px.post("/v1/messages/count_tokens", raw, headers=_hdr())
        assert st == 200 and json.loads(data) == {"input_tokens": 7}, data
        assert srv.fake.raws[-1] == raw and h["request-id"] == "req_fake_1"
        assert srv.fake.last["headers"]["anthropic-beta"] == "some-beta-2026-01-01"
    env = _env(port, {"TANDEM_BACKENDS": "anth,gen", "TANDEM_BACKEND_ANTH_FALLBACK": "gen",
                      "TANDEM_BACKEND_GEN_KIND": "generic",
                      "TANDEM_BACKEND_GEN_URL": f"http://127.0.0.1:{gport}/v1",
                      "TANDEM_ROUTE_1": "model-a=anth"})
    with Proxy(env) as px:
        px.sync()
        st, _, data = px.post("/v1/messages", RAW.replace(b',{"type":"web_search_20260209",'
                                                          b'"name":"web_search"}', b''),
                              headers=_hdr())
        assert st == 200, data
        up = srv.fake.last
        assert srv.fake.raws[-1] != RAW and "temperature" not in up["body"], "translated"
        assert up["headers"]["anthropic-version"] == "2023-06-01"
    srv.shutdown()
    gen.shutdown()


# ------------------------------------------------------------------------- hooks
def test_a_hook_that_wants_to_rewrite():
    """Gherkin: a transform that patches messages on a request that passes through is refused,
    journaled protocol_native, and its policy applies (open: the request goes as sent; closed:
    refused in the Messages shape). A parameter patch reaches the native body; a guard's deny
    and an answer patch work as the design says."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    hs, hport = hook_server()
    body = {"model": "model-a", "max_tokens": 10,
            "messages": [{"role": "user", "content": "x"}]}
    try:
        with Proxy(_env(port)) as px:
            px.sync()

            def hook(doc):
                st, _, out = px.post("/v1/fleet/hooks", doc)
                assert st == 201, out
                return json.loads(out)["hook"]["id"]

            def journal(hid):
                st, _, out = px.get(f"/v1/fleet/hooks/{hid}/calls")
                return json.loads(out)["calls"]
            hs.respond = lambda env: {"action": "modify", "patch": {
                "messages": [{"role": "user", "content": "changed"}]}}
            hid = hook({"name": "rewrite", "endpoint": f"http://127.0.0.1:{hport}/",
                        "role": "transform", "payload": "full", "policy": "open",
                        "stages": ["request.before_engine"], "priority": 1})
            raw = json.dumps(body).encode()
            st, _, data = px.post("/v1/messages", raw, headers=_hdr())
            assert st == 200 and srv.fake.raws[-1] == raw, data
            env = hs.calls[-1]["env"]
            assert env["request"]["protocol"] == {"inbound": "messages", "upstream": "messages"}
            assert env["event"]["content"]["messages"] == [{"role": "user", "content": "x"}], \
                "the hook saw the canonical chat shape"
            refused = [c for c in journal(hid) if c["outcome"] == "refused"]
            assert refused and refused[-1]["error"].startswith("protocol_native"), \
                journal(hid)
            # closed: the request is refused, in the Messages shape
            st, _, out = px.request("PATCH", f"/v1/fleet/hooks/{hid}", {"policy": "closed"})
            assert st == 200, out
            n = len(srv.fake.requests)
            st, h, data = px.post("/v1/messages", body, headers=_hdr())
            doc = json.loads(data)
            assert st == 529 and doc["type"] == "error" and "protocol_native" in \
                doc["error"]["message"], data
            assert len(srv.fake.requests) == n, "nothing went upstream"
            # a parameter patch reaches the native body, in its own field
            hs.respond = lambda env: {"action": "modify", "patch": {
                "temperature": 0.1, "max_tokens": 5, "stop": "END"}}
            st, _, data = px.post("/v1/messages", body, headers=_hdr())
            assert st == 200, data
            assert srv.fake.last["body"] == dict(body, temperature=0.1, max_tokens=5,
                                                 stop_sequences=["END"]), srv.fake.last["body"]
            # a guard's deny keeps the Messages shape
            hook({"name": "stop", "kind": "builtin", "target": "deny", "role": "guard",
                  "stages": ["request.received"], "priority": 100,
                  "options": {"reason": "not today"}})
            st, h, data = px.post("/v1/messages", body, headers=_hdr())
            assert st == 403 and json.loads(data)["error"] == {"type": "permission_error",
                                                               "message": "not today"}
    finally:
        srv.shutdown()
        hs.shutdown()


def test_a_hook_that_moves_the_request_off_the_messages_api():
    """A hook that moves a request that passes through to a backend of another kind (a model
    patch at request.received, a route.select decision) refuses it, 409 protocol_native in the
    Messages shape, nothing upstream; a request.error fallback of another kind is not tried and
    the backend's own failure stands. Parameter patches land whatever their order."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    gen, gport = fake_backend.start(models=["fake-a"])
    env = _env(port, {"TANDEM_BACKENDS": "anth,gen", "TANDEM_BACKEND_GEN_KIND": "generic",
                      "TANDEM_BACKEND_GEN_URL": f"http://127.0.0.1:{gport}/v1"})
    body = {"model": "model-a", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}
    with Proxy(env) as px:
        px.sync()

        def hook(doc):
            st, _, out = px.post("/v1/fleet/hooks", dict(doc, kind="builtin", priority=1))
            assert st == 201, out
            return json.loads(out)["hook"]["id"]

        def off(hid):
            st, _, out = px.request("DELETE", f"/v1/fleet/hooks/{hid}")
            assert st in (200, 204), out
        for doc in ({"name": "m", "target": "set", "role": "transform",
                     "stages": ["request.received"], "options": {"set": {"model": "fake-a"}}},
                    {"name": "r", "target": "route", "role": "decider",
                     "stages": ["route.select"], "options": {"backend": "gen"}}):
            hid = hook(doc)
            n = len(srv.fake.requests) + len(gen.fake.bodies)
            st, h, data = px.post("/v1/messages", body, headers=_hdr())
            d = json.loads(data)
            assert st == 409 and d["type"] == "error" and "Messages API" in \
                d["error"]["message"] and h["request-id"] == d["request_id"], data
            assert len(srv.fake.requests) + len(gen.fake.bodies) == n, "nothing upstream"
            off(hid)
        hid = hook({"name": "fb", "target": "fallback", "role": "decider",
                    "stages": ["request.error"], "options": {"backend": "gen"}})
        srv.fake.queue.append({"status": 400, "body": {"type": "error", "error": {
            "type": "invalid_request_error", "message": "nope"}}})
        for _ in range(3):
            srv.fake.queue.append({"status": 529, "body": {"type": "error", "error": {
                "type": "overloaded_error", "message": "busy"}}})
        st, _, data = px.post("/v1/messages", body, headers=_hdr())
        assert st == 400, data                        # not retried: the backend's own 400
        st, _, data = px.post("/v1/messages", body, headers=_hdr())
        assert st == 529 and json.loads(data)["error"]["message"] == "busy", data
        assert len(srv.fake.queue) == 2, "one try: the fallback hook's pick was not tried"
        srv.fake.queue.clear()
        assert gen.fake.bodies == [] or all(b.get("model") != "model-a"
                                            for b in gen.fake.bodies)
        off(hid)
        hid = hook({"name": "p", "target": "set", "role": "transform",
                    "stages": ["request.before_engine"], "options": {"set": {
                        "response_format": {"type": "json_schema",
                                            "json_schema": {"schema": {"type": "object"}}},
                        "reasoning_effort": "high", "parallel_tool_calls": False,
                        "tool_choice": "auto"}}})
        st, _, data = px.post("/v1/messages", body, headers=_hdr())
        assert st == 200, data
        assert srv.fake.last["body"] == dict(
            body, tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": "high", "format": {"type": "json_schema",
                                                        "schema": {"type": "object"}}}), \
            srv.fake.last["body"]
    srv.shutdown()
    gen.shutdown()


def test_hooks_name_the_protocols():
    """hooks-v1 1.1: request.protocol is {openai, openai} for an OpenAI client on an OpenAI
    backend, {openai, messages} on a Messages API backend, {messages, openai} for a Messages
    client on an engine; an answer patch on a pass-through is refused."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    gen, gport = fake_backend.start(models=["fake-a"])
    hs, hport = hook_server()
    hs.respond = lambda env: None
    env = _env(port, {"TANDEM_BACKENDS": "anth,gen", "TANDEM_BACKEND_GEN_KIND": "generic",
                      "TANDEM_BACKEND_GEN_URL": f"http://127.0.0.1:{gport}/v1"})
    try:
        with Proxy(env) as px:
            px.sync()
            st, _, out = px.post("/v1/fleet/hooks", {
                "name": "see", "endpoint": f"http://127.0.0.1:{hport}/", "role": "transform",
                "stages": ["request.received", "response.after_engine"], "priority": 1,
                "payload": "full"})
            assert st == 201, out
            bearer = {"Authorization": f"Bearer {KEY}"}
            cases = ((("/v1/chat/completions", "fake-a", bearer), ("openai", "openai")),
                     (("/v1/chat/completions", "model-a", bearer), ("openai", "messages")),
                     (("/v1/messages", "fake-a", _hdr()), ("messages", "openai")),
                     (("/v1/messages", "model-a", _hdr()), ("messages", "messages")))
            for (path, model, hdr), (inb, upb) in cases:
                hs.calls.clear()
                st, _, data = px.post(path, {"model": model, "max_tokens": 10, "messages": [
                    {"role": "user", "content": "x"}]}, headers=hdr)
                assert st == 200, (path, model, data)
                got = [c["env"]["request"]["protocol"] for c in hs.calls]
                assert got and all(p == {"inbound": inb, "upstream": upb} for p in got), got
            # a stream.chunk observer on a streamed pass-through sees canonical chunks
            st, out = px.post("/v1/fleet/hooks", {
                "name": "chunks", "endpoint": f"http://127.0.0.1:{hport}/",
                "role": "observer", "stages": ["stream.chunk"], "priority": 2})[:2]
            assert st == 201, out
            hs.calls.clear()
            st, _, data = px.post("/v1/messages", {"model": "model-a", "max_tokens": 10,
                                                   "stream": True, "messages": [
                                                       {"role": "user", "content": "x"}]},
                                  headers=_hdr())
            assert st == 200 and data == bytes(srv.fake.answers[-1]), data
            px.svc.hooks.bg.drain(5)
            kinds = [c["env"]["event"]["kind"] for c in hs.calls
                     if c["env"]["stage"] == "stream.chunk"]
            assert "content" in kinds and "finish" in kinds and "done" in kinds, kinds
            # an answer patch on a pass-through: refused, the answer as the backend sent it
            hs.respond = lambda env: {"action": "modify", "patch": {"choices": []}} \
                if env["stage"] == "response.after_engine" else None
            st, _, data = px.post("/v1/messages", {"model": "model-a", "max_tokens": 10,
                                                   "messages": [{"role": "user",
                                                                 "content": "x"}]},
                                  headers=_hdr())
            assert st == 200 and data == bytes(srv.fake.answers[-1]), data
    finally:
        srv.shutdown()
        gen.shutdown()
        hs.shutdown()


# ------------------------------------------------------------------------- /v1/models
def test_models_in_two_shapes():
    """Gherkin: with anthropic-version the list is {data: [{type: model, ...}], has_more,
    first_id, last_id}, paged by limit and after_id/before_id; without it the answer is today's,
    engine cards and decision models included, byte for byte."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    eng, eport = fake_backend.start(models=["TandemLLM"], cache=False)
    eng.fake.cards["TandemLLM"] = {"profile": "chat", "tandem": {
        "backend": "engine", "kind": "engine", "context_length": 262144,
        "capabilities": {"thinking": True, "tools": True, "vision": True}}}
    eng.fake.top = {"decision_models": [{"id": "decider-1", "object": "decision_model",
                                         "created": 1, "owned_by": "x"}]}
    env = _env(port, {"TANDEM_BACKENDS": "anth,engine", "TANDEM_BACKEND_ENGINE_KIND": "tandemllm",
                      "TANDEM_BACKEND_ENGINE_URL": f"http://127.0.0.1:{eport}/v1"})
    with Proxy(env) as px:
        px.sync()
        bearer = {"Authorization": f"Bearer {KEY}"}
        st, h, data = px.get("/v1/models", headers=bearer)
        assert st == 200
        want = json.dumps({"object": "list", "data": px.svc.models(int(px.svc.started)),
                           **px.svc.model_extras()}, ensure_ascii=False).encode()
        assert data == want, "today's answer, byte for byte"
        doc = json.loads(data)
        assert doc["decision_models"][0]["id"] == "decider-1" and \
            any(m["id"] == "TandemLLM" and m.get("profile") == "chat" for m in doc["data"])
        ids = [m["id"] for m in doc["data"]]
        for hdr in ({"anthropic-version": "2023-06-01", **bearer}, {"x-api-key": KEY}):
            st, h, data = px.get("/v1/models?limit=2", headers=hdr)
            page = json.loads(data)
            assert st == 200 and set(page) == {"data", "has_more", "first_id", "last_id"}, data
            assert h["request-id"].startswith("req_")
            assert [m["id"] for m in page["data"]] == ids[:2] and page["has_more"] is True
            assert (page["first_id"], page["last_id"]) == (ids[0], ids[1])
            m = page["data"][0]
            assert m["type"] == "model" and m["display_name"] and \
                m["created_at"].endswith("Z") and m["lifecycle"] == "active"
            assert set(m["capabilities"]) == {
                "batch", "citations", "code_execution", "context_management", "effort",
                "image_input", "pdf_input", "server_tools", "structured_outputs", "thinking"}
        st, _, data = px.get(f"/v1/models?after_id={ids[1]}&limit=1000",
                             headers={"x-api-key": KEY})
        nxt = json.loads(data)
        assert [m["id"] for m in nxt["data"]] == ids[2:] and nxt["has_more"] is False
        st, _, data = px.get(f"/v1/models?before_id={ids[2]}&limit=1",
                             headers={"x-api-key": KEY})
        assert [m["id"] for m in json.loads(data)["data"]] == [ids[1]]
        st, _, data = px.get("/v1/models/TandemLLM", headers={"x-api-key": KEY})
        one = json.loads(data)
        assert st == 200 and one["type"] == "model" and one["id"] == "TandemLLM", data
        assert one == next(m for m in nxt["data"] + page["data"] if m["id"] == "TandemLLM")
        assert one["max_input_tokens"] == 262144 and \
            one["capabilities"]["image_input"]["supported"] is True and \
            one["capabilities"]["thinking"]["supported"] is True
        st, _, data = px.get("/v1/models/model-b", headers={"x-api-key": KEY})
        b = json.loads(data)
        assert b["display_name"] == "Model B" and b["max_input_tokens"] == 200001 and \
            b["max_tokens"] == 32000 and b["capabilities"]["image_input"]["supported"] is False
        assert b["capabilities"]["effort"]["max"]["supported"] is False
        st, _, data = px.get("/v1/models/nope", headers={"x-api-key": KEY})
        assert st == 404 and json.loads(data)["error"]["type"] == "not_found_error"
        st, _, data = px.get("/v1/models?limit=0", headers={"x-api-key": KEY})
        assert st == 400 and json.loads(data)["error"]["type"] == "invalid_request_error"
        st, _, data = px.get("/v1/models", headers={"anthropic-version": "2023-06-01",
                                                    **OUTSIDE})
        assert st == 401 and json.loads(data)["error"]["type"] == "authentication_error"
    srv.shutdown()
    eng.shutdown()


# ------------------------------------------------------------------------- the stash
def _loop(px, fake, stream: bool, key=KEY, turns=3, history=None):
    """An OpenAI client's tool loop on the Messages API backend; [(status, body)] per turn.
    `history` (a list) gets the conversation, the last answer included."""
    msgs = [{"role": "user", "content": "weather in two cities?"}]
    out = []
    for _ in range(turns):
        st, _, data = px.post("/v1/chat/completions", {
            "model": "model-a", "messages": msgs, "tools": [FN], "stream": stream,
            "reasoning_effort": "low"}, headers={"Authorization": f"Bearer {key}"})
        out.append((st, data))
        if st != 200:
            break
        if stream:
            content, calls = "", {}
            for ev in data.split(b"\n\n"):
                if not ev.startswith(b"data: {"):
                    continue
                for ch in json.loads(ev[6:]).get("choices") or []:
                    d = ch.get("delta") or {}
                    content += d.get("content") or ""
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc["index"], {"id": None, "type": "function",
                                                           "function": {"name": None,
                                                                        "arguments": ""}})
                        c["id"] = tc.get("id") or c["id"]
                        f = tc.get("function") or {}
                        c["function"]["name"] = f.get("name") or c["function"]["name"]
                        c["function"]["arguments"] += f.get("arguments") or ""
            msg = {"role": "assistant", "content": content or None}
            if calls:
                msg["tool_calls"] = [calls[i] for i in sorted(calls)]
        else:
            msg = json.loads(data)["choices"][0]["message"]
            msg.pop("reasoning_content", None)
        if history is not None:
            history[:] = msgs + [msg]
        if not msg.get("tool_calls"):
            break
        msgs = msgs + [msg] + [{"role": "tool", "tool_call_id": c["id"], "content": "12 C"}
                               for c in msg["tool_calls"]]
    return out


def test_the_stash_keeps_thinking_in_a_tool_loop():
    """Gherkin: a 3-turn tool loop from an OpenAI client on a thinking model; the fake server
    refuses a turn that comes back without its signed thinking. With the stash every turn
    passes and the upstream history holds the original thinking blocks and signatures, a
    follow-up after the final (text) answer too; without it (a stash that keeps nothing) the
    second turn is refused."""
    for stream in (False, True):
        srv, port = fake_anthropic.start(key=BACKEND_KEY)
        srv.fake.think = srv.fake.strict_thinking = True
        with Proxy(_env(port)) as px:
            px.sync()
            hist: list = []
            got = _loop(px, srv.fake, stream, history=hist)
            assert [s for s, _ in got] == [200, 200, 200], got
            assert ("text", "done after 2 results") in srv.fake.produced, "the loop ended"
            last = srv.fake.last["body"]["messages"]
            th = [b for m in last if m["role"] == "assistant" for b in m["content"]
                  if b["type"] == "thinking"]
            assert [b["thinking"] for b in th] == ["Plan 1.", "Plan 2."], last
            assert all(b["signature"] in srv.fake.signatures for b in th)
            assert px.svc.relay.stash.view()["turns"] == 3
            # a follow-up after the final answer: its text turn keeps its thinking too
            st, _, data = px.post("/v1/chat/completions", {
                "model": "model-a", "tools": [FN], "stream": stream,
                "messages": hist + [{"role": "user", "content": "and tomorrow?"}]},
                headers={"Authorization": f"Bearer {KEY}"})
            assert st == 200, data
            sent = srv.fake.last["body"]["messages"]
            assert sent[-2]["content"][0]["thinking"] == "Plan 3.", sent[-2]
            assert all(r["error_type"] is None for r in px.rows()), px.rows()
            # the control: with nothing kept, the fake refuses the second turn
            px.svc.relay.stash = stash_mod.Stash(max_bytes=0)
            got = _loop(px, srv.fake, stream)
            assert [s for s, _ in got] == [200, 400], got
            assert b"Invalid `signature`" in got[-1][1]
        srv.shutdown()


def test_the_stash_an_evicted_turn_marks_the_row():
    """Gherkin: a turn the stash evicted (here: after its hour) goes without its thinking; the
    ledger row says thinking_evicted and the answer names `thinking` in X-Tandem-Dropped."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.think = True
    clock = Clock()
    with Proxy(_env(port)) as px:
        px.sync()
        px.svc.relay.stash = stash_mod.Stash(clock=clock)
        msgs = [{"role": "user", "content": "weather?"}]
        hdr = {"Authorization": f"Bearer {KEY}"}
        req = {"model": "model-a", "messages": msgs, "tools": [FN]}
        st, _, data = px.post("/v1/chat/completions", req, headers=hdr)
        msg = json.loads(data)["choices"][0]["message"]
        msgs += [msg, {"role": "tool", "tool_call_id": msg["tool_calls"][0]["id"],
                       "content": "12 C"}]
        clock.advance(stash_mod.TTL_S + 1)
        st, h, data = px.post("/v1/chat/completions", dict(req, messages=msgs), headers=hdr)
        assert st == 200, data
        sent = srv.fake.last["body"]["messages"][1]["content"]
        assert [b["type"] for b in sent] == ["text", "tool_use"], sent
        assert "thinking" in h["X-Tandem-Dropped"].split(","), h
        rows = px.rows()
        assert rows[-1]["error_type"] == "thinking_evicted" and rows[-1]["status"] == 200
        assert rows[0]["error_type"] is None
    srv.shutdown()


def test_the_stash_is_per_token_over_http():
    """AC: another key sending the same history gets no thinking of the first key's."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.think = True
    with Proxy(_env(port)) as px:
        px.sync()
        got = _loop(px, srv.fake, False, turns=2)
        assert [s for s, _ in got] == [200, 200]
        hist = srv.fake.last["body"]["messages"]
        assert any(b["type"] == "thinking" for b in hist[1]["content"])
        msgs = [{"role": "user", "content": "weather in two cities?"},
                {"role": "assistant", "content": "Call 1.", "tool_calls": [
                    {"id": "toolu_fake_1", "type": "function", "function": {
                        "name": "get_weather", "arguments": '{"city": "City 1"}'}}]},
                {"role": "tool", "tool_call_id": "toolu_fake_1", "content": "12 C"}]
        st, _, data = px.post("/v1/chat/completions", {"model": "model-a", "messages": msgs,
                                                       "tools": [FN]},
                              headers={"Authorization": f"Bearer {KEY2}"})
        assert st == 200, data
        sent = srv.fake.last["body"]["messages"][1]["content"]
        assert not any(b["type"] == "thinking" for b in sent), sent
    srv.shutdown()


def test_the_stash_is_per_token_for_messages_clients_too():
    """A Messages API client on a mixed route (translated twice) keeps its thinking through the
    stash, under its own key: the same history from another key goes without."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.think = True
    gen, gport = fake_backend.start(models=["model-a"])
    env = _env(port, {"TANDEM_BACKENDS": "anth,gen", "TANDEM_BACKEND_ANTH_FALLBACK": "gen",
                      "TANDEM_BACKEND_GEN_KIND": "generic",
                      "TANDEM_BACKEND_GEN_URL": f"http://127.0.0.1:{gport}/v1",
                      "TANDEM_ROUTE_1": "model-a=anth"})
    with Proxy(env) as px:
        px.sync()
        req = {"model": "model-a", "max_tokens": 100, "tools": [TOOL],
               "messages": [{"role": "user", "content": "weather?"}]}
        st, _, data = px.post("/v1/messages", req, headers=_hdr())
        m = json.loads(data)
        assert st == 200 and m["content"][0]["type"] == "thinking", m
        call = m["content"][-1]
        nxt = dict(req, messages=req["messages"] + [
            {"role": "assistant", "content": m["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "content": "12 C"}]}])
        for key, kept in ((KEY, True), (KEY2, False)):
            st, _, data = px.post("/v1/messages", nxt, headers=_hdr(key=key))
            assert st == 200, data
            sent = srv.fake.last["body"]["messages"][1]["content"]
            th = [b for b in sent if b["type"] == "thinking"]
            assert (th[0]["signature"] in srv.fake.signatures if kept else th == []), sent
    srv.shutdown()
    gen.shutdown()


def test_the_stash_is_bounded_and_per_token():
    """AC: 64 MB and 1 hour by default; least recently used out first; per token, backend and
    model; an evicted turn and every earlier one go without thinking (front, never middle)."""
    assert stash_mod.MAX_BYTES == 64 * 1024 * 1024 and stash_mod.TTL_S == 3600.0
    clock = Clock()

    def turn(n, think=True):
        return ([{"type": "thinking", "thinking": f"t{n}", "signature": f"s{n}"}]
                if think else []) + [{"type": "text", "text": f"call {n}"},
                                     {"type": "tool_use", "id": f"c{n}", "name": "f",
                                      "input": {"n": n}}]

    def sent(n):          # the turn as a translated history holds it
        return {"role": "assistant", "content": [{"type": "text", "text": f"call {n}"},
                                                 {"type": "tool_use", "id": f"c{n}",
                                                  "name": "f", "input": {"n": n}}]}
    one = len(json.dumps(turn(1)).encode())
    s = stash_mod.Stash(max_bytes=one * 3, clock=clock)
    scope = ("k:a", "anth", "model-a")
    for n in (1, 2, 3):
        assert s.keep(scope, turn(n), at=2 * n - 1)
    assert not s.keep(scope, turn(9, think=False), at=0), "no thinking, nothing kept"
    hist = [{"role": "user", "content": "q"}]
    for n in (1, 2, 3):
        hist += [sent(n), {"role": "user", "content": [{"type": "tool_result",
                                                        "tool_use_id": f"c{n}"}]}]
    out, dropped = s.restore(scope, hist)
    assert dropped == 0 and [out[i]["content"][0]["signature"] for i in (1, 3, 5)] == \
        ["s1", "s2", "s3"]
    # another token, backend or model: nothing
    for other in (("k:b", "anth", "model-a"), ("k:a", "other", "model-a"),
                  ("k:a", "anth", "model-b")):
        assert s.restore(other, hist) == (hist, 0)
    # a fourth turn pushes the least recently used out (bytes)
    clock.advance(1)
    s.restore(scope, hist[:4])                  # turns 1 and 2 used now, 3 not
    assert s.keep(scope, turn(4), at=7)
    assert s.view()["turns"] == 3 and s.bytes <= one * 3
    out, dropped = s.restore(scope, hist + [sent(4)])
    # turn 3 is gone: it and every earlier turn go without thinking; turn 4 keeps its own
    assert dropped == 3, dropped
    assert all(out[i] == hist[i] for i in (1, 3, 5))
    assert out[7]["content"][0]["signature"] == "s4"
    # a changed call is not restored
    bad = dict(sent(4), content=[sent(4)["content"][0], dict(sent(4)["content"][1],
                                                              input={"n": 5})])
    out, dropped = s.restore(scope, [bad])
    assert dropped == 1 and out[0] == bad
    # the hour
    clock.advance(stash_mod.TTL_S + 1)
    out, dropped = s.restore(scope, [sent(4)])
    assert dropped == 1 and s.view()["turns"] == 0
    # a stream's blocks reach the stash with their signatures
    kept = []
    sc = tr.StreamToChat(0, keep=kept.append)
    for name, obj in fake_anthropic._events(fake_anthropic._message(
            {"content": turn(1) + [{"type": "redacted_thinking", "data": "xx"}],
             "stop_reason": "tool_use", "usage": {}}, "m", 1)):
        sc.feed(f"event: {name}\ndata: {json.dumps(obj)}".encode())
    assert kept == [turn(1) + [{"type": "redacted_thinking", "data": "xx"}]], kept


def test_the_1h_cache_write_price():
    """A price row's `cache_write_1h` prices the 1 h writes; without one they cost the cache
    write price; the 5 minute writes and the reads as before."""
    from proxy.adapters.anthropic import Anthropic
    a = Anthropic({"id": "anth", "kind": "anthropic", "url": "http://x", "anthropic": {}})
    u = a.read_usage(ANSWER["usage"])
    assert (u["prompt_tokens"], u["cached_tokens"], u["cache_write_tokens"],
            u["cache_write_1h_tokens"]) == (2600, 2000, 500, 300), u
    price = {"input": 2.0, "cached_input": 0.2, "cache_write": 2.5, "output": 10.0}
    base = 100 * 2.0 + 2000 * 0.2 + 7 * 10.0
    assert Adapter.cost(u, price) == round(base + 500 * 2.5)
    assert Adapter.cost(u, dict(price, cache_write_1h=4.0)) == round(base + 200 * 2.5
                                                                        + 300 * 4.0)
    assert Adapter.cost(dict(u, cache_write_1h_tokens=None), dict(price, cache_write_1h=4.0)) \
        == round(base + 500 * 2.5)
    # end to end: the price row from its variable (a fifth value), the row's cost
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    with Proxy(_env(port, {"TANDEM_PRICE_1": "anth/model-a=2,0.2,10,2.5,4"})) as px:
        px.sync()
        row = next(m for m in px.svc.cfg.models("anth") if m["model"] == "model-a")
        assert row["price"]["cache_write_1h"] == 4.0, row["price"]
        # a price set without the field (the page has none) keeps it; null clears it
        p = {"input": 2, "cached_input": 0.2, "cache_write": 2.5, "output": 10}
        for price, want in ((dict(p, cache_write_1h=3.0), 3.0), (p, 3.0),
                            (dict(p, cache_write_1h=None), None)):
            px.svc.cfg.update_model("anth", "model-b", {"price": price})
            row = next(m for m in px.svc.cfg.models("anth") if m["model"] == "model-b")
            assert row["price"].get("cache_write_1h") == want, (price, row["price"])
        for stream in (False, True):
            srv.fake.queue.append({"message": ANSWER})
            st, _, data = px.post("/v1/messages", _stream_raw(stream), headers=_hdr())
            assert st == 200, data
        rows = [r for r in px.rows() if r["endpoint"] == "messages"]
        assert [r["cost_micros"] for r in rows] == [2370, 2370], rows
    srv.shutdown()


if __name__ == "__main__":
    run(globals())
