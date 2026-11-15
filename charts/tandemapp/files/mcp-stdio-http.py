#!/usr/bin/env python3
"""Serve a stdio MCP server as streamable HTTP on loopback (JSON answers, no SSE).

    stdio_http.py --port 8031 -- some-mcp-server --stdio

One child process, shared by every client. Request ids are remapped so two clients cannot
collide. The child is started on the first request and again after it exits; a restart replays
the first initialize so a client mid-session does not notice, and a later initialize is answered
from the first one's result. Server-to-client requests (ping,
roots/list) are answered here; the child's notifications are dropped.
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CALL_TIMEOUT_S = 600.0


class Child:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.ids = itertools.count(1)
        self.waiting: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.starting = threading.Lock()
        self.init_params: dict | None = None
        self.init_answer: dict | None = None

    def _write(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self.lock:
                    w = self.waiting.get(msg["id"])
                if w is not None:
                    w["msg"] = msg
                    w["event"].set()
            elif msg.get("method") and "id" in msg:
                result = {"roots": []} if msg["method"] == "roots/list" else {}
                with self.lock:
                    if self.proc is proc:
                        self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        with self.lock:
            for w in self.waiting.values():
                if w["proc"] is proc:
                    w["event"].set()

    def _start(self) -> None:
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=sys.stderr)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _ensure(self, replay: bool) -> None:
        with self.starting:                      # a call waits until a restart's replay is done
            with self.lock:
                if self.proc is not None and self.proc.poll() is None:
                    return
                self._start()
            if replay and self.init_params is not None:
                self._ask("initialize", self.init_params, 60.0)
                with self.lock:
                    self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _ask(self, method: str, params, timeout: float) -> dict:
        rid = next(self.ids)
        with self.lock:
            w = {"event": threading.Event(), "msg": None, "proc": self.proc}
            self.waiting[rid] = w
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            self._write(msg)
        try:
            w["event"].wait(timeout)
            if w["msg"] is None:
                raise RuntimeError("no answer from the stdio server")
            return w["msg"]
        finally:
            with self.lock:
                self.waiting.pop(rid, None)

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        if "id" not in msg:                                  # a notification
            self._ensure(replay=True)
            with self.lock:
                self._write(msg)
            return None
        try:
            if method == "initialize" and self.init_answer is not None:
                ans = self.init_answer
            elif method == "initialize":
                self.init_params = msg.get("params") or {}
                self._ensure(replay=False)
                ans = self._ask(method, self.init_params, 60.0)
                if "result" in ans:
                    self.init_answer = ans
            else:
                self._ensure(replay=True)
                ans = self._ask(method, msg.get("params"), CALL_TIMEOUT_S)
        except (RuntimeError, OSError) as exc:
            return {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32000, "message": str(exc)}}
        out = {k: v for k, v in ans.items() if k in ("result", "error")}
        return {"jsonrpc": "2.0", "id": msg["id"], **out}


def make_handler(child: Child, path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(405, b"", "text/plain")

        def do_DELETE(self):
            self._send(204)

        def do_POST(self):
            if self.path.split("?")[0] != path:
                return self._send(404, b"", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError:
                return self._send(400, b'{"error":"bad json"}')
            msgs = body if isinstance(body, list) else [body]
            answers = [a for a in (child.handle(m) for m in msgs) if a is not None]
            if not answers:
                return self._send(202)
            out = answers if isinstance(body, list) else answers[0]
            self._send(200, json.dumps(out).encode())

    return Handler


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--path", default="/mcp")
    p.add_argument("argv", nargs="+")
    a = p.parse_args()
    child = Child(a.argv)
    ThreadingHTTPServer((a.host, a.port), make_handler(child, a.path)).serve_forever()


if __name__ == "__main__":
    main()
