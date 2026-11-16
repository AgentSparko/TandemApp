"""A fake Anthropic-compatible Messages server for the proxy's tests: the standard library, no
model (SRV-877).

    srv, port = fake_anthropic.start(key="sk-fixture-...")    srv.fake is the state
    srv.shutdown()

`POST /v1/messages` checks the key (`x-api-key`, or `Authorization: Bearer` with
`auth="bearer"`) and that `anthropic-version` and `max_tokens` are there (else the API's own 401
and 400), keeps every request (`fake.requests`: path, headers, body), and answers:

    fake.queue        answers to give first, in order: {"status", "body"} (a JSON error or any
                      body), {"message": {...}} (a whole message, streamed when asked), or
                      {"message": {...}, "error_after": n} (the stream breaks after n text
                      deltas with an `error` event)
    otherwise         with tools offered and fewer than `fake.tool_turns` tool results in the
                      conversation: one `tool_use` call (`get_weather`, id toolu_fake_<n>);
                      else the text "done after <n> results". Usage: `fake.usage` (input and
                      cache counts) and 5 output tokens.

A stream is sent as the API sends it: `message_start`, a `ping`, each block's start, deltas (text
in two pieces, tool input JSON in two `input_json_delta` pieces) and stop, `message_delta` with
the stop reason and the output count, `message_stop`; chunked.

`GET /v1/models` lists `fake.models` in pages of 2 (`has_more`, `first_id`, `last_id`,
`after_id`), with `display_name`, `max_input_tokens`, `max_tokens` and `capabilities`.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION_HEADER = "anthropic-version"


class Fake:
    def __init__(self, key: str | None = None, auth: str = "x-api-key", models=None,
                 page: int = 2):
        self.key, self.auth = key, auth
        self.models = list(models or ["model-a", "model-b", "model-c", "model-d", "model-e"])
        self.page = page
        self.queue: list[dict] = []
        self.requests: list[dict] = []
        self.model_pages = 0
        self.tool_turns = 2
        self.usage = {"input_tokens": 10, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}
        self.lock = threading.Lock()

    @property
    def last(self) -> dict:
        return self.requests[-1]

    def default_message(self, body: dict) -> dict:
        results = sum(1 for m in body.get("messages") or [] if isinstance(m.get("content"), list)
                      for b in m["content"] if isinstance(b, dict)
                      and b.get("type") == "tool_result")
        usage = dict(self.usage, output_tokens=5)
        if body.get("tools") and results < self.tool_turns:
            n = results + 1
            return {"content": [{"type": "text", "text": f"Call {n}."},
                                {"type": "tool_use", "id": f"toolu_fake_{n}",
                                 "name": "get_weather", "input": {"city": f"City {n}"}}],
                    "stop_reason": "tool_use", "usage": usage}
        return {"content": [{"type": "text", "text": f"done after {results} results"}],
                "stop_reason": "end_turn", "usage": usage}


def _message(m: dict, model: str, n: int) -> dict:
    out = {"id": f"msg_fake_{n}", "type": "message", "role": "assistant", "model": model,
           "content": [], "stop_reason": None, "stop_sequence": None}
    out.update(copy.deepcopy(m))
    return out


def _events(msg: dict, error_after=None):
    usage = dict(msg.get("usage") or {})
    start = dict(msg, content=[], stop_reason=None,
                 usage={k: v for k, v in usage.items() if k != "output_tokens"} | {
                     "output_tokens": 1})
    yield "message_start", {"type": "message_start", "message": start}
    yield "ping", {"type": "ping"}
    sent = 0
    for i, b in enumerate(msg.get("content") or []):
        if b["type"] == "text":
            yield "content_block_start", {"type": "content_block_start", "index": i,
                                          "content_block": {"type": "text", "text": ""}}
            t = b["text"]
            for piece in (t[:len(t) // 2], t[len(t) // 2:]):
                if error_after is not None and sent >= error_after:
                    yield "error", {"type": "error", "error": {"type": "overloaded_error",
                                                                "message": "Overloaded"}}
                    return
                yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                              "delta": {"type": "text_delta", "text": piece}}
                sent += 1
        elif b["type"] == "thinking":
            yield "content_block_start", {"type": "content_block_start", "index": i,
                                          "content_block": {"type": "thinking", "thinking": "",
                                                            "signature": ""}}
            yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                          "delta": {"type": "thinking_delta",
                                                    "thinking": b["thinking"]}}
            yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                          "delta": {"type": "signature_delta",
                                                    "signature": "sig-fake"}}
        elif b["type"] == "tool_use":
            yield "content_block_start", {"type": "content_block_start", "index": i,
                                          "content_block": {"type": "tool_use", "id": b["id"],
                                                            "name": b["name"], "input": {}}}
            j = json.dumps(b["input"])
            for piece in (j[:len(j) // 2], j[len(j) // 2:]):
                yield "content_block_delta", {"type": "content_block_delta", "index": i,
                                              "delta": {"type": "input_json_delta",
                                                        "partial_json": piece}}
        yield "content_block_stop", {"type": "content_block_stop", "index": i}
    if error_after is not None:
        yield "error", {"type": "error", "error": {"type": "overloaded_error",
                                                    "message": "Overloaded"}}
        return
    yield "message_delta", {"type": "message_delta",
                            "delta": {"stop_reason": msg.get("stop_reason"),
                                      "stop_sequence": None},
                            "usage": {"output_tokens": usage.get("output_tokens", 0)}}
    yield "message_stop", {"type": "message_stop"}


def _model(mid: str, i: int) -> dict:
    return {"type": "model", "id": mid, "display_name": f"Model {mid[-1].upper()}",
            "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 200000 + i,
            "max_tokens": 32000,
            "capabilities": {"image_input": {"supported": i % 2 == 0},
                             "effort": {"supported": True, "low": {"supported": True},
                                        "medium": {"supported": True},
                                        "high": {"supported": True},
                                        "xhigh": {"supported": False},
                                        "max": {"supported": i == 0}}}}


def make_handler(fake: Fake):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, status: int, obj, extra=()):
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("request-id", "req_fake_1")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, etype: str, msg: str, extra=()):
            self._json(status, {"type": "error", "error": {"type": etype, "message": msg},
                                "request_id": "req_fake_1"}, extra)

        def _authorized(self) -> bool:
            if fake.key is None:
                return True
            if fake.auth == "bearer":
                return self.headers.get("Authorization") == f"Bearer {fake.key}"
            return self.headers.get("x-api-key") == fake.key

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path != "/v1/models":
                return self._error(404, "not_found_error", "no such route")
            if not self._authorized():
                return self._error(401, "authentication_error", "invalid x-api-key")
            q = parse_qs(u.query)
            ids = fake.models
            start = 0
            if q.get("after_id"):
                start = ids.index(q["after_id"][0]) + 1
            page = ids[start:start + fake.page]
            with fake.lock:
                fake.model_pages += 1
                fake.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                      "body": None})
            self._json(200, {"data": [_model(m, ids.index(m)) for m in page],
                             "has_more": start + fake.page < len(ids),
                             "first_id": page[0] if page else None,
                             "last_id": page[-1] if page else None})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if urlsplit(self.path).path != "/v1/messages":
                return self._error(404, "not_found_error", "no such route")
            try:
                body = json.loads(raw)
            except ValueError:
                return self._error(400, "invalid_request_error", "the body is not JSON")
            with fake.lock:
                fake.requests.append({"path": self.path, "headers": dict(self.headers.items()),
                                      "body": body})
                item = fake.queue.pop(0) if fake.queue else None
                count = len(fake.requests)
            if not self._authorized():
                return self._error(401, "authentication_error", "invalid x-api-key")
            if not self.headers.get(VERSION_HEADER):
                return self._error(400, "invalid_request_error",
                                   "anthropic-version: header is required")
            if not isinstance(body.get("max_tokens"), int):
                return self._error(400, "invalid_request_error", "max_tokens: Field required")
            if item is not None and "status" in item:
                extra = [("retry-after", "0")] if item["status"] in (429, 529) else []
                return self._json(item["status"], item.get("body") or {}, extra)
            m = item["message"] if item is not None else fake.default_message(body)
            msg = _message(m, body.get("model"), count)
            if not body.get("stream"):
                return self._json(200, msg)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("request-id", "req_fake_1")
            self.end_headers()
            for name, obj in _events(msg, (item or {}).get("error_after")):
                data = f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return H


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start(port: int = 0, host: str = "127.0.0.1", **kw):
    """(server, port) running in a daemon thread; `server.fake` is the state."""
    fake = Fake(**kw)
    srv = Server((host, port), make_handler(fake))
    srv.fake = fake
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
    return srv, srv.server_address[1]
