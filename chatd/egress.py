"""The sandbox's way out: the internet, never the LAN or this box (SRV-355, Chat 6).

A sandbox container runs with `--network none`: it has a loopback and nothing else, so no packet of
its own reaches the LAN, the router, the engine on :8000 or chatd. Its way out is an HTTP proxy:
inside the container `relay.py` listens on 127.0.0.1:3128 (HTTP_PROXY and HTTPS_PROXY point
there) and passes every connection to a unix socket that chatd serves for that chat only
(`<data>/egress/<conversation>/proxy.sock`, mounted read-only at /run/tandem). Here each request
is checked before anything is connected:

- the host is resolved here, and the connection goes to the address that was checked (a name that
  resolves to a public address once and a private one later cannot slip through);
- every address must be global: private (10/8, 172.16/12, 192.168/16, fc00::/7), loopback,
  link-local, CGNAT (100.64/10), multicast, reserved and the unspecified ranges are refused, IPv4
  mapped into IPv6 included; one refused address refuses the name;
- `CONNECT host:port` tunnels TLS (pip, npm, git over https, curl); an absolute `http://` request
  is passed on with `Connection: close`;
- a chat whose network is `off` gets 403 for everything.

This replaces the plan's nftables rule for the `tandem-sbx` user (that needs root, which this box's
chatd does not have): the container cannot open a socket to anything but its own loopback, which is
stricter than a rule that drops what it sends. `fetch()` is the same check for chatd's own
web_fetch tool. `request()` is the general form: a host list (only those hosts, redirects
included), and for a host on that list the user configured (a SearXNG instance), a private address
allowed. Logs carry counts, never a host or a path.

A PLUGIN'S SANDBOX (SRV-859) uses the same proxy with a host list instead of the internet: its
`mode()` answers an `Allow` (the hosts of the plugin's granted `network:` capabilities). Then only
those hosts (and ports, when the capability names one) are reached, a granted host may be on the
LAN, and nothing else is: no other LAN address, no public host it was not granted. A loopback,
link-local, unspecified or multicast address, and the Kubernetes API's address, are refused even
when a capability names them: they are this box, the cloud's metadata or the cluster itself.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import select
import socket
import ssl
import threading
import time
import urllib.parse

CHUNK = 65536
CONNECT_TIMEOUT_S = 15.0
SPLICE_IDLE_TIMEOUT_S = 300.0        # a tunnel with nothing either way this long is half-open:
                                     # the pump ends on it and both sockets close
MAX_FETCH = 4 * 1024 * 1024


class Refused(Exception):
    pass


def never(addr: str) -> bool:
    """An address no grant opens: loopback, link-local (the cloud's metadata), unspecified,
    multicast, and the Kubernetes API's own address."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return True
    api = (os.environ.get("KUBERNETES_SERVICE_HOST") or "").strip("[]")
    return bool(api) and str(ip) == api


class Allow:
    """A plugin sandbox's way out: only these `host` or `host:port` names (its granted
    `network:` capabilities), a private address allowed for them, `never` addresses refused."""

    def __init__(self, hosts):
        self.hosts = frozenset(str(h).lower() for h in hosts or ())

    def permits(self, host: str, port: int) -> bool:
        host = (host or "").lower().strip("[]").rstrip(".")
        return host in self.hosts or f"{host}:{port}" in self.hosts

    def connect(self, host: str, port: int) -> socket.socket:
        if not self.permits(host, port):
            raise Refused(f"{host}:{port} is not a host this plugin was granted")
        for fam, _, _, _, sa in resolve(host, port, private_ok=True):
            if never(sa[0]):
                raise Refused(f"{host} resolves to an address no plugin may reach")
        return connect(host, port, private_ok=True)


def allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def resolve(host: str, port: int, private_ok: bool = False) -> list[tuple]:
    """The addresses to connect to, every one checked; Refused when any is not public (unless
    `private_ok`: a host the user named, such as their own SearXNG instance)."""
    if not host or port <= 0 or port > 65535:
        raise Refused("bad host or port")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refused(f"cannot resolve {host}") from None
    if not infos:
        raise Refused(f"cannot resolve {host}")
    for fam, _, _, _, sa in infos:
        if not private_ok and not allowed(sa[0]):
            raise Refused(f"{host} is a local network address; only the public internet is "
                          f"reachable")
    return infos


def connect(host: str, port: int, private_ok: bool = False,
            timeout: float = CONNECT_TIMEOUT_S) -> socket.socket:
    last = None
    for fam, typ, proto, _, sa in resolve(host, port, private_ok):
        s = socket.socket(fam, typ, proto)
        s.settimeout(timeout)
        try:
            s.connect(sa)
            s.settimeout(None)
            return s
        except OSError as exc:
            last = exc
            s.close()
    raise Refused(f"cannot connect to {host}:{port} ({type(last).__name__})")


def _hostport(u: urllib.parse.SplitResult) -> tuple[str, int]:
    return (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


def request(url: str, *, method: str = "GET", body: bytes | None = None,
            headers: dict | None = None, hosts: set | frozenset | None = None,
            private_ok: bool = False, timeout: float = 30.0, max_bytes: int = MAX_FETCH,
            redirects: int = 5) -> tuple[int, str, bytes]:
    """One HTTP request through the check: (status, content type, body). `hosts`: the only
    `host` or `host:port` names it may reach, redirects checked again; None: any public host.
    `private_ok` (with `hosts` only): those hosts may resolve to a private address."""
    private_ok = private_ok and hosts is not None
    for _ in range(redirects + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise Refused("only http and https URLs")
        host, port = _hostport(u)
        if hosts is not None and host not in hosts and f"{host}:{port}" not in hosts:
            raise Refused(f"{host} is not a host this request may reach")
        sock = connect(u.hostname, port, private_ok, min(CONNECT_TIMEOUT_S, timeout))
        try:
            if u.scheme == "https":
                ctx = ssl.create_default_context()
                ctx.set_alpn_protocols(["http/1.1"])          # as http.client offers it
                sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
            sock.settimeout(timeout)
            conn = http.client.HTTPConnection(u.hostname, port)
            conn.sock = sock
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                url = urllib.parse.urljoin(url, r.getheader("Location"))
                if r.status in (301, 302, 303):
                    method, body = "GET", None
                continue
            data = r.read(max_bytes)
            return r.status, (r.getheader("Content-Type") or "").split(";")[0], data
        except (TimeoutError, socket.timeout):
            raise Refused(f"no answer from {host} within {timeout:g} s") from None
        except ssl.SSLError as exc:
            raise Refused(f"TLS with {host} failed ({exc.reason or type(exc).__name__})") from None
        except (OSError, http.client.HTTPException) as exc:
            # a reset in the handshake or mid-answer (a rate limit can look like this)
            raise Refused(f"the connection to {host} failed ({type(exc).__name__})") from None
        finally:
            sock.close()
    raise Refused("too many redirects")


def fetch(url: str, max_bytes: int = MAX_FETCH, redirects: int = 5) -> tuple[int, str, bytes]:
    """GET a public URL with the same check (web_fetch); redirects are checked again."""
    return request(url, headers={"User-Agent": "TandemLLM-chat/0.1 (web_fetch)",
                                 "Accept": "text/html,text/plain,*/*;q=0.8"},
                   max_bytes=max_bytes, redirects=redirects)


class Proxy:
    """One chat's proxy on its unix socket; `mode()` is asked per connection (on or off)."""

    def __init__(self, path: str, mode, log=None):
        self.path = path
        self.mode = mode
        self.log = log or (lambda line: None)
        self.sock: socket.socket | None = None
        self.stats = {"connections": 0, "refused": 0}

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o666)
        s.listen(64)
        self.sock = s
        threading.Thread(target=self._accept, name="chatd-egress", daemon=True).start()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept(self) -> None:
        while self.sock is not None:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        serve(c, self.mode, self.stats)


class TcpProxy:
    """The same proxy on one TCP port for every sandbox of a cluster (sandbox.mode kubernetes,
    chatd/k8s.py): a sandbox pod has no unix socket of chatd's, only the pod network, and a
    NetworkPolicy lets it reach this port and nothing else. `lookup(peer address)` names the
    connection's sandbox and gives its mode function, or None: an address that is not a running
    sandbox's pod is refused before anything is read."""

    def __init__(self, host: str, port: int, lookup, log=None):
        self.host, self.port = host, port
        self.lookup = lookup
        self.log = log or (lambda line: None)
        self.sock: socket.socket | None = None
        self.stats = {"connections": 0, "refused": 0, "unknown": 0}

    def start(self) -> None:
        fam = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(128)
        self.port = s.getsockname()[1]
        self.sock = s
        threading.Thread(target=self._accept, name="chatd-egress-tcp", daemon=True).start()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _accept(self) -> None:
        while self.sock is not None:
            try:
                c, peer = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c, peer[0]), daemon=True).start()

    def _serve(self, c: socket.socket, peer: str) -> None:
        try:
            mode = self.lookup(peer.split("%")[0].removeprefix("::ffff:"))
        except Exception:                                           # noqa: BLE001
            mode = None
        if mode is None:
            self.stats["unknown"] += 1
            msg = b"not a sandbox of this chatd\n"
            try:
                c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                          b"Content-Length: " + str(len(msg)).encode() +
                          b"\r\nConnection: close\r\n\r\n" + msg)
                # what the client sent is read, not left behind: a close with unread bytes is a
                # reset, and the client could lose the answer
                c.shutdown(socket.SHUT_WR)
                c.settimeout(2)
                while c.recv(CHUNK):
                    pass
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
            return
        serve(c, mode, self.stats)


