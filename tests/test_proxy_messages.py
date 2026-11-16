"""SRV-879: the Messages API in front of TandemApp (proxy/messages.py, cell 4 of
proxy/translate/anthropic.py).

  1  golden fixtures for every row of the cell 4 table, requests, whole answers, streams and
     errors (tests/fixtures/proxy/anthropic/cell4.json, written from the documented shapes)
  2  a strict client of the stream, written from the documented event flow (message_start; per
     block a start, its deltas, its stop; message_delta; message_stop; ping anywhere after the
     start; an error ends it), that also folds the events into the final message the way a client
     library does; every stream of this file passes through it
  3  end to end against a fake OpenAI-compatible engine: a message with a system prompt and a
     tool, a streamed tool call, both key headers and neither upstream, errors in shape with a
     request-id (401, 404, 529), count_tokens exact or 404, a server tool refused, images, a tool
     loop of 3 turns, stop reasons and usage, a silent prefill kept alive by ping, a client that
     leaves, cache_control never upstream, a model on a Messages API backend (both translations)
  4  the harness's guard: refused on a backend of kind anthropic (config, variables), held off at
     run time, its side requests never sent there, and the health page's finding

Run: python tests/test_proxy_messages.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
from proxy import messages as messages_mod  # noqa: E402
from proxy.translate import anthropic as tr  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "proxy", "anthropic",
                   "cell4.json")
ROWS = ("system prompts", "cache control", "thinking", "tool choice", "tools", "images",
        "stop reasons", "parameters", "stream")
KEY = "user-key-0123456789abcdef"
BACKEND_KEY = FIXTURE_KEYS[0]
TOOL = {"name": "get_weather", "description": "Weather of a city",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}


def _fx() -> dict:
    with open(FIX) as fh:
        return json.load(fh)


# ------------------------------------------------------------------------- 2 the strict client
STOPS = ("end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal",
         "model_context_window_exceeded")
DELTAS = {"text": ("text_delta",), "thinking": ("thinking_delta", "signature_delta"),
          "tool_use": ("input_json_delta",)}


def parse(data: bytes) -> list[tuple[str, dict]]:
    """SSE bytes -> [(event name, data)]; each event names its type twice and the two agree."""
    out = []
    for raw in data.replace(b"\r\n", b"\n").split(b"\n\n"):
        if not raw.strip():
            continue
        name, payload = None, []
        for line in raw.split(b"\n"):
            if line.startswith(b"event:"):
                name = line[6:].strip().decode()
            elif line.startswith(b"data:"):
                payload.append(line[5:].lstrip(b" "))
        assert name and payload, raw
        obj = json.loads(b"\n".join(payload))
        assert obj["type"] == name, (name, obj)
        out.append((name, obj))
    return out


def strict(data: bytes) -> dict:
    """The documented flow, checked event by event; the final message folded from it (text
    joined, thinking and its signature, each tool's input parsed from its joined partial JSON,
    usage from message_start then updated by message_delta). An `error` event ends the stream:
    {"error": {...}, "partial": ...}."""
    ev = parse(data)
    assert ev and ev[0][0] == "message_start", [e[0] for e in ev[:3]]
    m = ev[0][1]["message"]
    assert m["type"] == "message" and m["role"] == "assistant" and m["content"] == [] and \
        m["stop_reason"] is None and m["id"].startswith("msg_"), m
    msg = {"id": m["id"], "model": m["model"], "content": [], "usage": dict(m["usage"]),
           "stop_reason": None, "stop_sequence": None}
    state, cur, parts, nxt = "blocks", None, [], 0
    for i, (name, obj) in enumerate(ev[1:], 1):
        assert state != "stopped", f"event after message_stop: {name}"
        if name == "ping":
            continue
        if name == "error":
            assert set(obj["error"]) >= {"type", "message"}, obj
            assert i == len(ev) - 1, "nothing after an error"
            return {"error": obj["error"], "partial": msg}
        if name == "content_block_start":
            assert state == "blocks" and cur is None, (name, i)
            assert obj["index"] == nxt, (obj["index"], nxt)
            b = obj["content_block"]
            assert b["type"] in DELTAS, b
            if b["type"] == "text":
                assert b == {"type": "text", "text": ""}, b
            elif b["type"] == "thinking":
                assert b == {"type": "thinking", "thinking": "", "signature": ""}, b
            else:
                assert b["input"] == {} and b["id"] and b["name"], b
            cur, parts = dict(b), []
            sig_seen = False
            continue
        if name == "content_block_delta":
            assert cur is not None and obj["index"] == nxt, (name, i)
            d = obj["delta"]
            assert d["type"] in DELTAS[cur["type"]], (cur["type"], d)
            if d["type"] == "signature_delta":
                assert not sig_seen, "one signature"
                sig_seen = True
                cur["signature"] = d["signature"]
            else:
                assert not (cur["type"] == "thinking" and sig_seen), "the signature comes last"
                parts.append(d.get("text", d.get("thinking", d.get("partial_json"))))
            continue
        if name == "content_block_stop":
            assert cur is not None and obj["index"] == nxt, (name, i)
            if cur["type"] == "text":
                cur["text"] = "".join(parts)
            elif cur["type"] == "thinking":
                assert sig_seen, "a thinking block ends with its signature"
                cur["thinking"] = "".join(parts)
            else:
                inp = json.loads("".join(parts) or "{}")
                assert isinstance(inp, dict), inp
                cur["input"] = inp
            msg["content"].append(cur)
            cur, nxt = None, nxt + 1
            continue
        if name == "message_delta":
            assert cur is None, "message_delta inside a block"
            state = "delta"
            assert obj["delta"]["stop_reason"] in STOPS, obj
            msg["stop_reason"] = obj["delta"]["stop_reason"]
            msg["stop_sequence"] = obj["delta"].get("stop_sequence")
            msg["usage"].update({k: v for k, v in (obj.get("usage") or {}).items()
                                 if v is not None})
            continue
        if name == "message_stop":
            assert state == "delta", "message_stop before message_delta"
            state = "stopped"
            continue
        raise AssertionError(f"unknown event {name}")
    assert state == "stopped", f"the stream ended in {state}"
    return msg


def test_the_strict_client_refuses_what_the_flow_forbids():
    good = tr.message_events({"id": "msg_x", "model": "m", "content": [
        {"type": "text", "text": "a"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1}})
    assert strict(good)["content"] == [{"type": "text", "text": "a"}]
    ev = good.split(b"\n\n")
    bad = (b"\n\n".join([ev[1], ev[0]] + ev[2:]),           # a block before message_start
           b"\n\n".join(ev[:4] + ev[5:]),                   # message_delta inside a block
           b"\n\n".join(ev[:-2]) + b"\n\n",                 # no message_stop
           good + tr.PING)                                  # something after message_stop
    for b in bad:
        try:
            strict(b)
        except (AssertionError, KeyError, IndexError):
            continue
        raise AssertionError(b)


# ------------------------------------------------------------------------- 1 golden fixtures
def test_every_row_of_the_table_has_golden_fixtures():
    fx = _fx()
    rows = set()
    for part in ("requests", "answers", "streams"):
        rows |= {c["row"] for c in fx[part].values()}
    assert rows == set(ROWS), set(ROWS) ^ rows
    assert {c["row"] for c in fx["requests"].values()} >= set(ROWS) - {"stop reasons"}


def test_golden_requests():
    for name, c in _fx()["requests"].items():
        if "error" in c:
            try:
                tr.from_messages(c["body"])
            except tr.TranslateError as e:
                assert (e.param, e.message) == (c["error"], c["message"]), name
                assert c["error"].split(".")[0] in json.dumps(c["body"]) or \
                    c["error"] == "max_tokens", name
            else:
                raise AssertionError(f"{name}: no 400")
            continue
        chat, dropped, meta = tr.from_messages(c["body"])
        assert (chat, dropped, meta) == (c["want"], c["dropped"], c["meta"]), (name, chat)
        assert list(tr.thinking_of(c["body"])) == c["thinking"], name
        assert "cache_control" not in json.dumps(chat), name


class _H:
    """A client socket's handler: what `Out` writes to it."""

    def __init__(self):
        self.out, self.close_connection, self.status, self.headers = bytearray(), False, None, {}
        outer = self

        class W:
            def write(self, d):
                outer.out += d

            def flush(self):
                pass
        self.wfile = W()

    def send_response(self, st, *_):
        self.status = st

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass


