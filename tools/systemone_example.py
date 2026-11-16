"""An example decision provider on the systemone wire (SRV-851), standard library only.

    python tools/systemone_example.py --port 8720 [--key SECRET] [--model my-decider]

Point a `systemone-http` decision provider at http://<host>:8720 (docs/decision-providers.md).
It answers the three calls the controller makes, in the shapes pinned by
contracts/engine-api-v1/decision/systemone.schema.json:

    GET  /readyz          {"status": "ready", ...}: the health view's probe
    GET  /v1/models       the models with their `limits` (how much text a question may carry)
    POST /v1/systemone    {state, model, questions: {id: {type: choice, instructions, criteria}}}
                          -> {model, answers: {id: {type, choice, probabilities, confidence}},
                              usage, routing, path, latency_ms}

With --key every call needs `Authorization: Bearer <key>` (401 otherwise). `decide()` is where a
real provider calls its own model; this one scores each option by the words it shares with the
text, which is a toy. The tests start it in process (`start()`) with a few knobs of their own:
a delay, a fixed answer, a status, and the calls it received.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMITS = {"context_tokens": 512, "question_tokens": 192, "max_options": 20}
WORD = re.compile(r"[a-z0-9]+")


def decide(state: str, question: dict) -> tuple[str, dict]:
    """(the option id, every option's probability) for one `choice` question. Replace this with
    the call to your own model."""
    words = set(WORD.findall(state.lower()))
    opts = question.get("criteria") or {}
    score = {o: len(words & set(WORD.findall(f"{o} {d}".lower().replace("_", " "))))
             for o, d in opts.items()}
    weight = {o: math.exp(2.5 * s) for o, s in score.items()}
    total = sum(weight.values()) or 1.0
    probs = {o: round(w / total, 4) for o, w in weight.items()}
    return max(opts, key=lambda o: (score[o], o)), probs


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "systemone-example/1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code: int, kind: str, message: str) -> None:
        self._send(code, {"type": "error", "error": {"type": kind, "message": message}})

    def _authed(self) -> bool:
        key = self.server.key
        if not key:
            return True
        got = self.headers.get("Authorization") or ""
        if got.startswith("Bearer ") and hmac.compare_digest(got[7:], key):
            return True
        self._error(401, "authentication_error", "a valid bearer key is needed")
        return False

    def _note(self, path: str, body=None) -> None:
        with self.server.lock:
            self.server.calls.append({"path": path, "auth": self.headers.get("Authorization"),
                                      "body": body})

    def do_GET(self):
        path = self.path.split("?")[0]
        self._note(path)
        if not self._authed():
            return
        if path == "/readyz":
            return self._send(200, {"status": "ready", "engine": "example",
                                    "models": [self.server.model], "version": "1"})
        if path == "/v1/models":
            return self._send(200, {"object": "list", "data": [
                {"id": self.server.model, "object": "model", "limits": dict(self.server.limits),
                 "aliases": ["auto", "jev-latest"]}]})
        return self._error(404, "not_found_error", f"no route {path}")

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = None
        self._note(path, body)
        if not self._authed():
            return
        if path != "/v1/systemone":
            return self._error(404, "not_found_error", f"no route {path}")
        if self.server.delay_ms:
            time.sleep(self.server.delay_ms / 1000.0)
        if self.server.status != 200:
            return self._error(self.server.status, "overloaded_error", "busy")
        qs = (body or {}).get("questions")
        if not isinstance(body, dict) or not isinstance(qs, dict) or not qs:
            return self._error(422, "invalid_request_error", "questions: an object")
        t0 = time.perf_counter()
        state = body.get("state") if isinstance(body.get("state"), str) else \
            json.dumps(body.get("state"))
        answers = {}
        for qid, q in qs.items():
            if not isinstance(q, dict) or q.get("type") != "choice" or not q.get("criteria"):
                return self._error(422, "invalid_request_error",
                                   f"questions.{qid}: a choice question with criteria")
            label, probs = (self.server.answer(state, q) if self.server.answer is not None
                            else decide(state, q))
            answers[qid] = {"type": "choice", "choice": label, "probabilities": probs,
                            "confidence": float(probs.get(label, 0.0))}
        self._send(200, {"contract_version": "1.0", "model": self.server.model,
                         "answers": answers,
                         "usage": {"input_tokens": len(state) // 4, "output_tokens": 0},
                         "routing": {"model": self.server.model, "reason": "example"},
                         "path": "example",
                         "latency_ms": round((time.perf_counter() - t0) * 1e3, 3)})


def start(port: int = 0, key: str | None = None, model: str = "example-decider",
          host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    """The server on a thread: (server, port). Knobs: `delay_ms`, `status`, `answer(state,
    question) -> (label, probs)`, `limits`; `calls` lists what it received."""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.key, srv.model, srv.limits = key, model, dict(LIMITS)
    srv.delay_ms, srv.status, srv.answer = 0, 200, None
    srv.calls, srv.lock = [], threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main() -> None:
    ap = argparse.ArgumentParser(description="an example systemone decision provider")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8720)
    ap.add_argument("--key", default=None, help="the bearer key callers must send")
    ap.add_argument("--model", default="example-decider")
    a = ap.parse_args()
    srv, port = start(a.port, a.key, a.model, a.host)
    print(f"[systemone-example] on {a.host}:{port}, model {a.model}"
          f"{', bearer key required' if a.key else ''}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