def serve(c: socket.socket, mode, stats: dict) -> None:
    """One client connection of a sandbox: its request line checked, then tunnelled or passed
    on (see the module's doc). `mode()` is the chat's network: on, everything refused, or a
    plugin's `Allow` (only its granted hosts)."""
    stats["connections"] += 1
    up = None
    try:
        c.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            d = c.recv(CHUNK)
            if not d:
                return
            head += d
            if len(head) > 65536:
                return
        line, _, rest = head.partition(b"\r\n")
        method, target, _ = (line.decode("latin-1").split(" ") + ["", "", ""])[:3]
        m = mode()
        allow = m if isinstance(m, Allow) else None
        if allow is None and m != "on":
            raise Refused("the network is off for this chat")
        dial = allow.connect if allow is not None else connect
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            up = dial(host.strip("[]"), int(port or 443))
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            extra = rest.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in rest else b""
            if extra:
                up.sendall(extra)
        else:
            u = urllib.parse.urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                raise Refused("send absolute http:// URLs or CONNECT")
            up = dial(u.hostname, u.port or 80)
            hdrs, _, body = rest.partition(b"\r\n\r\n")
            keep = [h for h in hdrs.split(b"\r\n") if h and not h.lower().startswith(
                (b"proxy-", b"connection:", b"keep-alive:"))]
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            up.sendall(f"{method} {path} HTTP/1.1\r\n".encode() + b"\r\n".join(keep) +
                       b"\r\nConnection: close\r\n\r\n" + body)
        c.settimeout(None)
        _splice(c, up)                       # the pumps carry the idle timeout
    except (Refused, ValueError) as exc:
        stats["refused"] += 1
        msg = str(exc).encode()
        try:
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: "
                      + str(len(msg) + 1).encode() + b"\r\nConnection: close\r\n\r\n" + msg +
                      b"\n")
        except OSError:
            pass
    except OSError:
        pass
    finally:
        for s in (c, up):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def _splice(a: socket.socket, b: socket.socket,
            idle: float = SPLICE_IDLE_TIMEOUT_S) -> None:
    """Pump both ways until either side ends. The idle clock is SHARED by the two pumps:
    traffic in either direction feeds it, so a download — a client that sends nothing for
    minutes while upstream streams a pip/Hugging Face/git/SSE transfer — is not read as an
    idle tunnel. Only a tunnel both directions have stopped speaking through for `idle`
    dies, which is the half-open case this exists for."""
    last = [time.monotonic()]                # one shared cell: both pumps feed it

    def pump(src, dst):
        try:
            while True:
                # The poll is a select, not a socket timeout: a 1 s timeout set for this
                # pump's recv covered the same socket's sendall in the other pump, so a
                # client that pauses reading for longer than it lost a live download.
                # The sockets stay blocking — a blocked sendall waits the pause out as
                # backpressure — and select answers the poll.
                r, _, _ = select.select([src], [], [], min(1.0, idle))
                if not r:
                    if time.monotonic() - last[0] > idle:
                        break
                    continue
                d = src.recv(CHUNK)
                if not d:
                    break
                last[0] = time.monotonic()
                dst.sendall(d)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join(600)
