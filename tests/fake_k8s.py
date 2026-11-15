"""A fake Kubernetes API for the pod sandbox backend's tests (chatd/k8s.py): the standard library,
plain HTTP on 127.0.0.1, no cluster. It keeps pods in a dict and answers what the backend asks:

    GET    /api/v1/namespaces/<ns>/pods?labelSelector=k=v,...   the matching pods
    GET    /api/v1/namespaces/<ns>/pods/<name>                  one pod, or 404
    POST   /api/v1/namespaces/<ns>/pods[?dryRun=All]            create (dry run: check only)
    DELETE /api/v1/namespaces/<ns>/pods/<name>                  delete, or 404
    GET    /api/v1/namespaces/<ns>/pods/<name>/log              `logs[name]` or the probe's answer
    GET    /api/v1/namespaces/<ns>/pods/<name>/exec?command=... a WebSocket (v5.channel.k8s.io)
    GET    /api/v1/namespaces/<ns>/secrets                      403 (the Role has none)

What a new pod becomes is `mode`: running (Running at once, an address), pending (Pending,
Unschedulable), image (ErrImageNeverPull), or a callable(pod). A create with runtimeClassName
`missing` is refused as the RuntimeClass admission does; `fail_create` answers that status.
An exec runs `on_exec(ctx)` (the default: the command's last word echoed): `ctx.cmd` is the
command after chatd's wrapper, `ctx.stdin()` reads stdin until it closes, `ctx.out(ch, data)`
writes stdout (1) or stderr (2), and the return value is the exit code (None: no status).
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WRAPPED = 7          # /bin/sh -c WRAP tandem-exec <id> <workdir> <nproc>


class Ctx:
    def __init__(self, sock: socket.socket, pod: dict, argv: list[str], tty: bool):
        self.sock, self.pod, self.argv, self.tty = sock, pod, argv, tty
        self.cmd = argv[WRAPPED:] if argv[:2] == ["/bin/sh", "-c"] else argv
        self.exec_id = argv[4] if argv[:2] == ["/bin/sh", "-c"] else ""
        self.buf = b""
        self.resizes: list[dict] = []

    def frame(self, payload: bytes, op: int = 2) -> None:
        n = len(payload)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        self.sock.sendall(head + payload)

    def out(self, ch: int, data: bytes) -> None:
        self.frame(bytes([ch]) + data)

    def _read_frame(self):
        while True:
            if len(self.buf) >= 2:
                n, off = self.buf[1] & 0x7F, 2
                if n == 126 and len(self.buf) >= 4:
                    n, off = struct.unpack(">H", self.buf[2:4])[0], 4
                elif n == 127 and len(self.buf) >= 10:
                    n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
                if len(self.buf) >= off + 4 + n:
                    mask = self.buf[off:off + 4]
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.buf[off + 4:off + 4 + n]))
                    op = self.buf[0] & 0x0F
                    self.buf = self.buf[off + 4 + n:]
                    return op, data
            d = self.sock.recv(65536)
            if not d:
                return None
            self.buf += d

    def stdin(self) -> bytes:
        got = b""
        while True:
            f = self._read_frame()
            if f is None or f[0] == 8:
                return got
            op, data = f
            if not data:
                continue
            if data[0] == 0:
                got += data[1:]
            elif data[0] == 255:
                return got
            elif data[0] == 4:
                self.resizes.append(json.loads(data[1:]))

    def frames(self, seconds: float) -> None:
        """Read what the client sends for a while (resizes, stdin)."""
        self.sock.settimeout(seconds)
        try:
            while True:
                f = self._read_frame()
                if f is None:
                    return
                if f[1][:1] == b"\x04":
                    self.resizes.append(json.loads(f[1][1:]))
        except OSError:
            return
        finally:
            self.sock.settimeout(None)


class FakeK8s:
    def __init__(self, namespace: str = "ns"):
        self.ns = namespace
        self.pods: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.mode = "running"
        self.fail_create: int | None = None
        self.logs: dict[str, str] = {}
        self.probe = {"controller": False, "egress": True, "api": False}
        self.on_exec = lambda ctx: (ctx.out(1, (" ".join(ctx.cmd[-1:]) + "\n").encode()), 0)[1]
        self.execs: list[Ctx] = []
        self.lock = threading.Lock()
        self.ip_n = 10
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, st, doc):
                raw = json.dumps(doc).encode() if not isinstance(doc, (bytes, str)) else \
                    (doc.encode() if isinstance(doc, str) else doc)
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

            def do_DELETE(self):
                fake.handle(self, "DELETE")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    # ------------------------------------------------------------------ pods
    def status_for(self, pod: dict) -> None:
        mode = self.mode
        if callable(mode):
            mode(pod)
            return
        self.ip_n += 1
        if mode == "running":
            pod["status"] = {"phase": "Running", "podIP": f"10.9.0.{self.ip_n}",
                             "hostIP": "10.8.0.1",
                             "containerStatuses": [{"name": "sandbox", "restartCount": 0,
                                                    "state": {"running": {"startedAt": "now"}}}]}
        elif mode == "pending":
            pod["status"] = {"phase": "Pending", "conditions": [
                {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                 "message": "0/1 nodes are available: 1 Insufficient memory."}]}
        elif mode == "image":
            pod["status"] = {"phase": "Pending", "containerStatuses": [
                {"name": "sandbox", "state": {"waiting": {"reason": "ErrImageNeverPull",
                                                          "message": "image not present"}}}]}

    def add(self, name: str, labels: dict, annotations: dict | None = None, phase="Running",
            ip="10.9.1.1", terminated: dict | None = None) -> dict:
        state = {"terminated": terminated} if terminated else {"running": {"startedAt": "x"}}
        pod = {"metadata": {"name": name, "namespace": self.ns, "labels": dict(labels),
                            "annotations": dict(annotations or {}), "uid": "u-" + name},
               "spec": {}, "status": {"phase": phase, "podIP": ip,
                                      "containerStatuses": [{"name": "sandbox", "state": state}]}}
        self.pods[name] = pod
        return pod

    def oom(self, name: str) -> None:
        pod = self.pods[name]
        pod["status"]["phase"] = "Failed"
        pod["status"]["containerStatuses"][0]["state"] = {"terminated": {
            "reason": "OOMKilled", "exitCode": 137, "containerID": "containerd://x" + name,
            "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")}}

    def evict(self, name: str) -> None:
        pod = self.pods[name]
        pod["status"].update(phase="Failed", reason="Evicted",
                             message="The node was low on resource: memory.")
        pod["status"]["containerStatuses"][0]["state"] = {"terminated": {
            "reason": "Error", "exitCode": 137}}

    # ------------------------------------------------------------------ requests
    def handle(self, h, method: str) -> None:
        u = urllib.parse.urlsplit(h.path)
        q = urllib.parse.parse_qs(u.query)
        self.calls.append((method, h.path))
        if h.headers.get("Authorization") != "Bearer test-token":
            return h._send(401, {"kind": "Status", "message": "Unauthorized"})
        base = f"/api/v1/namespaces/{self.ns}/pods"
        if u.path == f"/api/v1/namespaces/{self.ns}/secrets":
            return h._send(403, {"kind": "Status", "reason": "Forbidden",
                                 "message": "secrets is forbidden"})
        if not u.path.startswith(base):
            return h._send(403, {"kind": "Status", "reason": "Forbidden", "message": "forbidden"})
        rest = u.path[len(base):].strip("/").split("/") if u.path != base else []
        if method == "GET" and not rest:
            sel = {}
            for part in (q.get("labelSelector") or [""])[0].split(","):
                k, _, v = part.partition("=")
                if k:
                    sel[k] = v
            with self.lock:
                items = [p for p in self.pods.values()
                         if all(p["metadata"]["labels"].get(k) == v for k, v in sel.items())]
            return h._send(200, {"kind": "PodList", "items": json.loads(json.dumps(items))})
        if method == "POST" and not rest:
            n = int(h.headers.get("Content-Length") or 0)
            pod = json.loads(h.rfile.read(n))
            if pod["spec"].get("runtimeClassName") == "missing":
                return h._send(403, {"kind": "Status", "reason": "Forbidden", "message":
                                     f'pods "{pod["metadata"]["name"]}" is forbidden: pod '
                                     'rejected: RuntimeClass "missing" not found'})
            if self.fail_create:
                return h._send(self.fail_create, {"kind": "Status", "message": "etcd is down"})
            if "dryRun" in q:
                return h._send(201, pod)
            name = pod["metadata"]["name"]
            with self.lock:
                if name in self.pods:
                    return h._send(409, {"kind": "Status", "reason": "AlreadyExists",
                                         "message": f'pods "{name}" already exists'})
                pod["metadata"]["uid"] = "u-" + name
                if "tandem.chat.probe" in pod["metadata"]["labels"]:
                    pod["status"] = {"phase": "Succeeded"}
                    self.logs.setdefault(name, json.dumps(self.probe) + "\n")
                else:
                    self.status_for(pod)
                self.pods[name] = pod
            return h._send(201, pod)
        name = rest[0] if rest else ""
        pod = self.pods.get(name)
        if method == "DELETE" and len(rest) == 1:
            with self.lock:
                gone = self.pods.pop(name, None)
            return h._send(200 if gone else 404, gone or {"kind": "Status", "reason": "NotFound",
                                                          "message": f'pods "{name}" not found'})
        if pod is None:
            return h._send(404, {"kind": "Status", "reason": "NotFound",
                                 "message": f'pods "{name}" not found'})
        if method == "GET" and len(rest) == 1:
            return h._send(200, pod)
        if method == "GET" and rest[1:] == ["log"]:
            return h._send(200, self.logs.get(name, ""))
        if method == "GET" and rest[1:] == ["exec"]:
            return self._exec(h, pod, q)
        return h._send(404, {"kind": "Status", "message": "no such route"})

    def _exec(self, h, pod: dict, q: dict) -> None:
        if pod["status"].get("phase") != "Running":
            return h._send(400, {"kind": "Status", "message":
                                 "cannot exec into a container in a completed pod; current phase "
                                 f"is {pod['status'].get('phase')}"})
        key = h.headers.get("Sec-WebSocket-Key", "").encode()
        accept = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
        h.send_response(101)
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", accept)
        h.send_header("Sec-WebSocket-Protocol", "v5.channel.k8s.io")
        h.end_headers()
        h.wfile.flush()
        h.close_connection = True
        sock = h.connection
        ctx = Ctx(sock, pod, q.get("command") or [], (q.get("tty") or ["false"])[0] == "true")
        self.execs.append(ctx)
        for ch in (0, 1, 2, 3):
            ctx.frame(bytes([ch]))                   # the channels' opening frames
        code = self.on_exec(ctx)
        if code is not None:
            st = {"status": "Success"} if code == 0 else {
                "status": "Failure", "reason": "NonZeroExitCode",
                "details": {"causes": [{"reason": "ExitCode", "message": str(code)}]}}
            ctx.out(3, json.dumps(st).encode())
        try:
            ctx.frame(struct.pack(">H", 1000), op=8)
        except OSError:
            pass
