"""A fake engine host for the onboarding tests (SRV-827..833): the engine's pairing window
(contracts/engine-pair-v1) written again from its README, its Setup Guide's model routes, and an
mDNS responder on loopback. Standard library only; nothing of TandemApp's pairing code or of the
engine's: tests/test_engine_pair.py holds this copy to the pinned fixtures byte for byte (fixed
y, session, nonce and token), then every flow runs against it with random ones.

    e = FakeEngine(host_key="ssh-ed25519 AAAA...", engine_dir=..., authorized_keys=...).start()
    code = e.open()                       `tandem pair`: a window, its code
    e.port                                its HTTP port on 127.0.0.1
    e.swap = {"host_key": "..."}          step 1 answers with another host key (a man in the
                                          middle; with "fingerprint" too, a consistent one)
    e.tokens                              the bearers its HTTP routes take
    e.calls                               (method, path, bearer ok) of each request, no body
    r = MdnsResponder(answer_bytes).start()   answers a _tandem._tcp browse on 127.0.0.1
    sock = loop_socket([r.addr])          a Browser socket whose multicast goes to the responders

The window keeps the engine's rules: 6 digits, 5 minutes, 5 runs (each step 1 spends one), single
use, a run's session 60 s; GET /v1/engine/pair answers without the token only while a window is
open (401 otherwise, as the engine's token rule for a caller from the network); wrong codes and
abandoned runs spend the code. Step 2 appends the key's forced-command line (the engine's
`key_line`) to `authorized_keys`, which tests/fake_ssh.py reads, and issues a token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAIR = "engine-pair-v1"
AAD = b"TandemLLM pairing v1"
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404"
    "DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F4"
    "06B7EDEE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA"
    "8FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C3290"
    "5E462E36CE3BE39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE5"
    "15D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
Q = (P - 1) // 2
OPTS = "no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"
KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


# ---------------------------------------------------------------------------- the math, again
def _h2g(label: bytes) -> int:
    out, i = b"", 0
    while len(out) < 256 + 32:
        out += hashlib.sha512(label + i.to_bytes(4, "big")).digest()
        i += 1
    return pow(int.from_bytes(out, "big") % P, 2, P)


M, N = _h2g(b"TandemLLM SPAKE2 M"), _h2g(b"TandemLLM SPAKE2 N")


def hkdf(ikm: bytes, info: bytes, n: int) -> bytes:
    prk = hmac.new(b"\0" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


def w_of(code: str) -> int:
    return int.from_bytes(hkdf(code.encode(), b"TandemLLM pairing w", 64), "big") % Q


def transcript(ida: str, idb: str, pa: int, pb: int, k: int, w: int) -> bytes:
    def lp(b):
        return struct.pack("<Q", len(b)) + b
    return b"".join(lp(x) for x in (ida.encode(), idb.encode(), pa.to_bytes(256, "big"),
                                     pb.to_bytes(256, "big"), k.to_bytes(256, "big"),
                                     w.to_bytes(256, "big")))


def fp_of(b64: str) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(b64)).digest()).decode().rstrip("=")


def seal(ke: bytes, plain: bytes, nonce: bytes) -> dict:
    k = hkdf(ke, b"TandemLLM engine pairing seal" + AAD, 64)
    stream, i = b"", 0
    while len(stream) < len(plain):
        stream += hmac.new(k[:32], nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    box = bytes(a ^ b for a, b in zip(plain, stream))
    return {"nonce": nonce.hex(), "box": box.hex(),
            "tag": hmac.new(k[32:], nonce + box, hashlib.sha256).hexdigest()}


class Refuse(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


# ---------------------------------------------------------------------------- the engine
class FakeEngine:
    def __init__(self, host_key: str, *, host_keys: list[str] | None = None, ssh_user: str = "u",
                 ssh_port: int = 22, engine_dir: str = "/home/u/TandemLLM",
                 authorized_keys: str | None = None, version: str | None = "0.15.4",
                 mode: str = "setup", clock=time.time, y: int | None = None,
                 session: str | None = None, nonce: bytes | None = None, token: str | None = None,
                 models: list[dict] | None = None):
        self.host_key = host_key
        self.host_keys = host_keys or [host_key]
        self.ssh_user, self.ssh_port = ssh_user, ssh_port
        self.engine_dir, self.authorized_keys = engine_dir, authorized_keys
        self.version, self.mode, self.clock = version, mode, clock
        self.fixed = {"y": y, "session": session, "nonce": nonce, "token": token}
        self.window: dict | None = None
        self.tokens: set[str] = set()
        self.calls: list[tuple] = []
        self.swap: dict = {}
        self.pair_routes = True              # False: an engine before 1.9 (404 not_found)
        self.allow = True                    # False: QSE_ALLOW_FROM leaves this caller out
        self.models = models if models is not None else [
            {"id": "qwen3.8-27b", "name": "Qwen3.8 27B", "family": "qwen3.8", "params": "27B",
             "pitch": "the dense one", "recommended": True, "icon": None, "best_speed": None,
             "community_count": 0}]
        self.jobs: dict[str, dict] = {}
        self.applied: list[dict] = []
        self.plan_errors: list[str] = []
        self.setup_password = False          # True: its Setup Guide wants the admin password
        self._lock = threading.Lock()
        self.srv = None

    # ------------------------------------------------------------------ the window
    def open(self, code: str | None = None, ttl: float = 300.0, tries: int = 5) -> str:
        with self._lock:
            self.window = {"code": code or f"{secrets.randbelow(10 ** 6):06d}", "state": "open",
                           "expires_at": self.clock() + ttl, "tries": tries, "tries_left": tries,
                           "sessions": {}}
            return self.window["code"]

    def close(self) -> None:
        with self._lock:
            if self.window and self.window["state"] == "open":
                self.window["state"] = "closed"

    def _expire(self) -> None:
        w = self.window
        if w and w["state"] == "open" and self.clock() >= w["expires_at"]:
            w["state"] = "expired"
            w["sessions"].clear()

    def is_open(self) -> bool:
        with self._lock:
            self._expire()
            return bool(self.window) and self.window["state"] == "open"

    def status(self) -> dict:
        with self._lock:
            self._expire()
            w = self.window
            if w is None:
                return {"pair": PAIR, "open": False, "state": "none"}
            return {"pair": PAIR, "open": w["state"] == "open", "state": w["state"],
                    "expires_in": max(0, round(w["expires_at"] - self.clock())),
                    "tries_left": w["tries_left"], "fingerprint": fp_of(self.host_key.split()[1])}

    def _usable(self, w) -> None:
        self._expire()
        if w["state"] in ("expired", "spent") or w["state"] != "open":
            raise Refuse(410, "code_spent", "this code cannot be used; run `tandem pair` again")

    def _spend_if_out(self, w) -> None:
        if w["tries_left"] <= 0 and w["state"] == "open":
            w["state"] = "spent"
            w["sessions"].clear()

    def _win(self):
        if self.window is None or self.window["state"] == "closed":
            raise Refuse(404, "no_window", "no pairing window is open: run `tandem pair`")
        return self.window

    def step1(self, body: dict) -> dict:
        if not isinstance(body, dict) or not KEY_ID.match(str(body.get("key_id") or "")):
            raise Refuse(400, "bad_request", "the body is {pA, public_key, key_id, name?}")
        parts = str(body.get("public_key") or "").split()
        if len(parts) != 2 or parts[0] != "ssh-ed25519" or len(base64.b64decode(parts[1])) != 51:
            raise Refuse(400, "bad_request", "public_key: one ssh-ed25519 public key")
        try:
            pa = int(body.get("pA"), 16)
        except (TypeError, ValueError):
            pa = -1
        if not 1 < pa < P - 1 or pow(pa, Q, P) != 1:
            raise Refuse(400, "bad_request", "pA: not a group element")
        with self._lock:
            w = self._win()
            self._usable(w)
            self._spend_if_out(w)
            self._usable(w)
            w["tries_left"] -= 1
            code_w = w_of(w["code"])
            y = self.fixed["y"] or 1 + secrets.randbelow(Q - 1)
            pb = pow(2, y, P) * pow(N, code_w, P) % P
            k = pow(pa * pow(M, Q - code_w, P) % P, y, P)
            host_key = self.swap.get("host_key", self.host_key)
            fp = fp_of(host_key.split()[1])
            ida = f"tandemapp-key {fp_of(parts[1])} {body['key_id']}"
            tt = transcript(ida, f"tandemengine {fp_of(self.host_key.split()[1])}", pa, pb, k, code_w)
            ka = hashlib.sha256(tt).digest()[16:]
            kc = hkdf(ka, b"ConfirmationKeys" + AAD, 32)
            sid = self.fixed["session"] or secrets.token_hex(16)
            w["sessions"][sid] = {"until": self.clock() + 60, "kca": kc[:16], "tt": tt,
                                  "key_id": body["key_id"], "key_b64": parts[1]}
            return {"pair": PAIR, "session": sid, "pB": format(pb, "x"),
                    "cB": hmac.new(kc[16:], tt, hashlib.sha256).hexdigest(), "host_key": host_key,
                    "fingerprint": self.swap.get("fingerprint", fp_of(self.host_key.split()[1])),
                    "tries_left": w["tries_left"],
                    "expires_in": max(0, round(w["expires_at"] - self.clock()))}

    def step2(self, body: dict) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("session"), str) or \
                not isinstance(body.get("cA"), str):
            raise Refuse(400, "bad_request", "the body is {session, cA}")
        with self._lock:
            w = self._win()
            s = w["sessions"].pop(body["session"], None)
            if s is None or s["until"] <= self.clock():
                self._usable(w)
                raise Refuse(400, "bad_request", "no such pairing session (one try, 60 s)")
            self._usable(w)
            if not hmac.compare_digest(body["cA"], hmac.new(s["kca"], s["tt"], hashlib.sha256).hexdigest()):
                self._spend_if_out(w)
                raise Refuse(403, "wrong_code", "the code does not match" + (
                    f": {w['tries_left']} tries left" if w["state"] == "open" else "; spent"))
            line = (f'command="/bin/bash {self.engine_dir}/bin/tandem remote --key-id {s["key_id"]}",'
                    f'{OPTS} ssh-ed25519 {s["key_b64"]} {s["key_id"]}')
            added = True
            if self.authorized_keys:
                os.makedirs(os.path.dirname(self.authorized_keys), mode=0o700, exist_ok=True)
                old = open(self.authorized_keys).read() if os.path.exists(self.authorized_keys) else ""
                if f" ssh-ed25519 {s['key_b64']} " in old:
                    if line not in old.splitlines():
                        w["state"] = "open"
                        raise Refuse(409, "key_present", "the key already has a line of its own")
                    added = False
                else:
                    with open(self.authorized_keys, "a") as f:
                        f.write(line + "\n")
            token = self.fixed["token"] or secrets.token_urlsafe(32)
            self.tokens.add(token)
            w["state"] = "paired"
            w["sessions"].clear()
            doc = {"pair": PAIR, "key_id": s["key_id"], "ssh_user": self.ssh_user,
                   "ssh_port": self.ssh_port, "host_key": self.host_key, "host_keys": self.host_keys,
                   "http_port": self.port if self.srv else 8000, "http_token": token,
                   "engine_version": self.version, "mode": self.mode,
                   "authorized_keys": {"added": added, "line": line}}
            plain = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
            ke = hashlib.sha256(s["tt"]).digest()[:16]
            return {"pair": PAIR, "sealed": seal(ke, plain, self.fixed["nonce"] or secrets.token_bytes(16))}

    def abandon(self, body: dict) -> dict:
        with self._lock:
            w = self._win()
            gone = w["sessions"].pop(str((body or {}).get("session")), None) is not None
            if gone:
                self._spend_if_out(w)
            return {"pair": PAIR, "abandoned": gone, "tries_left": w["tries_left"], "state": w["state"]}

    # ------------------------------------------------------------------ the Setup Guide
    def _job(self, jid: str) -> dict | None:
        """A job moves one step a read: check, fetch (its detail the bytes so far), then done."""
        j = self.jobs.get(jid)
        if j is None:
            return None
        j["reads"] += 1
        order = ["check", "unload_assistant", "fetch", "preflight", "write_config", "switch", "smoke"]
        n = min(j["reads"], len(order))
        for i, s in enumerate(j["job"]["steps"]):
            if s["id"] == "rollback":
                continue
            s["state"] = "done" if i < n - 1 else "running" if i == n - 1 else "pending"
            if s["id"] == "fetch" and s["state"] == "running":
                s["detail"] = "9.2 of 18.4 GB"
        if j["reads"] >= len(order) + 1:
            j["job"]["state"] = "done"
            for s in j["job"]["steps"]:
                s["state"] = "done" if s["id"] != "rollback" else "skipped"
        else:
            j["job"]["state"] = "running"
        return j["job"]

    def setup(self, method: str, parts: list[str], body) -> tuple[int, dict]:
        known = {m["id"] for m in self.models}
        if method == "GET" and parts == ["models"]:
            return 200, {"contract_version": "1.0", "models": self.models, "errors": []}
        if method == "GET" and len(parts) == 3 and parts[0] == "models" and parts[2] == "current":
            if parts[1] not in known:
                return 404, {"error": {"type": "unknown_model", "code": "unknown_model",
                                       "message": f"no model {parts[1]!r}"}}
            return 200, {"contract_version": "1.0", "model": parts[1], "unmatched": [],
                         "selection": {"model": parts[1], "profile": "exl3-4.5", "components": {},
                                       "slots": 4, "window": 131072}}
        if method == "POST" and parts in (["plan"], ["apply"]):
            sel = (body or {}).get("selection") if isinstance(body, dict) else None
            if not isinstance(sel, dict) or sel.get("model") not in known:
                return 400, {"error": {"type": "unknown_model", "code": "unknown_model",
                                       "message": "the selection names no model of this box"}}
            plan = {"contract_version": "1.0", "ok": not self.plan_errors, "errors": self.plan_errors,
                    "error_codes": ["over_budget"] * len(self.plan_errors), "warnings": [],
                    "profile": {"current": None, "next": sel["profile"], "on_disk": False,
                                "download_gb": 18.4},
                    "downloads": [], "changes": [], "restart": True, "commands": []}
            if parts == ["plan"]:
                return 200, plan
            if self.plan_errors:
                return 409, {"error": {"type": "over_budget", "code": "over_budget",
                                       "message": " ".join(self.plan_errors)}, "plan": plan}
            if any(j["job"]["state"] in ("queued", "running") for j in self.jobs.values()):
                return 409, {"error": {"type": "job_running", "code": "job_running",
                                       "message": "a job runs; one setup job at a time"}}
            jid = f"20261115T1000{len(self.jobs):02d}-abcdef"
            steps = [{"id": i, "label": i, "state": "pending", "started_at": None,
                      "ended_at": None, "detail": None}
                     for i in ("check", "unload_assistant", "fetch", "preflight", "write_config",
                               "switch", "smoke", "rollback")]
            job = {"id": jid, "kind": "apply", "state": "queued", "created_at": 0, "ended_at": None,
                   "profile": sel["profile"], "steps": steps, "message": None, "changes": [],
                   "rolled_back": False, "failed_step": None, "pid": None, "url": "x",
                   "model": sel["model"], "summary": {}, "retry_of": None}
            self.jobs[jid] = {"job": job, "reads": 0}
            self.applied.append(sel)
            return 202, {"contract_version": "1.0", "job": job}
        if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            j = self._job(parts[1])
            if j is None:
                return 404, {"error": {"type": "not_found", "code": "not_found", "message": "no job"}}
            return 200, j
        if method == "GET" and parts == ["status"]:
            run = next((j["job"] for j in self.jobs.values() if j["job"]["state"] in ("queued", "running")), None)
            return 200, {"contract_version": "1.0", "engine": "setup", "job": run}
        return 404, {"error": {"type": "not_found", "code": "not_found", "message": "no route"}}

    # ------------------------------------------------------------------ HTTP
    def start(self) -> "FakeEngine":
        eng = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, st: int, doc: dict, headers=()):
                raw = json.dumps(doc).encode()
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _do(self, method: str):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                path = self.path.split("?")[0]
                auth = self.headers.get("Authorization") or ""
                ok = auth.startswith("Bearer ") and auth[7:] in eng.tokens
                eng.calls.append((method, self.path, ok))
                if not eng.allow:
                    return self._send(403, {"error": {"type": "forbidden_address",
                                                      "message": "this address is not allowed"}})
                if path == "/health":
                    return self._send(200, {"status": "ok", "mode": eng.mode, "engine_api": "1.9",
                                            "version": eng.version})
                if path.startswith("/v1/engine/pair"):
                    if not eng.pair_routes:
                        return self._send(404, {"error": {"type": "not_found", "message": "no route"}})
                    rest = path[len("/v1/engine/pair"):].strip("/")
                    if not eng.is_open() and not ok:
                        return self._send(401, {"error": {"type": "unauthorized",
                                                          "message": "a bearer token is needed"}})
                    try:
                        if method == "GET" and rest == "":
                            return self._send(200, eng.status())
                        fn = {"1": eng.step1, "2": eng.step2, "abandon": eng.abandon}.get(rest)
                        if method != "POST" or fn is None:
                            return self._send(404, {"error": {"type": "not_found", "message": "no route"}})
                        return self._send(200, fn(body))
                    except Refuse as exc:
                        return self._send(exc.status, {"error": {"type": exc.kind, "message": exc.message}})
                if path == "/v1/models":
                    if not ok:
                        return self._send(401, {"error": {"type": "unauthorized",
                                                          "message": "a bearer token is needed"}})
                    return self._send(200, {"object": "list", "data": []})
                if path.startswith("/v1/setup/"):
                    if not ok:
                        return self._send(401, {"error": {"type": "unauthorized",
                                                          "message": "a bearer token is needed"}})
                    if eng.setup_password:
                        return self._send(401, {"error": {"type": "unauthorized", "code": "unauthorized",
                                                          "message": "the admin password is set"}})
                    if method == "POST" and self.headers.get("Content-Type") != "application/json":
                        return self._send(415, {"error": {"type": "json_only", "message": "json only"}})
                    st, doc = eng.setup(method, path[len("/v1/setup/"):].split("/"), body)
                    return self._send(st, doc)
                return self._send(404, {"error": {"type": "not_found", "message": "no route"}})

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self

    def stop(self) -> None:
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()


# ---------------------------------------------------------------------------- mDNS on loopback
class MdnsResponder:
    """Answers a browse for `_tandem._tcp.local` with `answer` (the engine's bytes), by unicast to
    the asker; every query it got is in `queries`."""

    def __init__(self, answer: bytes, service: bytes = b"\x07_tandem\x04_tcp\x05local\x00"):
        self.answer, self.service = answer, service
        self.queries: list[bytes] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.addr = self.sock.getsockname()
        self._stop = threading.Event()

    def start(self) -> "MdnsResponder":
        def run():
            self.sock.settimeout(0.1)
            while not self._stop.is_set():
                try:
                    data, src = self.sock.recvfrom(9000)
                except socket.timeout:
                    continue
                except OSError:
                    return
                self.queries.append(data)
                if self.service in data and not (len(data) > 2 and data[2] & 0x80):
                    self.sock.sendto(self.answer, src)
        threading.Thread(target=run, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.sock.close()


class _LoopSock:
    def __init__(self, targets: list[tuple]):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        self.targets = targets

    def sendto(self, data: bytes, _addr) -> int:
        for t in self.targets:
            self.s.sendto(data, t)
        return len(data)

    def recvfrom(self, n: int):
        return self.s.recvfrom(n)

    def settimeout(self, t) -> None:
        self.s.settimeout(t)

    def close(self) -> None:
        self.s.close()


def loop_socket(targets: list[tuple]):
    """A proxy/mdns.py Browser's `sock`: its multicast query goes to each responder here."""
    return lambda _iface=None: _LoopSock(targets)
