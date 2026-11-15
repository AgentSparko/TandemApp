"""Chat sandboxes as Kubernetes pods (SRV-863): the `kubernetes` sandbox backend.

chatd runs in the controller's pod (the Helm chart, `sandbox.mode: kubernetes`) and makes each
chat's sandbox a pod in the release's namespace through the Kubernetes API, with the controller
pod's ServiceAccount token (a Role: pods, pods/exec and pods/log in this namespace, nothing
else). `QSE_SANDBOX_BACKEND=kubernetes` selects it (chatd/sandbox.py `make`); it keeps the
interface of `Sandboxes`, so the tools, terminals, the preview and MCP servers do not change.
Standard library only: HTTPS to the API, and a small WebSocket client for exec.

THE POD. `tandem-chat-<id>` from the sandbox image (QSE_SANDBOX_IMAGE, imagePullPolicy
QSE_K8S_IMAGE_PULL, Never by default: the image is put on the node by ops/sandbox/build-k8s.sh,
never pulled), labelled with the chart's sandbox labels (QSE_K8S_LABELS, JSON) and chatd's own
`tandem.chat` (the chat) and `tandem.chat.instance` (this chatd). As the Docker path: uid 1000,
no privilege escalation, every capability dropped, seccomp RuntimeDefault, a read-only root, an
in-memory /tmp (512 MiB, counted in the memory limit), no service-account token, no service
links, a dead DNS resolver (127.0.0.1: names are resolved by the egress proxy). Limits: memory
QSE_SANDBOX_MEM_GB as both request and limit (the scheduler keeps that much free for it; no swap:
the kubelet's default), QSE_SANDBOX_CPUS (4) CPUs as the limit, and 512 processes. The process
limit: Kubernetes has no per-pod field (the kubelet's podPidsLimit is the node's), so with a user
namespace (QSE_K8S_HOST_USERS=false, `hostUsers: false`: uid 1000 in the pod is an unprivileged
uid of the node, not the node's own uid 1000) every exec runs under RLIMIT_NPROC 512, which the
kernel counts per user namespace, i.e. per pod. Without one RLIMIT_NPROC would count the node's
uid 1000 everywhere, so it is not set and the node's podPidsLimit is the only bound. An optional
runtimeClassName (QSE_K8S_RUNTIME_CLASS: gVisor, Kata). The pod runs `sleep infinity` with a
shared process namespace (the pod's pause process is pid 1 and reaps, Docker's --init),
restartPolicy Never, on the controller's node, owned by the controller's pod (a deleted
controller pod takes its sandboxes with it).

THE FILES. /workspace and /home/agent are subPath mounts of the release's own claim
(QSE_K8S_CLAIM, mounted in chatd's pod at QSE_K8S_VOLUME_ROOT): `<data>/workspaces/<id>` and
`<data>/homes/<id>`, the same folders the Docker path binds. chatd reads and writes them as plain
folders (the file panel, uploads, snapshots, the purge) with no copy in or out. A ReadWriteOnce
claim is mounted by pods of one node, hence the node affinity: with a single-node volume (hostPath,
local) this is the design's limit, and sandboxes do not spread over nodes. chatd and the sandbox
are different uids (10001 and 1000), so chatd gives both rwx through POSIX ACLs on the two folders
(access and default entries, written as xattrs; the Pi's M3 ACLs, made by chatd itself), once per
chat, over the whole tree the first time (a tree copied from another host).

THE NETWORK. A NetworkPolicy of the chart lets a sandbox pod reach only chatd's egress port
(QSE_K8S_EGRESS_PORT, 3128) on the controller's pod, and nothing reach it. chatd's egress proxy
(chatd/egress.py, the same checks: the public internet yes, the LAN, the cluster and this box no;
a chat whose network is off gets 403) listens on that port; a connection is the sandbox whose pod
has the peer's address, and an address that is no running sandbox of this chatd is refused.
HTTP_PROXY and HTTPS_PROXY point there. At start chatd proves the policy is enforced: a probe pod
with the sandbox's labels and spec tries the controller's own port, the Kubernetes API and the
egress port. If the first two answer, the cluster does not enforce NetworkPolicy, and there are no
sandboxes at all (each call says why): a pod without an enforced policy has the pod network.

EXEC. The pods/exec subresource over WebSocket (v5.channel.k8s.io, else v4): the stream is turned
into what the Docker path's callers read, a local socket with Docker's 8-byte frames (a TTY: raw
bytes); their writes go to the process's stdin, a half close closes it. Each exec is wrapped by
/bin/sh that records its pid in /tmp/.tandem-exec/<exec id> and goes to its working folder; an
exec is ended (`end_exec`: TERM to it) or killed (a run's timeout or cancel: TERM, then KILL, to
its process tree) by a second exec that signals that pid. The exit code comes from the stream's
status channel.

LIMITS AND STATES. The running count for QSE_SANDBOX_MAX_RUNNING and QSE_SANDBOX_TOTAL_GB is
this chatd's pods in the cluster (not only its own table), read at most every 2 s. There is no
memory floor of chatd's own here: the scheduler places a pod only where its memory request fits;
a pod that stays Pending (no node with room, a quota) is reported as capacity and deleted after
the wait (WAIT_S, 60 s). An image the node does not have, a runtimeClass that does not exist and
any other refusal fail the call with the cluster's reason. A kill at the memory limit stops the
whole pod (the kubelet sets memory.oom.group): its status says OOMKilled and the call's result
says out of memory; an eviction (the node short of memory) says so, not OOM. 30 minutes idle
stops (deletes) the pod; the files stay. At start and once a minute, this chatd's pods whose chat
has no running sandbox here are deleted, and at start pods made for another egress address (a
previous controller pod) too.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse

from chatd import sandbox as _sb
from chatd.egress import TcpProxy
from chatd.sandbox import (CPUS, GiB, INSTANCE_LABEL, LABEL, PIDS, Box, Gone, Sandboxes,
                           Unavailable)

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
CONTAINER = "sandbox"
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROTOCOLS = ("v5.channel.k8s.io", "v4.channel.k8s.io")
PROBE_LABEL = "tandem.chat.probe"
EGRESS_ANNOTATION = "tandem.chat/egress"
ID_ANNOTATION = "tandem.chat/id"
PIDDIR = "/tmp/.tandem-exec"
#: the exec wrapper: $1 exec id, $2 working folder, $3 process limit (0: none), then the command
WRAP = ('mkdir -p ' + PIDDIR + ' 2>/dev/null; echo $$ > ' + PIDDIR + '/"$1" 2>/dev/null; '
        'cd "$2" 2>/dev/null || cd /; [ "$3" = 0 ] || prlimit --nproc="$3" --pid $$ 2>/dev/null; '
        'shift 3; exec "$@"')
#: signals an exec's process (`one`) or its process tree, TERM then KILL after 2 s (`tree`)
KILL_PY = r"""
import os, signal, sys, time
try:
    pid = int(open(sys.argv[1]).read().strip())
