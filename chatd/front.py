"""`python -m chatd.front`: the dashboard and the Chat tab in front of an engine that has no forward.

    python -m chatd.front --port 4180 --dashboard dashboard/dist \\
        --engine http://127.0.0.1:8000 [--chatd http://127.0.0.1:8016]

The engine server forwards `/v1/chat-app/*` itself (server/chat_forward.py). An engine that runs
another build -- an older release, a branch without the forward, vLLM -- does not, and this front
stands in for it, so the Chat tab can be tried without restarting the engine:

    /dashboard/*        the built dashboard (server/static.py, the engine's own file server)
    /v1/chat-app/*      to chatd with the forward key, as the engine server would (no password:
                        the front signs everyone in as the owner)
    everything else     to the engine unchanged (its own auth applies there)

OPS-300: it is for this machine only. It signs every caller in, so it binds to a loopback address
and refuses any other `--host`. To reach the Chat tab from another device, run TandemApp.

It holds nothing and computes nothing; stopping it stops nothing in chatd.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatd.__main__ import load_key, log
from tandemapp import chat_forward, static
from tandemapp.auth import LOOPBACK


class FrontHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    dist = ""
    engine: tuple[str, int] = ("127.0.0.1", 8000)
    chatd: tuple[str, int] = ("127.0.0.1", 8016)
    key = ""
    _status = None

    def log_message(self, fmt, *args):
        pass

    def _go(self, method: str) -> None:
        path = self.path.split("?")[0]
        if method == "GET" and (path == "/" or path == "/dashboard" or
                                path.startswith("/dashboard/")):
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return static.serve(self, self.path, self.dist)
        if chat_forward.wanted(path):
            return chat_forward.forward(self, method, [
                ("X-Tandem-Forward", self.key), ("X-Tandem-Principal", "owner"),
                ("X-Tandem-Password-Set", "0"), ("X-Tandem-Signed-In", "1")], self.chatd)
        return chat_forward.forward(self, method, [], self.engine)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_PATCH(self):
        self._go("PATCH")

    def do_DELETE(self):
        self._go("DELETE")

    def do_PUT(self):
        self._go("PUT")


def refused_host(host: str) -> str | None:
    """OPS-300: why `host` is refused for the front, or None for a loopback address."""
    if host in LOOPBACK or host == "localhost":
        return None
    return (f"--host {host or '(all addresses)'} refused: this front signs every caller in as "
            "the owner, so it is for this machine only (127.0.0.1 or ::1). To reach the Chat tab "
            "from another device, run TandemApp.")


def _addr(url: str) -> tuple[str, int]:
    u = urllib.parse.urlsplit(url)
    return u.hostname or "127.0.0.1", u.port or 80


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m chatd.front")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4180)
    ap.add_argument("--dashboard", required=True, help="the built dashboard (dashboard/dist)")
    ap.add_argument("--engine", default="http://127.0.0.1:8000")
    ap.add_argument("--chatd", default="http://127.0.0.1:8016")
    ap.add_argument("--data", default=os.environ.get("QSE_CHAT_DATA") or "~/.tandemllm/chat",
                    help="chatd's data folder (its forward.key)")
    a = ap.parse_args(argv)
    bad = refused_host(a.host)
    if bad:
        print(f"python -m chatd.front: {bad}", file=sys.stderr)
        return 2
    handler = type("Front", (FrontHandler,), {
        "dist": os.path.abspath(a.dashboard), "engine": _addr(a.engine),
        "chatd": _addr(a.chatd), "key": load_key(os.path.expanduser(a.data))})
    srv = ThreadingHTTPServer((a.host, a.port), handler)
    srv.daemon_threads = True
    log(f"[front] dashboard on http://{a.host}:{a.port}/dashboard/#/chat, chat to {a.chatd}, "
        f"the rest to {a.engine}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