def test_golden_answers_and_streams():
    import random
    rnd = random.Random(879)
    fx = _fx()
    for name, c in fx["answers"].items():
        got = tr.to_message(c["chat"], "msg_fixture", c["meta"])
        assert got == c["want"], (name, got)
        # the same answer as a stream passes the strict client and folds to the same message
        folded = strict(tr.message_events(got))
        assert folded["content"] == got["content"] and folded["stop_reason"] == \
            got["stop_reason"] and folded["usage"] == got["usage"], name
    for name, c in fx["streams"].items():
        conv = tr.ChatToMessages("msg_fixture", c["meta"])
        data = conv.start() + b"".join(conv.feed(e.encode()) for e in c["events"]) + \
            conv.finish()
        assert [o for _, o in parse(data)] == c["want"], name
        got = strict(data)
        if name == "tool_calls_in_pieces":
            assert [b["input"] for b in got["content"] if b["type"] == "tool_use"] == \
                [{"city": "Oslo"}, {}]
        if name == "error_frame":
            assert got["error"]["type"] == "api_error"
        # the same events through `Out` in random pieces, as a socket gives them: the same bytes
        raw = ("\n\n".join(c["events"]) + "\n\n").encode()
        for _ in range(10):
            h = _H()
            o = messages_mod.Out(h, {}, False, "msg_fixture", c["meta"], "req_t", [])
            o.send_response(200)
            o.send_header("Content-Type", "text/event-stream")
            o.end_headers()
            i = 0
            while i < len(raw):
                j = min(len(raw), i + rnd.randint(1, 40))
                o.wfile.write(raw[i:j])
                i = j
            o.end()
            assert bytes(h.out) == data, name