except (OSError, ValueError):
    sys.exit(0)
def tree(root):
    kids = {}
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                ppid = int(open('/proc/%s/stat' % d, 'rb').read().rpartition(b')')[2].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            kids.setdefault(ppid, []).append(int(d))
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out
sigs = [signal.SIGTERM, signal.SIGKILL] if sys.argv[2] == 'tree' else [signal.SIGTERM]
for i, sig in enumerate(sigs):
    for p in (tree(pid) if sys.argv[2] == 'tree' else [pid]):
        try:
            os.kill(p, sig)
        except OSError:
            pass
    if i + 1 < len(sigs):
        time.sleep(2)
"""
#: the start check's probe: which of the three addresses answer (connect within 3 s)
PROBE_PY = r"""
import json, os, socket, sys, time
time.sleep(2)
def reach(host, port):
    if not host:
        return None
    s = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET)
    s.settimeout(3)
    try:
        s.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()
print(json.dumps({'controller': reach(sys.argv[1], sys.argv[2]),
                  'egress': reach(sys.argv[1], sys.argv[3]),
                  'api': reach(os.environ.get('KUBERNETES_SERVICE_HOST', ''),
                               os.environ.get('KUBERNETES_SERVICE_PORT', '443'))}))
"""


class ApiError(OSError):
    """The API answered with an error status (`status`, the Status' `reason` and `message`)."""

    def __init__(self, status: int, doc):
        self.status = status
        self.reason = (doc or {}).get("reason", "") if isinstance(doc, dict) else ""
        self.message = ((doc or {}).get("message") if isinstance(doc, dict) else str(doc or ""))
        super().__init__(f"the Kubernetes API answered {status}: {(self.message or '')[:300]}")


# ====================================================================== the API
class Api:
    """The Kubernetes API with a ServiceAccount token (read at every call: bound tokens rotate)."""

    def __init__(self, url: str | None = None, token_file: str | None = None,
                 ca_file: str | None = None, namespace: str | None = None, env=None):
        env = os.environ if env is None else env
        self.url = (url or env.get("QSE_K8S_API") or "https://kubernetes.default.svc").rstrip("/")
        self.token_file = token_file or env.get("QSE_K8S_TOKEN_FILE") or f"{SA_DIR}/token"
        self.ca_file = ca_file or env.get("QSE_K8S_CA_FILE") or f"{SA_DIR}/ca.crt"
        ns = namespace or env.get("QSE_K8S_NAMESPACE")
        if not ns:
            try:
                with open(f"{SA_DIR}/namespace") as fh:
                    ns = fh.read().strip()
            except OSError:
                ns = "default"
        self.namespace = ns
        u = urllib.parse.urlsplit(self.url)
        self.tls = u.scheme == "https"
        self.host = u.hostname or "localhost"
        self.port = u.port or (443 if self.tls else 80)
        self._ctx: ssl.SSLContext | None = None

    def token(self) -> str:
        try:
            with open(self.token_file) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def ctx(self) -> ssl.SSLContext:
        if self._ctx is None:
            self._ctx = ssl.create_default_context(
                cafile=self.ca_file if os.path.exists(self.ca_file) else None)
        return self._ctx

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        tok = self.token()
        if tok:
            h["Authorization"] = "Bearer " + tok
        return h

    def call(self, method: str, path: str, body=None, timeout: float = 30.0):
        """(status, the answer's JSON or text). OSError when the API cannot be reached."""
        if self.tls:
            c = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                            context=self.ctx())
        else:
            c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            h = self._headers()
            if data is not None:
                h["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, raw.decode("utf-8", "replace")
        except (http.client.HTTPException, ssl.SSLError) as exc:
            raise OSError(f"the Kubernetes API: {type(exc).__name__}: {exc}") from None
        finally:
            c.close()

    def pods(self, sub: str = "") -> str:
        return f"/api/v1/namespaces/{urllib.parse.quote(self.namespace)}/pods{sub}"

    def websocket(self, path: str, timeout: float = 30.0) -> "WebSocket":
        """GET `path` upgraded to a WebSocket with the channel protocols; ApiError when the API
        refuses the upgrade (its Status), OSError when it cannot be reached."""
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            sock = self.ctx().wrap_socket(raw, server_hostname=self.host) if self.tls else raw
            key = base64.b64encode(secrets.token_bytes(16))
            head = [f"GET {path} HTTP/1.1", f"Host: {self.host}:{self.port}",
                    "Upgrade: websocket", "Connection: Upgrade",
                    f"Sec-WebSocket-Key: {key.decode()}", "Sec-WebSocket-Version: 13",
                    f"Sec-WebSocket-Protocol: {', '.join(PROTOCOLS)}"]
            for k, v in self._headers().items():
                if k != "Accept":
                    head.append(f"{k}: {v}")
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = sock.recv(65536)
                if not d:
                    raise OSError("the Kubernetes API closed the exec stream at its start")
                buf += d
                if len(buf) > 1 << 20:
                    raise OSError("the exec stream's head is too long")
            hdr, _, rest = buf.partition(b"\r\n\r\n")
            lines = hdr.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            fields = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                fields[k.strip().lower()] = v.strip()
            if status != 101:
                n = int(fields.get("content-length") or 0)
                while len(rest) < n:
                    d = sock.recv(65536)
                    if not d:
                        break
                    rest += d
                try:
                    doc = json.loads(rest[:n] if n else rest)
                except ValueError:
                    doc = {"message": rest[:300].decode("utf-8", "replace")}
                raise ApiError(status, doc)
            want = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
            if fields.get("sec-websocket-accept") != want:
                raise OSError("the exec stream's handshake does not match")
            return WebSocket(sock, fields.get("sec-websocket-protocol", ""), rest)
        except BaseException:
            raw.close()
            raise


class WebSocket:
    """A client WebSocket (RFC 6455) on a blocking socket: one reader (`frames`) and senders
    under one lock (an SSL socket is never read and written at once)."""

    def __init__(self, sock: socket.socket, protocol: str, first: bytes = b""):
        self.sock, self.protocol = sock, protocol
        self.buf = bytearray(first)
        self.lock = threading.Lock()
        self.closed = False
        self._frag: tuple[int, bytearray] | None = None

    def fileno(self) -> int:
        return self.sock.fileno()

    def pending(self) -> bool:
        return bool(self.buf) or (isinstance(self.sock, ssl.SSLSocket) and
                                  self.sock.pending() > 0)

    def send(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if n < 4096 else \
            _mask(payload, mask)
        with self.lock:
            if self.closed:
                raise OSError("the exec stream is closed")
            self.sock.sendall(head + mask + body)

    def read(self, timeout: float) -> list[tuple[int, bytes]] | None:
        """The whole frames that arrived within `timeout` s ([] when none); None at the end."""
        if not self.pending():
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return []
        with self.lock:
            self.sock.settimeout(timeout if timeout > 0 else 0.001)
            try:
                d = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError, BlockingIOError):
                d = None
            except OSError:
                return None
            finally:
                try:
                    self.sock.settimeout(None)
                except OSError:
                    pass
        if d == b"":
            return None
        if d:
            self.buf += d
        return self._parse()

    def _parse(self) -> list[tuple[int, bytes]]:
        out = []
        while len(self.buf) >= 2:
            b0, b1 = self.buf[0], self.buf[1]
            n, off = b1 & 0x7F, 2
            if n == 126:
                if len(self.buf) < 4:
                    break
                n, off = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                if len(self.buf) < 10:
                    break
                n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
            if n > _sb.FRAME_CAP:
                raise OSError(f"an exec frame claims {n:,} bytes (over the 64 MiB cap)")
            masked = b1 & 0x80
            if masked:
                off += 4
            if len(self.buf) < off + n:
                break
            payload = bytes(self.buf[off:off + n])
            if masked:
                payload = _mask(payload, bytes(self.buf[off - 4:off]))
            del self.buf[:off + n]
            fin, op = b0 & 0x80, b0 & 0x0F
            if op == 0:
                if self._frag is None:
                    continue
                self._frag[1].extend(payload)
                if fin:
                    out.append((self._frag[0], bytes(self._frag[1])))
                    self._frag = None
            elif not fin and op in (1, 2):
                self._frag = (op, bytearray(payload))
            else:
                out.append((op, payload))
        return out

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            mask = secrets.token_bytes(4)
            self.sock.sendall(bytes([0x88, 0x82]) + mask + _mask(struct.pack(">H", 1000), mask))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big")


def exit_of(status) -> int | None:
    """The exit code in an exec's status channel (a metav1.Status), None when it names none."""
    if not isinstance(status, dict):
        return None
    if status.get("status") == "Success":
        return 0
    for c in ((status.get("details") or {}).get("causes") or []):
        if c.get("reason") == "ExitCode":
            try:
                return int(c.get("message"))
            except (TypeError, ValueError):
                return None
    return None


class Exec:
    """One exec: the WebSocket pumped into a local socket pair. The caller holds `outer`; what
    the process writes arrives there as Docker frames (raw with a TTY), what the caller writes
    goes to its stdin, and a half close (shutdown SHUT_WR) closes stdin."""

    def __init__(self, ws: WebSocket, tty: bool, stdin: bool = True, pod: str = "", log=None):
        self.ws, self.tty, self.stdin, self.pod = ws, tty, stdin, pod
        self.log = log or (lambda line: None)
        self.outer, self.inner = socket.socketpair()
        self.running, self.code, self.status = True, None, None
        self.ended_at: float | None = None
        self.thread = threading.Thread(target=self._pump, name="chatd-k8s-exec", daemon=True)
        self.thread.start()

    def resize(self, cols: int, rows: int) -> None:
        self.ws.send(2, b"\x04" + json.dumps({"Width": int(cols), "Height": int(rows)}).encode())

    def _out(self, ch: int, data: bytes) -> None:
        if self.tty:
            self.inner.sendall(data)
        else:
            self.inner.sendall(bytes([ch, 0, 0, 0]) + struct.pack(">I", len(data)) + data)

    def _pump(self) -> None:
        stdin_open, v5 = self.stdin, self.ws.protocol.startswith("v5")
        try:
            while True:
                rl = [self.ws.sock] + ([self.inner] if stdin_open else [])
                if self.ws.pending():
                    ready = [self.ws.sock]
                else:
                    ready, _, _ = select.select(rl, [], [], 1.0)
                    if not ready and not self.stdin and _peer_gone(self.inner):
                        return             # the caller closed its end: the stream goes too
                if self.inner in ready:
                    try:
                        d = self.inner.recv(65536)
                    except OSError:
                        d = b""
                    if d:
                        self.ws.send(2, b"\x00" + d)
                    else:
                        stdin_open = False
                        if v5:
                            self.ws.send(2, b"\xff\x00")        # v5: close stdin
                if self.ws.sock in ready:
                    frames = self.ws.read(0)
                    if frames is None:
                        return
                    for op, data in frames:
                        if op == 8:
                            return
                        if op == 9:
                            self.ws.send(10, data)
                        elif op in (1, 2) and len(data) > 1:
                            ch, payload = data[0], data[1:]
                            if ch in (1, 2):
                                self._out(ch, payload)
                            elif ch == 3:
                                try:
                                    self.status = json.loads(payload)
                                except ValueError:
                                    self.status = {"message": payload[:300].decode("utf-8",
                                                                                   "replace")}
                                self.code = exit_of(self.status)
        except OSError:
            pass                       # the caller closed its end, or the stream broke
        finally:
            self.running, self.ended_at = False, time.monotonic()
            self.ws.close()
            try:
                self.inner.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.inner.close()


def _peer_gone(s: socket.socket) -> bool:
    """The other end of a socket pair reached its end (for an exec without stdin, whose caller
    never half closes: it closed)."""
    try:
        r, _, _ = select.select([s], [], [], 0)
        return bool(r) and s.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


# ====================================================================== the files: ACLs
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER = 1, 2, 4, 0x10, 0x20
_UNDEF = 0xFFFFFFFF


def _acl(entries) -> bytes:
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", t, p, i)
                                           for t, p, i in sorted(entries))


