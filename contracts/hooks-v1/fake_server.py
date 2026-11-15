"""A fake hooks-v1 receiver: the standard library only, for tests and to try a hook by hand.

    python contracts/hooks-v1/fake_server.py --port 9300 --secret hks_... \\
        [--set temperature=0.1] [--deny-model 'gpt-*'] [--delay-ms 0]

or in a test:

    srv, port = fake_server.start(secrets=["hks_..."])
    srv.respond = lambda env: {"action": "continue"}     # the answer per call (a dict, None for
                                                         # 204, or (status, dict) for an error)
    srv.delay_s = 0.2                                    # wait before answering
    srv.calls                                            # [{headers, body, env, verdict}]
    srv.shutdown()

Every call is checked as a receiver must (README, Signing): the HMAC over "<timestamp>." + body
with any of `secrets`, the timestamp within the replay window, the body's `id` not seen before.
A call that fails answers 401 {"error": why} and is kept with its verdict. With no secret the
check is skipped (verdict `unchecked`).

By default the server answers `continue`; --set k=v answers `modify` with that patch
(JSON values), --deny-model answers `deny` for a model id matching the glob.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WINDOW_S = 300


def signature(secret: str, ts: int, body: bytes) -> str:
    return hmac.new(secret.encode(), str(int(ts)).encode() + b"." + body,
                    hashlib.sha256).hexdigest()


class Check:
    """The receiver's rule: (True, "ok") or (False, why)."""

    def __init__(self, secrets, window_s: int = WINDOW_S, clock=time.time):
        self.secrets, self.window_s, self.clock = list(secrets), window_s, clock
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def __call__(self, headers, body: bytes) -> tuple[bool, str]:
        sig = headers.get("X-Tandem-Signature")
        ts = headers.get("X-Tandem-Timestamp")
        if not sig or not ts:
            return False, "no_signature"
        try:
            t = int(ts)
        except ValueError:
            return False, "bad_timestamp"
        now = self.clock()
        if abs(now - t) > self.window_s:
            return False, "stale"
        got = [p.strip()[3:] for p in sig.split(",") if p.strip().startswith("v1=")]
        if not any(hmac.compare_digest(signature(s, t, body), g)
                   for s in self.secrets for g in got):
            return False, "bad_signature"
        try:
            did = json.loads(body)["id"]
        except (ValueError, KeyError, TypeError):
            return False, "bad_body"
        with self.lock:
            for k in [k for k, v in self.seen.items() if now - v > self.window_s]:
                del self.seen[k]
            if did in self.seen:
                return False, "replayed"
            self.seen[did] = now
        return True, "ok"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, doc=None) -> None:
        raw = b"" if doc is None else json.dumps(doc).encode()
        self.send_response(code)
        if doc is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        srv = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ok, why = srv.check(self.headers, body) if srv.check is not None else (True, "unchecked")
        try:
            env = json.loads(body)
        except ValueError:
            env = None
        rec = {"headers": dict(self.headers.items()), "body": body, "env": env, "verdict": why,
               "path": self.path}
        with srv.lock:
            srv.calls.append(rec)
        if not ok:
            return self._send(401, {"error": why})
        if srv.delay_s:
            time.sleep(srv.delay_s)
        got = srv.respond(env) if srv.respond is not None else {"action": "continue"}
        if isinstance(got, tuple):
            return self._send(got[0], got[1])
        if got is None:
            return self._send(204)
        if isinstance(got, (bytes, str)):
            raw = got.encode() if isinstance(got, str) else got
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self._send(200, got)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, secrets=(), window_s: int = WINDOW_S, clock=time.time):
        super().__init__(addr, Handler)
        self.check = Check(secrets, window_s, clock) if secrets else None
        self.respond = None
        self.delay_s = 0.0
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return                       # the caller gave up waiting (a timeout under test)
        super().handle_error(request, client_address)

    def set_secrets(self, secrets) -> None:
        self.check = Check(secrets, self.check.window_s if self.check else WINDOW_S)


def start(secrets=(), port: int = 0, host: str = "127.0.0.1", **kw):
    srv = Server((host, port), secrets, **kw)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv, srv.server_address[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9300)
    ap.add_argument("--secret", action="append", default=[])
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=JSON")
    ap.add_argument("--deny-model", default=None, metavar="GLOB")
    ap.add_argument("--delay-ms", type=int, default=0)
    a = ap.parse_args()
    patch = {}
    for kv in a.set:
        k, _, v = kv.partition("=")
        try:
            patch[k] = json.loads(v)
        except ValueError:
            patch[k] = v

    def respond(env):
        model = ((env or {}).get("request") or {}).get("model") or ""
        if a.deny_model and fnmatch.fnmatchcase(model, a.deny_model):
            return {"action": "deny", "reason": f"model {model} is not allowed here"}
        if patch:
            return {"action": "modify", "patch": patch}
        return {"action": "continue"}
    srv, port = start(a.secret, a.port, a.host)
    srv.respond = respond
    srv.delay_s = a.delay_ms / 1000.0
    print(f"fake hook server on http://{a.host}:{port}/ "
          f"({'signatures checked' if a.secret else 'no secret: signatures not checked'})",
          flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