def test_golden_errors():
    for name, c in _fx()["errors"].items():
        data = json.dumps(c["body"]).encode() if c["body"] is not None else b"<html>x</html>"
        assert tr.error_in(c["status"], data) == (c["want_status"], c["want_type"],
                                                  c["want_message"]), name
    doc = json.loads(tr.messages_error(529, "busy", request_id="req_1"))
    assert doc == {"type": "error", "error": {"type": "overloaded_error", "message": "busy"},
                   "request_id": "req_1"}


def test_our_thinking_blocks_are_known_again():
    sig = tr.signature("abc")
    assert tr.ours(sig) and not tr.ours("EqQBforeign") and not tr.ours(None)
    assert sig == tr.signature("abc") != tr.signature("abd")


# ------------------------------------------------------------------------- 3 end to end
class Scripted:
    """A minimal OpenAI-compatible engine whose answers a test writes: `queue` holds whole
    chat.completion dicts, or lists of chunk dicts (a stream), or {"quiet": s, then: ...}; the
    requests are kept (`seen`: path, headers, body)."""

    def __init__(self):
        self.queue: list = []
        self.seen: list = []
        self.tokens = None
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, st, obj):
                data = json.dumps(obj).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/v1/models"):
                    return self._json(200, {"object": "list", "data": [
                        {"id": "m-a", "object": "model", "owned_by": "x"}]})
                return self._json(404, {"error": {"message": "no"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                fake.seen.append({"path": self.path, "headers": dict(self.headers.items()),
                                  "body": body})
                if self.path == "/v1/tokenize":
                    if fake.tokens is None:
                        return self._json(404, {"error": {"message": "no route"}})
                    return self._json(200, {"tokens": fake.tokens(body)})
                item = fake.queue.pop(0)
                if isinstance(item, dict) and "quiet" in item:
                    time.sleep(item["quiet"])
                    item = item["then"]
                done = True
                if isinstance(item, dict) and "chunks" in item:
                    item, done = item["chunks"], item.get("done", True)
                if isinstance(item, dict):
                    return self._json(200, item)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.close_connection = True
                for c in item:
                    self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
                    self.wfile.flush()
                if done:
                    self.wfile.write(b"data: [DONE]\n\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def shutdown(self):
        self.srv.shutdown()


def _completion(content=None, finish="stop", calls=None, usage=None, **choice):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    ch = dict({"index": 0, "message": msg, "finish_reason": finish}, **choice)
    return {"id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": "m-a",
            "choices": [ch], "usage": usage or {"prompt_tokens": 9, "completion_tokens": 3,
                                                 "total_tokens": 12}}


def _env(port, kind="tandemllm", bid="eng", extra=None, keys=True):
    up = bid.upper()
    env = {"TANDEM_BACKENDS": bid, f"TANDEM_BACKEND_{up}_KIND": kind,
           f"TANDEM_BACKEND_{up}_URL": f"http://127.0.0.1:{port}/v1"}
    if keys:
        env["TANDEM_API_KEYS"] = f"alice:{KEY}"
    env.update(extra or {})
    return env


def _hdr(**kw):
    h = {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    h.update(kw)
    return {k: v for k, v in h.items() if v is not None}


def _msg(text="hi", **kw):
    b = {"model": "fake-a", "max_tokens": 200, "messages": [{"role": "user", "content": text}]}
    b.update(kw)
    return b


def test_a_message_to_a_local_engine():
    """Gherkin: a system prompt and a tool reach the engine as a system message and a function
    tool; the client gets content blocks, a stop reason and usage; the row says messages."""
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        st, h, data = px.post("/v1/messages", _msg("hello FAKE_TOOL", system="Be brief.",
                                                     tools=[TOOL]), headers=_hdr())
        assert st == 200 and h["Content-Type"] == "application/json", data
        assert h["request-id"].startswith("req_")
        m = json.loads(data)
        assert m["type"] == "message" and m["role"] == "assistant" and m["model"] == "fake-a"
        assert [b["type"] for b in m["content"]] == ["text", "tool_use"], m
        assert m["stop_reason"] == "tool_use" and m["content"][1]["input"] == {"path": "a.txt"}
        assert m["usage"]["input_tokens"] > 0 and m["usage"]["output_tokens"] > 0
        assert set(m["usage"]) >= {"cache_read_input_tokens", "cache_creation_input_tokens"}
        up = srv.fake.bodies[-1]
        assert up["messages"][0] == {"role": "system", "content": "Be brief."}
        assert up["tools"] == [{"type": "function", "function": {
            "name": "get_weather", "description": "Weather of a city",
            "parameters": TOOL["input_schema"]}}]
        r = px.rows()[-1]
        assert (r["endpoint"], r["user"], r["status"], r["finish_reason"]) == \
            ("messages", "alice", 200, "tool_calls"), r
        assert r["client_id"].startswith("k:"), "the key's hash names the client"
    srv.shutdown()


def test_a_streamed_tool_call():
    """Gherkin: the events in the documented order; the tool's input joins to valid JSON."""
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        st, h, data = px.post("/v1/messages", _msg("go FAKE_TOOL FAKE_REASONING", tools=[TOOL],
                                                     stream=True), headers=_hdr())
        assert st == 200 and h["Content-Type"] == "text/event-stream" and h["request-id"]
        m = strict(data)
        kinds = [b["type"] for b in m["content"]]
        assert kinds == ["thinking", "text", "tool_use", "tool_use"], kinds
        assert m["content"][0]["thinking"] and tr.ours(m["content"][0]["signature"])
        assert [b["input"] for b in m["content"][2:]] == [{"path": "a.txt"}, {"path": "a.txt"}]
        assert m["stop_reason"] == "tool_use" and m["usage"]["output_tokens"] > 0
        assert m["usage"]["input_tokens"] > 0, "message_delta carries the real counts"
        assert srv.fake.bodies[-1]["stream_options"] == {"include_usage": True}
        r = px.rows()[-1]
        assert r["endpoint"] == "messages" and r["stream"] == 1 and r["tool_calls"] == 2
    srv.shutdown()


def test_both_key_headers_and_neither_upstream():
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        for h in (_hdr(), _hdr(**{"x-api-key": None, "Authorization": f"Bearer {KEY}"})):
            h["anthropic-beta"] = "some-beta-2026-01-01"
            st, _, data = px.post("/v1/messages", _msg(), headers=h)
            assert st == 200, data
            sent = {k.lower(): v for k, v in srv.fake.last["headers"].items()}
            assert KEY not in json.dumps(srv.fake.last), "the client's key went upstream"
            assert not {"x-api-key", "authorization", "anthropic-version",
                        "anthropic-beta"} & set(sent), sent
        # SEC-28: in local mode (this machine, no password) a key no token has is as none:
        # served, and never sent upstream; through a proxy in front it is the gate's 401
        st, _, data = px.post("/v1/messages", _msg(), headers=_hdr(**{"x-api-key": "wrong" * 4}))
        assert st == 200, data
        assert "wrong" * 4 not in json.dumps(srv.fake.last)
        st, _, data = px.post("/v1/messages", _msg(), headers=_hdr(**{
            "x-api-key": "wrong" * 4, "X-Forwarded-For": "192.0.2.7"}))
        assert st == 401 and json.loads(data)["error"]["type"] == "authentication_error", data
    srv.shutdown()


def test_errors_keep_the_shape():
    """Gherkin: a missing key 401, an unknown model 404, a draining engine 529, each with a
    request-id; and the other refusals of ours."""
    srv, port = fake_backend.start(models=["fake-a"])
    # SEC-28: not listening yet, so no local mode: a request without a key is the gate's 401
    with Proxy(_env(port), local=False) as px:
        px.sync()

        def check(st, h, data, status, etype):
            doc = json.loads(data)
            assert st == status and doc["type"] == "error" and \
                doc["error"]["type"] == etype, (st, data)
            assert h["request-id"] == doc["request_id"] and doc["error"]["message"]
            return doc
        check(*px.post("/v1/messages", _msg(), headers={"anthropic-version": "2023-06-01"}),
              401, "authentication_error")
        check(*px.post("/v1/messages", dict(_msg(), model="nope"), headers=_hdr()), 404,
              "not_found_error")
        px.svc.drain_of = lambda bid: "the test node"
        for stream in (False, True):
            st, h, data = px.post("/v1/messages", _msg(stream=stream), headers=_hdr())
            check(st, h, data, 529, "overloaded_error")
            assert h["Retry-After"] == "30"
        px.svc.drain_of = lambda bid: None
        check(*px.post("/v1/messages", b"{not json", headers=_hdr()), 400,
              "invalid_request_error")
        check(*px.post("/v1/messages", [1], headers=_hdr()), 400, "invalid_request_error")
        check(*px.post("/v1/messages", {"model": "fake-a", "messages": []}, headers=_hdr()),
              400, "invalid_request_error")
        check(*px.post("/v1/messages/batches", {}, headers=_hdr()), 404, "not_found_error")
        rows = px.rows()
        # the ledger keeps the pipeline's own status (503 drained); the client saw 529. The
        # gate's 401 comes before the pipeline: no row for it
        assert [r["status"] for r in rows] == [404, 503, 503, 400, 400, 400], \
            [r["status"] for r in rows]
        assert {r["endpoint"] for r in rows} == {"messages"}
        assert rows[0]["error_type"] == "model_not_found"
    srv.shutdown()


def test_count_tokens():
    """Gherkin: exact with the engine's tokenize route, 404 and no number without one."""
    eng = Scripted()
    eng.tokens = lambda body: 7 * len(body["messages"]) + (5 if body.get("tools") else 0)
    with Proxy(_env(eng.port)) as px:
        px.sync()
        body = {"model": "m-a", "system": "sys", "tools": [TOOL],
                "messages": [{"role": "user", "content": "count me"}]}
        st, h, data = px.post("/v1/messages/count_tokens", body, headers=_hdr())
        assert st == 200 and json.loads(data) == {"input_tokens": 7 * 2 + 5}, data
        q = eng.seen[-1]
        assert q["path"] == "/v1/tokenize" and q["body"]["messages"][0]["role"] == "system"
        assert q["body"]["tools"][0]["type"] == "function" and h["request-id"]
        eng.tokens = None
        st, _, data = px.post("/v1/messages/count_tokens", body, headers=_hdr())
        doc = json.loads(data)
        assert st == 404 and doc["error"]["type"] == "not_found_error" and \
            "input_tokens" not in data.decode()
        assert px.rows()[-1]["endpoint"] == "count_tokens"
    eng.shutdown()
    # a cloud API counts nothing and is never asked
    srv, port = fake_backend.start(models=["m-a"])
    with Proxy(_env(port, kind="openai", bid="oai")) as px:
        px.sync()
        n0 = srv.fake.requests
        st, _, data = px.post("/v1/messages/count_tokens", body, headers=_hdr())
        assert st == 404 and srv.fake.requests == n0, data
    srv.shutdown()


def test_a_server_tool_and_other_refusals():
    """Gherkin: a server-side tool type is a 400 naming the type; nothing goes upstream."""
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        n0 = len(srv.fake.bodies)
        for extra, word in (({"tools": [{"type": "web_search_20260209", "name": "web_search"}]},
                             "web_search_20260209"),
                            ({"messages": [{"role": "user", "content": [{"type": "document",
                                                                          "source": {}}]}]},
                             "document"),
                            ({"mcp_servers": [{"type": "url", "url": "https://x.example",
                                               "name": "x"}]}, "mcp_servers")):
            st, h, data = px.post("/v1/messages", dict(_msg(), **extra), headers=_hdr())
            doc = json.loads(data)
            assert st == 400 and doc["error"]["type"] == "invalid_request_error" and \
                word in doc["error"]["message"], data
        assert len(srv.fake.bodies) == n0
        assert px.rows()[-1]["error_type"] == "untranslatable"
    srv.shutdown()


def test_images_and_cache_control_never_upstream():
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        body = _msg(cache_control={"type": "ephemeral"}, metadata={"user_id": "u-1"},
                    system=[{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": "iVBORw0KGgo="}},
                        {"type": "image", "source": {"type": "url",
                                                     "url": "https://img.example/a.png"}},
                        {"type": "text", "text": "what is it", "cache_control": {
                            "type": "ephemeral", "ttl": "1h"}}]}])
        for stream in (False, True):
            st, h, data = px.post("/v1/messages", dict(body, stream=stream), headers=_hdr())
            assert st == 200, data
            up = srv.fake.bodies[-1]
            assert "cache_control" not in json.dumps(up) and "u-1" not in json.dumps(up)
            assert [p["type"] for p in up["messages"][1]["content"]] == \
                ["image_url", "image_url", "text"]
            assert up["messages"][1]["content"][0]["image_url"]["url"] == \
                "data:image/png;base64,iVBORw0KGgo="
            assert h["X-Tandem-Dropped"] == "cache_control,metadata", h
            (json.loads(data) if not stream else strict(data))
    srv.shutdown()


def test_a_tool_loop_of_three_turns_stop_reasons_and_usage():
    eng = Scripted()
    call = lambda i: [{"id": f"call_{i}", "type": "function", "function": {  # noqa: E731
        "name": "get_weather", "arguments": json.dumps({"city": f"C{i}"})}}]
    eng.queue = [_completion(None, "tool_calls", call(1)), _completion(None, "tool_calls", call(2)),
                 _completion("done after 2", "stop", usage={
                     "prompt_tokens": 120, "completion_tokens": 4, "total_tokens": 124,
                     "prompt_tokens_details": {"cached_tokens": 100}})]
    with Proxy(_env(eng.port)) as px:
        px.sync()
        msgs = [{"role": "user", "content": "weather twice"}]
        for turn in range(3):
            st, _, data = px.post("/v1/messages", {"model": "m-a", "max_tokens": 100,
                                                   "messages": msgs, "tools": [TOOL]},
                                  headers=_hdr())
            m = json.loads(data)
            assert st == 200, data
            if turn < 2:
                assert m["stop_reason"] == "tool_use"
                tu = m["content"][-1]
                assert tu["type"] == "tool_use" and tu["input"] == {"city": f"C{turn + 1}"}
                msgs = msgs + [{"role": "assistant", "content": m["content"]},
                               {"role": "user", "content": [{"type": "tool_result",
                                                             "tool_use_id": tu["id"],
                                                             "content": f"{turn + 1} C"}]}]
            else:
                assert m["stop_reason"] == "end_turn" and m["content"][0]["text"] == "done after 2"
                assert m["usage"] == {"input_tokens": 20, "output_tokens": 4,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 100}
        sent = eng.seen[-1]["body"]["messages"]
        assert [x["role"] for x in sent] == ["user", "assistant", "tool", "assistant", "tool"]
        assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"city":"C1"}'
        assert sent[4] == {"role": "tool", "tool_call_id": "call_2", "content": "2 C"}
        # the other stop reasons, whole and streamed
        for finish, extra, want, seq in (("length", {}, "max_tokens", None),
                                         ("content_filter", {}, "refusal", None),
                                         ("stop", {"stop_reason": "END"}, "stop_sequence", "END")):
            eng.queue.append(_completion("x", finish, **extra))
            eng.queue.append([{"choices": [dict({"index": 0, "delta": {"content": "x"},
                                                 "finish_reason": finish}, **extra)]}])
            for stream in (False, True):
                st, _, data = px.post("/v1/messages", {"model": "m-a", "max_tokens": 9,
                                                       "stop_sequences": ["END"],
                                                       "stream": stream,
                                                       "messages": msgs[:1]}, headers=_hdr())
                m = strict(data) if stream else json.loads(data)
                assert (m["stop_reason"], m["stop_sequence"]) == (want, seq), (finish, stream)
        assert eng.seen[-1]["body"]["stop"] == ["END"]
    eng.shutdown()


def test_a_silent_prefill_keeps_the_client_through_ping():
    eng = Scripted()
    eng.queue = [{"quiet": 1.2, "then": [{"choices": [{"index": 0, "delta": {"content": "late"},
                                                        "finish_reason": "stop"}]}]}]
    old = messages_mod.PING_S
    messages_mod.PING_S = 0.3
    try:
        with Proxy(_env(eng.port)) as px:
            px.sync()
            t0 = time.time()
            st, h, data = px.post("/v1/messages", {"model": "m-a", "max_tokens": 9,
                                                   "stream": True, "messages": [
                                                       {"role": "user", "content": "x"}]},
                                  headers=_hdr())
            assert st == 200 and time.time() - t0 >= 1.2
            names = [n for n, _ in parse(data)]
            assert names[0] == "message_start" and names.count("ping") >= 2, names
            assert names.index("ping") < names.index("content_block_start")
            assert strict(data)["content"] == [{"type": "text", "text": "late"}]
            # the head went out early, then the backend refused: an error event in place
            px.svc.drain_of = lambda bid: None
            eng.queue = [{"quiet": 1.0, "then": {"error": "x"}}]
            st, _, data = px.post("/v1/messages", {"model": "m-a", "max_tokens": 9,
                                                   "stream": True, "messages": [
                                                       {"role": "user", "content": "x"}]},
                                  headers=_hdr())
            got = strict(data)
            assert st == 200 and got["error"]["type"] == "api_error", data
    finally:
        messages_mod.PING_S = old
    eng.shutdown()


def test_a_client_that_leaves_closes_the_upstream():
    srv, port = fake_backend.start(models=["fake-a"], tps=20)
    with Proxy(_env(port, keys=False)) as px:
        px.sync()
        s = socket.create_connection(("127.0.0.1", px.port))
        body = json.dumps(_msg("go FAKE_LONG 400", stream=True)).encode()
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        s.recv(4096)
        time.sleep(0.3)
        s.close()
        t0 = time.time()
        while srv.fake.closed_early == 0 and time.time() - t0 < 3:
            time.sleep(0.05)
        assert srv.fake.closed_early == 1 and time.time() - t0 < 1.5, time.time() - t0
        time.sleep(0.3)
        assert px.rows()[-1]["finish_reason"] == "abandoned"
    srv.shutdown()


def test_a_model_on_a_messages_api_backend_is_translated_twice():
    """A route with a backend of another kind in it (here a generic fallback) is translated,
    twice on the Messages API backend; a route of Messages API backends only passes through
    (SRV-881, tests/test_proxy_passthrough.py)."""
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    srv.fake.count = 42
    gen, gport = fake_backend.start(models=["model-a"])
    env = {"TANDEM_BACKENDS": "anth,gen", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
           "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}",
           "TANDEM_BACKEND_ANTH_KEY": BACKEND_KEY, "TANDEM_API_KEYS": f"alice:{KEY}",
           "TANDEM_BACKEND_ANTH_FALLBACK": "gen", "TANDEM_BACKEND_GEN_KIND": "generic",
           "TANDEM_BACKEND_GEN_URL": f"http://127.0.0.1:{gport}/v1",
           "TANDEM_ROUTE_1": "model-a=anth"}
    with Proxy(env) as px:
        px.sync()
        for stream in (False, True):
            st, h, data = px.post("/v1/messages", {
                "model": "model-a", "max_tokens": 300, "stream": stream, "tools": [TOOL],
                "temperature": 0.5, "messages": [{"role": "user", "content": "weather?"}]},
                headers=_hdr())
            assert st == 200, data
            m = strict(data) if stream else json.loads(data)
            assert m["stop_reason"] == "tool_use" and m["content"][-1]["name"] == "get_weather"
            up = srv.fake.last
            assert up["headers"].get("x-api-key") == BACKEND_KEY and KEY not in json.dumps(up)
            assert up["body"]["max_tokens"] == 300 and "temperature" not in up["body"]
            assert h["X-Tandem-Dropped"] == "temperature", h
        st, _, data = px.post("/v1/messages/count_tokens", {
            "model": "model-a", "messages": [{"role": "user", "content": "x"}]}, headers=_hdr())
        assert st == 200 and json.loads(data) == {"input_tokens": 42}
        assert srv.fake.last["path"] == "/v1/messages/count_tokens" and \
            "max_tokens" not in srv.fake.last["body"]
        srv.fake.count = None
        st, _, data = px.post("/v1/messages/count_tokens", {
            "model": "model-a", "messages": [{"role": "user", "content": "x"}]}, headers=_hdr())
        assert st == 404 and json.loads(data)["error"]["type"] == "not_found_error"
    srv.shutdown()
    gen.shutdown()


def test_a_whole_answer_to_a_stream_request_and_a_late_error():
    """`Out` on its own: a backend that answers a stream request whole still gives a stream; a
    refusal after the ping thread started the head is an error event."""
    H = _H

    def feed(o, status, obj):
        data = json.dumps(obj).encode()
        o.send_response(status)
        o.send_header("Content-Type", "application/json")
        o.send_header("Content-Length", str(len(data)))
        o.end_headers()
        o.wfile.write(data)
        o.end()

    meta = {"model": "m-a", "stop": [], "display": "summarized"}
    h = H()
    o = messages_mod.Out(h, {}, True, "msg_t", meta, "req_t", [])
    feed(o, 200, _completion("whole"))
    assert h.status == 200 and strict(bytes(h.out))["content"] == [{"type": "text",
                                                                   "text": "whole"}]
    old = messages_mod.PING_S
    messages_mod.PING_S = 0.05
    try:
        h = H()
        o = messages_mod.Out(h, {}, True, "msg_t", meta, "req_t", [])
        time.sleep(0.4)
        feed(o, 503, {"error": {"message": "full", "type": "server_busy"}})
    finally:
        messages_mod.PING_S = old
    got = strict(bytes(h.out))
    assert h.status == 200 and got["error"] == {"type": "overloaded_error", "message": "full"}


def test_a_stream_that_ends_without_done_ends_whole():
    """A finish chunk and then the close (no `[DONE]`): the relay reads it as whole, and so does
    the client: message_delta and message_stop, no error."""
    eng = Scripted()
    eng.queue = [{"chunks": [{"choices": [{"index": 0, "delta": {"content": "all"},
                                           "finish_reason": "stop"}]}], "done": False}]
    with Proxy(_env(eng.port)) as px:
        px.sync()
        st, _, data = px.post("/v1/messages", {"model": "m-a", "max_tokens": 9, "stream": True,
                                               "messages": [{"role": "user", "content": "x"}]},
                              headers=_hdr())
        m = strict(data)
        assert st == 200 and "error" not in m and m["stop_reason"] == "end_turn", data
        assert m["content"] == [{"type": "text", "text": "all"}]
    eng.shutdown()


def test_thinking_reaches_the_engine_as_its_own_switch():
    srv, port = fake_backend.start(models=["fake-a"])
    with Proxy(_env(port)) as px:
        px.sync()
        st, h, _ = px.post("/v1/messages", _msg(thinking={"type": "disabled"}), headers=_hdr())
        assert st == 200 and \
            srv.fake.bodies[-1]["chat_template_kwargs"] == {"enable_thinking": False}
        st, h, _ = px.post("/v1/messages", _msg(thinking={"type": "adaptive"},
                                                output_config={"effort": "high"}),
                           headers=_hdr())
        assert st == 200 and "chat_template_kwargs" not in srv.fake.bodies[-1]
        st, h, _ = px.post("/v1/messages", _msg(thinking={"type": "enabled",
                                                          "budget_tokens": 2048}),
                           headers=_hdr())
        assert h["X-Tandem-Dropped"] == "thinking.budget_tokens"
    srv.shutdown()


def test_hooks_see_the_chat_shape_and_a_deny_keeps_the_messages_shape():
    from hooks_util import hook_server
    srv, port = fake_backend.start(models=["fake-a"])
    hs, hport = hook_server()
    hs.respond = lambda env: {"action": "modify", "patch": {"temperature": 0.01}}
    try:
        with Proxy(_env(port)) as px:
            px.sync()

            def hook(doc):
                st, _, out = px.post("/v1/fleet/hooks", doc)
                assert st == 201, out
            hook({"name": "cool", "endpoint": f"http://127.0.0.1:{hport}/", "role": "transform",
                  "stages": ["request.before_engine"], "priority": 1})
            st, _, data = px.post("/v1/messages", _msg("hi", system="S", temperature=0.9),
                                  headers=_hdr())
            assert st == 200 and srv.fake.last["body"]["temperature"] == 0.01, data
            env = hs.calls[-1]["env"]
            assert env["stage"] == "request.before_engine" and \
                env["event"]["sizes"]["chars"]["system"] == 1, env
            hook({"name": "stop", "kind": "builtin", "target": "deny", "role": "guard",
                  "stages": ["request.before_engine"], "priority": 100,
                  "options": {"reason": "not today"}})
            for stream in (False, True):
                st, h, data = px.post("/v1/messages", _msg(stream=stream), headers=_hdr())
                doc = json.loads(data)
                assert st == 403 and doc["error"] == {"type": "permission_error",
                                                      "message": "not today"}, data
                assert h["X-Tandem-Hook"] == "stop" and h["request-id"] == doc["request_id"]
    finally:
        srv.shutdown()
        hs.shutdown()


# ------------------------------------------------------------------------- 4 the harness guard
def test_the_harness_is_refused_and_held_off_on_a_messages_api_backend():
    from proxy import config as config_mod
    from proxy import harness as harness_mod
    from proxy import memwriter
    srv, port = fake_anthropic.start(key=BACKEND_KEY)
    base = {"TANDEM_BACKENDS": "anth", "TANDEM_BACKEND_ANTH_KIND": "anthropic",
            "TANDEM_BACKEND_ANTH_URL": f"http://127.0.0.1:{port}",
            "TANDEM_BACKEND_ANTH_KEY": BACKEND_KEY}
    # a variable that switches it on stops the start, naming the variable
    for var, val in (("TANDEM_BACKEND_ANTH_HARNESS", "on"),
                     ("TANDEM_BACKEND_ANTH_WRITER", "on")):
        try:
            config_mod.parse_env(dict(base, **{var: val}))
        except config_mod.ConfigError as exc:
            assert var in str(exc) and "kind anthropic" in str(exc), exc
        else:
            raise AssertionError(var)
    with Proxy(base) as px:
        px.sync()
        st, _, data = px.post("/v1/config/backends", {
            "id": "a2", "kind": "anthropic", "url": f"http://127.0.0.1:{port}",
            "harness": {"mode": "on"}})
        assert st == 422 and b"harness.mode" in data, data
        st, _, data = px.request("PATCH", "/v1/config/backends/anth", {"harness": {"writer": "on"}})
        assert st == 422 and b"harness.writer" in data, data
        st, _, _ = px.request("PATCH", "/v1/config/backends/anth", {"harness": {"writer": "auto"}})
        assert st == 200
        # a database written before the rule: held off at run time, and said on the health page
        px.svc.cfg._conn.execute("UPDATE backends SET harness = ? WHERE id = 'anth'",
                                 (json.dumps({"mode": "on", "writer": "on"}),))
        px.svc.cfg._conn.commit()
        px.svc.cfg._bump()
        b = px.svc.cfg.backend("anth")
        assert b["harness"]["mode"] == "on"
        assert harness_mod.switches(b, {"features": {"mode": "on"}})["mode"] == "off"
        assert memwriter.resolve(b, None, "on") == ("off", "kind")
        assert not harness_mod.runs_on(b) and harness_mod.runs_on({"kind": "vllm"})
        got = harness_mod.conflicts(px.svc)
        assert [n for n, _ in got] == ["anth", "anth"], got
        st, _, data = px.get("/v1/fleet/health/controller?fresh=1")
        doc = json.loads(data)
        fs = [f for f in doc["findings"] if f["code"] == "harness_unsupported"]
        assert st == 200 and len(fs) == 1 and fs[0]["facts"]["backend"] == "anth", doc
        assert fs[0]["severity"] == "warning" and "mode is on" in fs[0]["detail"]
        # the request still goes, without the harness
        n0 = len(srv.fake.requests)
        st, _, _ = px.post("/v1/chat/completions", {"model": "model-a", "messages": [
            {"role": "user", "content": "x"}]})
        assert st == 200 and len(srv.fake.requests) == n0 + 1
    srv.shutdown()


if __name__ == "__main__":
    run(globals())