def grant(path: str, uids, *, dir_fd=None, name=None) -> bool:
    """rwx (files: rw, plus x where the owner has it) for `uids` on `path` by POSIX ACL, and for
    a folder the same as its default for what is made in it. False when the file system or the
    owner does not allow it."""
    target = name if dir_fd is not None else path
    try:
        st = os.stat(target, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    import stat as _st
    if _st.S_ISLNK(st.st_mode):
        return True
    m, is_dir = st.st_mode, _st.S_ISDIR(st.st_mode)
    perm = 7 if is_dir else (6 | ((m >> 6) & 1))
    acc = [(_ACL_USER_OBJ, (m >> 6) & 7, _UNDEF), (_ACL_GROUP_OBJ, (m >> 3) & 7, _UNDEF),
           (_ACL_MASK, 7, _UNDEF), (_ACL_OTHER, m & 7, _UNDEF)]
    acc += [(_ACL_USER, perm, u) for u in sorted(set(uids))]
    fd = None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else 0),
                     dir_fd=dir_fd)
        os.setxattr(fd, "system.posix_acl_access", _acl(acc))
        if is_dir:
            dflt = [(_ACL_USER_OBJ, 7, _UNDEF), (_ACL_GROUP_OBJ, 5, _UNDEF), (_ACL_MASK, 7, _UNDEF),
                    (_ACL_OTHER, 0, _UNDEF)] + [(_ACL_USER, 7, u) for u in sorted(set(uids))]
            os.setxattr(fd, "system.posix_acl_default", _acl(dflt))
        return True
    except (OSError, AttributeError):
        return False
    finally:
        if fd is not None:
            os.close(fd)


def has_grant(path: str, uid: int) -> bool:
    """The folder's default ACL names `uid` with rwx."""
    try:
        raw = os.getxattr(path, "system.posix_acl_default")
    except (OSError, AttributeError):
        return False
    for off in range(4, len(raw) - 7, 8):
        t, p, i = struct.unpack("<HHI", raw[off:off + 8])
        if t == _ACL_USER and i == uid and p == 7:
            return True
    return False


def grant_tree(root: str, uids) -> int:
    """`grant` on `root` and everything under it this process may change (by fds: a folder
    swapped for a link mid-walk is not followed). The number of entries granted."""
    n = int(grant(root, uids))
    for _, dirs, files, dfd in os.fwalk(root, follow_symlinks=False):
        for name in dirs + files:
            n += int(grant("", uids, dir_fd=dfd, name=name))
    return n


# ====================================================================== the sandboxes
def _labels_from(raw: str | None) -> dict:
    if not raw:
        return {"app.kubernetes.io/component": "chat-sandbox"}
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict):
            return {str(k): str(v) for k, v in doc.items()}
    except ValueError:
        pass
    out = {}
    for part in raw.split(","):
        k, _, v = part.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def _label_value(v: str) -> str:
    v = re.sub(r"[^A-Za-z0-9._-]", "-", v)[:63].strip("-._")
    return v or "x"


class KubeSandboxes(Sandboxes):
    """`Sandboxes` with pods for containers (the module's doc)."""

    def __init__(self, data_dir: str, *, api: Api | None = None, env=None, **kw):
        super().__init__(data_dir, **kw)
        env = os.environ if env is None else env
        self.engine = None                       # no container engine: the API is `self.api`
        self.api = api or Api(env=env)
        self.ns = self.api.namespace
        self.labels = _labels_from(env.get("QSE_K8S_LABELS"))
        self.pod_self = env.get("QSE_K8S_POD") or ""
        self.claim = env.get("QSE_K8S_CLAIM") or ""
        self.volume_root = os.path.realpath(env.get("QSE_K8S_VOLUME_ROOT") or
                                            os.path.dirname(os.path.realpath(data_dir)))
        self.runtime_class = env.get("QSE_K8S_RUNTIME_CLASS") or ""
        self.user_ns = (env.get("QSE_K8S_HOST_USERS") or "true").strip().lower() == "false"
        self.service_account = env.get("QSE_K8S_SERVICE_ACCOUNT") or ""
        self.uid = int(env.get("QSE_SANDBOX_UID") or 1000)
        self.cpus = float(env.get("QSE_SANDBOX_CPUS") or CPUS)
        self.pull = env.get("QSE_K8S_IMAGE_PULL") or "Never"
        self.egress_port = int(env.get("QSE_K8S_EGRESS_PORT") or 3128)
        self.egress_host = env.get("QSE_K8S_EGRESS_HOST") or ""
        self.controller_port = int(env.get("QSE_K8S_CONTROLLER_PORT") or 0)
        self.limit_bytes = _sb.LIMIT_BYTES
        self.execs: dict[str, Exec] = {}
        self.me: dict | None = None              # the controller's pod (name, uid, node, ip)
        self.egress: TcpProxy | None = None
        # the start check: state unknown (running), ok or error; `why` for the error
        self.check = {"state": "unknown", "why": "the cluster check has not finished", "at": 0.0,
                      "probe": None}
        self._check_lock = threading.Lock()
        self._pods_cache: tuple[float, list | None] = (0.0, None)
        self.sweep_every_s = 60.0
        self._swept = 0.0
        self.check_wait_s = 90.0

    # ------------------------------------------------------------------ names, selectors
    def name(self, cid: str) -> str:
        n = re.sub(r"[^a-z0-9-]", "-", cid.lower()).strip("-")
        full = "tandem-chat-" + n
        if len(full) > 63 or n != cid.lower():
            full = full[:50].rstrip("-") + "-" + hashlib.sha256(cid.encode()).hexdigest()[:12]
        return full

    def selector(self) -> str:
        sel = dict(self.labels)
        sel[INSTANCE_LABEL] = _label_value(self.instance)
        return ",".join(f"{k}={v}" for k, v in sorted(sel.items()))

    def list_pods(self, fresh: bool = False, probes: bool = False) -> list[dict] | None:
        """This chatd's pods (probe pods left out unless `probes`), at most every 2 s; None when
        the API fails."""
        now = time.monotonic()
        if not fresh and not probes and now - self._pods_cache[0] < 2.0 and \
                self._pods_cache[1] is not None:
            return self._pods_cache[1]
        try:
            st, doc = self.api.call("GET", self.api.pods("?labelSelector=" +
                                                         urllib.parse.quote(self.selector())))
        except OSError:
            return None
        if st != 200 or not isinstance(doc, dict):
            return None
        items = doc.get("items") or []
        if probes:
            return items
        pods = [p for p in items
                if PROBE_LABEL not in ((p.get("metadata") or {}).get("labels") or {})]
        self._pods_cache = (now, pods)
        return pods

    def _forget_pods(self) -> None:
        self._pods_cache = (0.0, None)

    @staticmethod
    def _cid_of(pod: dict) -> str | None:
        md = pod.get("metadata") or {}
        return (md.get("annotations") or {}).get(ID_ANNOTATION) or \
            (md.get("labels") or {}).get(LABEL)

    def get_pod(self, name: str) -> tuple[int, dict | None]:
        st, doc = self.api.call("GET", self.api.pods("/" + urllib.parse.quote(name)), timeout=10)
        return st, doc if isinstance(doc, dict) else None

    def delete_pod(self, name: str, grace: int = 0) -> int:
        try:
            st, _ = self.api.call("DELETE", self.api.pods(f"/{urllib.parse.quote(name)}"
                                                          f"?gracePeriodSeconds={grace}"),
                                  timeout=15)
        except OSError:
            return 0
        self._forget_pods()
        return st

    # ------------------------------------------------------------------ the start check
    def _whoami(self) -> None:
        if self.me is not None or not self.pod_self:
            return
        st, doc = self.get_pod(self.pod_self)
        if st == 200 and doc:
            self.me = {"name": doc["metadata"]["name"], "uid": doc["metadata"].get("uid"),
                       "node": (doc.get("spec") or {}).get("nodeName"),
                       "ip": (doc.get("status") or {}).get("podIP")}
            if not self.egress_host:
                self.egress_host = self.me["ip"] or ""
        else:
            raise Unavailable(f"chatd cannot read its own pod {self.pod_self} ({st})")

    def start_egress(self) -> None:
        if self.egress is None:
            self.egress = TcpProxy("0.0.0.0", self.egress_port, self._egress_lookup, self.log)
            self.egress.start()

    def _egress_lookup(self, ip: str):
        with self.lock:
            boxes = list(self.boxes.values())
        for b in boxes:
            if getattr(b, "ip", None) == ip and b.state in ("running", "creating"):
                return lambda cid=b.cid: self.network_mode(cid)
        return None

    def run_check(self) -> dict:
        """The cluster check (the module's doc): own pod, egress port, a dry-run create of a
        sandbox pod (runtimeClass, admission, quota), and the probe pod. Sets `check`."""
        with self._check_lock:
            try:
                self._whoami()
                self.start_egress()
                spec = self.pod_spec("probe-dry", probe=True)
                st, doc = self.api.call("POST", self.api.pods("?dryRun=All"), spec)
                if st not in (200, 201):
                    raise Unavailable(self._refusal(st, doc))
                probe = self._probe()
                self.check.update(probe=probe)
                if probe.get("controller") or probe.get("api"):
                    raise Unavailable("the cluster does not enforce NetworkPolicy: a sandbox pod "
                                      "reached " + " and ".join(
                                          k for k in ("controller", "api") if probe.get(k)) +
                                      ", so there are no sandboxes here (isolation is not "
                                      "enforced)")
                if probe.get("egress") is False:
                    self.log("[sandbox] a sandbox pod cannot reach chatd's egress port: sandboxes "
                             "have no network")
                self.check.update(state="ok", why="", at=time.monotonic())
                self.log(f"[sandbox] kubernetes: the cluster check passed ({json.dumps(probe)})")
            except (Unavailable, OSError) as exc:
                self.check.update(state="error", why=str(exc), at=time.monotonic())
                self.log(f"[sandbox] kubernetes: no sandboxes: {exc}")
            return dict(self.check)

    def _refusal(self, st: int, doc) -> str:
        msg = doc.get("message") if isinstance(doc, dict) else str(doc or "")
        if self.runtime_class and "RuntimeClass" in (msg or ""):
            return (f"the runtimeClass {self.runtime_class!r} does not exist in this cluster "
                    f"({st}: {(msg or '')[:200]})")
        return f"the cluster refuses a sandbox pod ({st}: {(msg or '')[:300]})"

    def _probe(self) -> dict:
        name = f"tandem-probe-{_label_value(self.instance)[:16].lower()}-{secrets.token_hex(3)}"
        spec = self.pod_spec(name, probe=True)
        port = self.controller_port or 8091
        spec["spec"]["containers"][0]["command"] = [
            "python3", "-c", PROBE_PY, self.egress_host, str(port), str(self.egress_port)]
        st, doc = self.api.call("POST", self.api.pods(), spec)
        if st not in (200, 201):
            raise Unavailable(self._refusal(st, doc))
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < self.check_wait_s:
                st, pod = self.get_pod(name)
                if st == 200 and pod:
                    phase = (pod.get("status") or {}).get("phase")
                    if phase in ("Succeeded", "Failed"):
                        break
                    why = self._stuck(pod)
                    if why:
                        raise Unavailable(why)
                time.sleep(1.0)
            else:
                raise Unavailable(f"the probe pod did not finish in {self.check_wait_s:g} s")
            st, log = self.api.call("GET", self.api.pods(f"/{name}/log?container={CONTAINER}"))
            text = log if isinstance(log, str) else json.dumps(log)
            for line in reversed(text.strip().splitlines()):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
            raise Unavailable(f"the probe pod said nothing readable ({text[:200]})")
        finally:
            self.delete_pod(name)

    def available(self) -> bool:
        """The cluster check passed; it runs in the background at start, again every 2 minutes
        while it fails."""
        st = self.check["state"]
        if st == "ok":
            return True
        if st == "unknown" or time.monotonic() - self.check["at"] > 120:
            if not self._check_lock.locked():
                if st != "unknown":
                    self.check["at"] = time.monotonic()
                threading.Thread(target=self.run_check, name="chatd-k8s-check",
                                 daemon=True).start()
        return False

    def unavailable_words(self) -> str:
        return self.check["why"] or "the cluster check has not finished"

    def status(self, cid: str) -> dict:
        st = super().status(cid)
        if st.get("error") == "the sandbox image is not installed on this box":
            st["error"] = self.unavailable_words()
        st["cpus"] = self.cpus
        return st

    # ------------------------------------------------------------------ the pod
    def pod_spec(self, name: str, cid: str | None = None, probe: bool = False) -> dict:
        labels = dict(self.labels)
        labels[INSTANCE_LABEL] = _label_value(self.instance)
        ann = {EGRESS_ANNOTATION: f"{self.egress_host}:{self.egress_port}"}
        if probe:
            labels[PROBE_LABEL] = "1"
        if cid is not None:
            labels[LABEL] = _label_value(cid)
            ann[ID_ANNOTATION] = cid
        proxy = f"http://{self.egress_host}:{self.egress_port}" if self.egress_host else ""
        env = {"HOME": "/home/agent", "TERM": "xterm-256color", "LANG": "C.UTF-8",
               "PATH": "/home/agent/.local/bin:/home/agent/.npm-global/bin:/usr/local/sbin:"
                       "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
               "npm_config_prefix": "/home/agent/.npm-global",
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONUNBUFFERED": "1"}
        if proxy:
            env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, http_proxy=proxy, https_proxy=proxy)
        mem = str(self.limit_bytes)
        mounts = [{"name": "tmp", "mountPath": "/tmp"}]
        volumes = [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "512Mi"}}]
        if cid is not None and self.claim:
            ws, home, _ = self.paths(cid)
            mounts += [{"name": "data", "mountPath": "/workspace", "subPath": self._rel(ws)},
                       {"name": "data", "mountPath": "/home/agent", "subPath": self._rel(home)}]
            volumes.append({"name": "data", "persistentVolumeClaim": {"claimName": self.claim}})
        else:
            mounts += [{"name": "ws", "mountPath": "/workspace"},
                       {"name": "home", "mountPath": "/home/agent"}]
            volumes += [{"name": "ws", "emptyDir": {"sizeLimit": "1Gi"}},
                        {"name": "home", "emptyDir": {"sizeLimit": "1Gi"}}]
        spec = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "shareProcessNamespace": True,
            "terminationGracePeriodSeconds": 3,
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["127.0.0.1"]},
            "securityContext": {"runAsUser": self.uid, "runAsGroup": self.uid,
                                "runAsNonRoot": True,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": CONTAINER, "image": self.image, "imagePullPolicy": self.pull,
                "command": ["sleep", "infinity"], "workingDir": "/workspace",
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "resources": {"limits": {"memory": mem, "cpu": f"{self.cpus:g}"},
                              "requests": {"memory": mem, "cpu": "100m"}},
                "securityContext": {"allowPrivilegeEscalation": False, "privileged": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": mounts}],
            "volumes": volumes,
        }
        if self.user_ns:
            spec["hostUsers"] = False
        if self.runtime_class:
            spec["runtimeClassName"] = self.runtime_class
        if self.service_account:
            spec["serviceAccountName"] = self.service_account
        md = {"name": name, "namespace": self.ns, "labels": labels, "annotations": ann}
        if self.me:
            if self.me.get("node"):
                spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution":
                                                     {"nodeSelectorTerms": [{"matchFields": [
                                                         {"key": "metadata.name", "operator": "In",
                                                          "values": [self.me["node"]]}]}]}}}
            if self.me.get("uid"):
                md["ownerReferences"] = [{"apiVersion": "v1", "kind": "Pod",
                                          "name": self.me["name"], "uid": self.me["uid"]}]
        return {"apiVersion": "v1", "kind": "Pod", "metadata": md, "spec": spec}

    def _rel(self, path: str) -> str:
        rel = os.path.relpath(os.path.realpath(path), self.volume_root)
        if rel.startswith(".."):
            raise Unavailable(f"the chat's folder {path} is not on the release's volume "
                              f"({self.volume_root})")
        return rel

    @staticmethod
    def _cstatus(pod: dict) -> dict:
        for c in (pod.get("status") or {}).get("containerStatuses") or []:
            if c.get("name") == CONTAINER:
                return c
        return {}

    def _stuck(self, pod: dict) -> str | None:
        """Why a pod that is not running never will, or None while it may still start."""
        st = pod.get("status") or {}
        w = (self._cstatus(pod).get("state") or {}).get("waiting") or {}
        reason, msg = w.get("reason") or "", (w.get("message") or "")[:200]
        if reason in ("ErrImageNeverPull", "ErrImagePull", "ImagePullBackOff",
                      "InvalidImageName"):
            return (f"the sandbox image {self.image} is not on this node ({reason}; build it "
                    f"there with ops/sandbox/build-k8s.sh)")
        if reason in ("CreateContainerConfigError", "CreateContainerError", "RunContainerError",
                      "CrashLoopBackOff"):
            return f"the sandbox pod cannot start ({reason}: {msg})"
        if st.get("phase") == "Failed":
            return f"the sandbox pod failed ({st.get('reason') or ''} {st.get('message') or ''})"
        return None

    @staticmethod
    def _pending_words(pod: dict) -> str | None:
        for c in (pod.get("status") or {}).get("conditions") or []:
            if c.get("type") == "PodScheduled" and c.get("status") == "False":
                return (c.get("message") or c.get("reason") or "not scheduled")[:240]
        return None

    def _files(self, cid: str) -> None:
        ws, home, _ = self.paths(cid)
        for d in (ws, home):
            os.makedirs(d, exist_ok=True)
            if not has_grant(d, self.uid):
                uids = (self.uid, os.getuid())
                n = grant_tree(d, uids)
                if not has_grant(d, self.uid):
                    self.log(f"[sandbox] {cid}: no ACL on {os.path.basename(d)} (the file system "
                             f"refuses it): the sandbox may not write its files")
                elif n > 1:
                    self.log(f"[sandbox] {cid}: {n} entries granted to the sandbox's uid")

    def _start(self, cid: str) -> str:
        if not self.claim:
            raise Unavailable("sandbox.mode kubernetes needs the release's claim "
                              "(QSE_K8S_CLAIM)")
        self._files(cid)
        b = self.box(cid)
        name = self.name(cid)
        st, pod = self.get_pod(name)
        if st == 200 and pod:
            labels = (pod.get("metadata") or {}).get("labels") or {}
            if labels.get(INSTANCE_LABEL) not in (None, _label_value(self.instance)):
                raise Unavailable(f"a sandbox pod of another chatd has this chat's name ({name})")
            if (pod.get("status") or {}).get("phase") == "Running" and \
                    (self._cstatus(pod).get("state") or {}).get("running") and \
                    (pod.get("metadata") or {}).get("annotations", {}).get(EGRESS_ANNOTATION) == \
                    f"{self.egress_host}:{self.egress_port}":
                b.ip = (pod.get("status") or {}).get("podIP")
                return name
            self._delete_wait(name)
        elif st not in (404,):
            raise Unavailable(f"the Kubernetes API answered {st} for the sandbox pod")
        spec = self.pod_spec(name, cid)
        for _ in range(3):
            st, doc = self.api.call("POST", self.api.pods(), spec)
            if st in (200, 201):
                break
            if st == 409:                          # the old one is still terminating
                self._delete_wait(name)
                continue
            raise Unavailable(self._refusal(st, doc))
        else:
            raise Unavailable(f"the sandbox pod {name} is still terminating")
        self._forget_pods()
        t0, pending = time.monotonic(), None
        while True:
            st, pod = self.get_pod(name)
            if st == 200 and pod:
                cs = self._cstatus(pod)
                if (pod.get("status") or {}).get("phase") == "Running" and \
                        (cs.get("state") or {}).get("running"):
                    b.ip = (pod.get("status") or {}).get("podIP")
                    return name
                why = self._stuck(pod)
                if why:
                    self.delete_pod(name)
                    raise Unavailable(why)
                pending = self._pending_words(pod) or pending
            elif st == 404:
                raise Unavailable("the sandbox pod was deleted while it started")
            if time.monotonic() - t0 > self.wait_s or self.stopping.is_set():
                self.delete_pod(name)
                spent = time.monotonic() - t0
                if pending:
                    raise Unavailable(
                        f"no sandbox for this call: the cluster has no room for it after "
                        f"{spent:.0f} s ({pending}). The command did not run. Do not call bash "
                        f"or render_page again for now: answer with what you already have, and "
                        f"tell the user the sandbox could not start for lack of capacity.",
                        {"sandbox": {"reason": "capacity", "why": pending,
                                     "waited_s": round(spent),
                                     "limit_gib": self.limit_bytes / GiB}})
                raise Unavailable(f"the sandbox pod did not start within {spent:.0f} s")
            time.sleep(0.5)

    def _delete_wait(self, name: str, wait: float = 30.0) -> None:
        self.delete_pod(name)
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            try:
                st, _ = self.get_pod(name)
            except OSError:
                return
            if st == 404:
                return
            time.sleep(0.5)

    def _alive(self, b: Box) -> bool:
        """The box's pod is still there and running. True when the API cannot say."""
        try:
            st, pod = self.get_pod(b.container or self.name(b.cid))
        except OSError:
            return True
        if st == 404:
            b.gone_why = None
            return False
        if st == 200 and pod:
            if (pod.get("status") or {}).get("phase") in ("Failed", "Succeeded") or \
                    (self._cstatus(pod).get("state") or {}).get("terminated"):
                b.gone_why = self._ended_words(pod)
                return False
        return True

    def _ended_words(self, pod: dict) -> str | None:
        st = pod.get("status") or {}
        if st.get("reason") == "Evicted":
            return ("the node evicted it: the node was short of memory (not this sandbox's own "
                    "limit)")
        t = (self._cstatus(pod).get("state") or {}).get("terminated") or {}
        if t.get("reason") == "OOMKilled":
            return f"a process passed its {_sb.limit_words()} memory limit and the pod was killed"
        return None

    def take_restarted(self, cid: str) -> str | None:
        words = super().take_restarted(cid)
        why = getattr(self.box(cid), "gone_why", None)
        if words and why:
            words = words[:-1] + f"; {why}]"
        return words

    def _proxy(self, b: Box) -> None:
        """No proxy per sandbox: the one TCP proxy knows each pod by its address."""
        self.start_egress()

    # ------------------------------------------------------------------ adopt, sweep
    def adopt(self) -> int:
        """At start: this chatd's running pods made for this egress address come back; every
        other pod of this chatd (finished, another controller pod's) is deleted. The cluster
        check starts in the background."""
        try:
            self._whoami()
        except (Unavailable, OSError) as exc:
            self.log(f"[sandbox] kubernetes: {exc}")
        try:
            self.start_egress()
        except OSError as exc:
            self.log(f"[sandbox] kubernetes: the egress port {self.egress_port}: {exc}")
        pods = self.list_pods(fresh=True, probes=True) or []
        here = f"{self.egress_host}:{self.egress_port}"
        n = 0
        for p in pods:
            md = p.get("metadata") or {}
            if PROBE_LABEL in (md.get("labels") or {}):
                self.delete_pod(md.get("name", ""))     # a check a previous chatd left
                continue
            cid = self._cid_of(p)
            running = (p.get("status") or {}).get("phase") == "Running" and \
                (self._cstatus(p).get("state") or {}).get("running")
            if cid and running and (md.get("annotations") or {}).get(EGRESS_ANNOTATION) == here:
                b = self.box(cid)
                b.container, b.ip = md.get("name"), (p.get("status") or {}).get("podIP")
                b.state, b.started, b.last_used = "running", self.clock(), self.clock()
                n += 1
            else:
                self.delete_pod(md.get("name", ""))
                self.log(f"[sandbox] removed a stale sandbox pod {md.get('name')}")
        self.available()
        return n

    def sweep(self) -> list[str]:
        """Pods of this chatd with no running sandbox here (a stop that did not reach the API, a
        chat deleted meanwhile, a pod made by a chatd that is gone): deleted. Their names."""
        pods = self.list_pods(fresh=True)
        out = []
        for p in pods or []:
            md = p.get("metadata") or {}
            cid = self._cid_of(p)
            with self.lock:
                b = self.boxes.get(cid) if cid else None
            if b is not None and b.state in ("running", "creating", "waiting_memory"):
                continue
            self.delete_pod(md.get("name", ""))
            out.append(md.get("name", ""))
        if out:
            self.log(f"[sandbox] removed {len(out)} sandbox pod(s) with no chat running here")
        return out

    # ------------------------------------------------------------------ limits
    def _avail_gb(self) -> float:
        return 0.0

    def admit(self) -> str | None:
        """None when a new sandbox may start now (count and total from the cluster's pods)."""
        running = self._running()
        pods = self.list_pods()
        live = [p for p in (pods or []) if (p.get("status") or {}).get("phase") in
                ("Pending", "Running") and not (p.get("metadata") or {}).get("deletionTimestamp")]
        n = max(len(running), len(live) if pods is not None else 0)
        if (n + 1) * self.limit_bytes / GiB > self.total_gb or n >= self.max_running:
            idle = sorted((b for b in running if not b.busy), key=lambda b: b.last_used)
            if idle:
                self.stop(idle[0].cid, "idle")
                return None if n - 1 < self.max_running else "making room"
            return f"{n} sandbox(es) of other chats are running commands, the most at once"
        return None

    def guard_once(self) -> str | None:
        """Idle pods stop; the sweep once a minute. No memory floor here (the module's doc)."""
        now = self.clock()
        if time.monotonic() - self._swept > self.sweep_every_s:
            self._swept = time.monotonic()
            try:
                self.sweep()
            except Exception as exc:                                # noqa: BLE001
                self.log(f"[sandbox] sweep {type(exc).__name__}")
        for b in self._running():
            if not b.busy and now - b.last_used > _sb.IDLE_S:
                self.stop(b.cid, "idle")
                return f"idle {b.cid}"
        return None

    def ensure(self, cid: str, cancel: threading.Event | None = None, on_wait=None) -> str:
        if not self.available():
            raise Unavailable(f"no sandbox: {self.unavailable_words()}")
        return super().ensure(cid, cancel=cancel, on_wait=on_wait)

    # ------------------------------------------------------------------ stop, remove
    def stop(self, cid: str, reason: str = "user") -> None:
        b = self.box(cid)
        if b.state in ("running", "creating", "error") or b.container:
            self.delete_pod(b.container or self.name(cid), grace=3)
            if b.state == "running":
                self.log(f"[sandbox] {cid} stopped ({reason})")
        b.state, b.stopped_reason = "stopped", reason
        b.ip = None
        self._changed(b)

    def kill_all(self, reason: str = "memory_guard") -> None:
        for b in self._running():
            self.delete_pod(b.container or self.name(b.cid), grace=0)
            b.state, b.stopped_reason, b.ip = "stopped", reason, None
            self._changed(b)
        self.log(f"[sandbox] every sandbox killed ({reason})")

    def remove(self, cid: str, files: bool = False) -> None:
        self.stop(cid, "user")
        b = self.box(cid)
        try:
            st, pod = self.get_pod(self.name(cid))
            if st == 200 and pod and ((pod.get("metadata") or {}).get("labels") or {}).get(
                    INSTANCE_LABEL) in (None, _label_value(self.instance)):
                self.delete_pod(self.name(cid))
        except OSError:
            pass
        b.container = None
        if files:
            import shutil
            for d in self.paths(cid):
                shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------------ exec
    def _exec_path(self, pod: str, argv: list[str], tty: bool, stdin: bool) -> str:
        q = [("container", CONTAINER), ("stdout", "true"), ("stderr", "false" if tty else "true"),
             ("stdin", "true" if stdin else "false"), ("tty", "true" if tty else "false")]
        q += [("command", a) for a in argv]
        return self.api.pods(f"/{urllib.parse.quote(pod)}/exec?" + urllib.parse.urlencode(q))

    def _prune(self) -> None:
        now = time.monotonic()
        for k, e in list(self.execs.items()):
            if not e.running and e.ended_at is not None and now - e.ended_at > 600:
                self.execs.pop(k, None)

    def open_exec(self, cid: str, cmd: list[str], *, tty: bool = False, stdin: bool = False,
                  env: list[str] | None = None, workdir: str = "/workspace"
                  ) -> tuple[str, socket.socket, bytes]:
        b = self.box(cid)
        pod = b.container or self.name(cid)
        exec_id = secrets.token_hex(8)
        argv = ["/bin/sh", "-c", WRAP, "tandem-exec", exec_id, workdir,
                str(PIDS if self.user_ns else 0)] + (["env", *env] if env else []) + list(cmd)
        try:
            ws = self.api.websocket(self._exec_path(pod, argv, tty, stdin))
        except ApiError as exc:
            text = (exc.message or "").lower()
            if exc.status == 404 or "not found" in text or "not running" in text or \
                    "completed pod" in text:
                self.gone(cid)
                raise Gone("the sandbox's pod was gone (deleted, or it ended); the next call "
                           "starts a new one, with the files kept") from None
            raise Unavailable(f"exec failed ({exc.status}: {(exc.message or '')[:200]})") \
                from None
        except OSError as exc:
            raise Unavailable(f"exec failed: {exc}") from None
        e = Exec(ws, tty, stdin, pod, self.log)
        self._prune()
        self.execs[exec_id] = e
        return exec_id, e.outer, b""

    def exit_code(self, exec_id: str) -> tuple[bool, int | None, int | None]:
        e = self.execs.get(exec_id)
        if e is None:
            return False, None, None
        if not e.running and e.code is None:
            return False, -1, None
        return e.running, e.code, None

    def _signal(self, exec_id: str, mode: str) -> None:
        """A second exec in the same pod signals the first one's pid (KILL_PY)."""
        e = self.execs.get(exec_id)
        if e is None or not e.running or not e.pod:
            return
        try:
            ws = self.api.websocket(self._exec_path(
                e.pod, ["python3", "-c", KILL_PY, f"{PIDDIR}/{exec_id}", mode], False, False))
        except OSError:
            return
        k = Exec(ws, False, False, e.pod)
        k.thread.join(15)
        k.outer.close()

    def _kill_exec(self, exec_id: str) -> None:
        """A run's timeout or cancel: its process tree, TERM then KILL."""
        self._signal(exec_id, "tree")

    def end_exec(self, exec_id: str) -> None:
        """TERM to an exec's own process when it still runs."""
        self._signal(exec_id, "one")

    def resize(self, exec_id: str, cols: int, rows: int) -> None:
        e = self.execs.get(exec_id)
        if e is not None and e.running:
            try:
                e.resize(cols, rows)
            except OSError:
                pass

    def capture(self, cid: str, argv: list[str], timeout: float = 10.0) -> tuple[int | None, str]:
        """A short command's (exit code, stdout), outside the box's busy count."""
        exec_id, sock, _ = self.open_exec(cid, argv)
        out = bytearray()
        try:
            _sb.demux(sock, b"", lambda s, d: out.extend(d) if s == 1 else None,
                      first_timeout=timeout)
        finally:
            sock.close()
        e = self.execs.get(exec_id)
        if e is not None:
            e.thread.join(2)
        return (e.code if e else None), out.decode("utf-8", "replace")

    def run(self, container_or_cid: str, cmd: list[str], *, timeout: float = 600,
            on_output=None, cancel: threading.Event | None = None) -> tuple[int, bool]:
        code, timed_out = super().run(container_or_cid, cmd, timeout=timeout,
                                      on_output=on_output, cancel=cancel)
        if code in (137, -1, None) and not timed_out and not (cancel and cancel.is_set()):
            # a kill at the limit ends the whole pod; give its status a moment to say so
            b = self.box(container_or_cid)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 6:
                try:
                    st, pod = self.get_pod(b.container or self.name(b.cid))
                except OSError:
                    break
                if st != 200 or not pod or (self._cstatus(pod).get("state") or {}).get(
                        "terminated") or (pod.get("status") or {}).get("phase") in ("Failed",
                                                                                   "Succeeded"):
                    break
                if time.monotonic() - t0 > 3:
                    break
                time.sleep(0.5)
        return code, timed_out

    # ------------------------------------------------------------------ memory, OOM
    def memory_used(self, b: Box) -> int | None:
        """The pod's memory in use (its cgroup's memory.current less inactive_file), read
        through an exec at most every 15 s."""
        if b.state != "running" or not b.container:
            return None
        now = self.clock()
        if now - b.mem[0] < 15.0:
            return b.mem[1]
        used = None
        try:
            code, out = self.capture(b.cid, ["cat", "/sys/fs/cgroup/memory.current",
                                             "/sys/fs/cgroup/memory.stat"], timeout=5)
            lines = out.split("\n")
            cur = int(lines[0].strip())
            inactive = 0
            for ln in lines[1:]:
                k, _, v = ln.partition(" ")
                if k == "inactive_file":
                    inactive = int(v)
            used = max(0, cur - inactive)
        except (Unavailable, OSError, ValueError, IndexError):
            used = None
        b.mem = (now, used)
        return used

    def oom_kills(self, cid: str) -> int | None:
        """Kills at the memory limit: the pod's container ended OOMKilled (each one counted
        once; the whole pod ends when the kubelet sets memory.oom.group), plus, while the pod
        runs, its cgroup's own count (a node that kills one process only)."""
        b = self.box(cid)
        if not hasattr(b, "ooms"):
            b.ooms, b.oom_seen, b.cg_ooms = 0, set(), 0
        try:
            st, pod = self.get_pod(b.container or self.name(cid))
        except OSError:
            return None
        if st != 200 or not pod:
            return b.ooms + b.cg_ooms
        cs = self._cstatus(pod)
        for t in ((cs.get("state") or {}).get("terminated"),
                  (cs.get("lastState") or {}).get("terminated")):
            if t and t.get("reason") == "OOMKilled":
                key = (pod["metadata"].get("uid"), t.get("containerID"), t.get("finishedAt"))
                if key not in b.oom_seen:
                    b.oom_seen.add(key)
                    b.ooms += 1
        if (cs.get("state") or {}).get("running"):
            try:
                _, out = self.capture(cid, ["cat", "/sys/fs/cgroup/memory.events"], timeout=5)
                for ln in out.split("\n"):
                    k, _, v = ln.partition(" ")
                    if k == "oom_kill":
                        b.cg_ooms = int(v)
            except (Unavailable, OSError, ValueError):
                pass
        return b.ooms + b.cg_ooms
