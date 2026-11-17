"""Pieces of the sign-in that more than one module reads (SEC-28).

The accounts, their sessions, API tokens and service identities decide who may do what
(proxy/accounts.py, proxy/authz.py, proxy/tokens.py). There is no shared admin token, no open
default and no dashboard login switch: a bare process bound to loopback with no owner password
runs in local mode (proxy/authlocal.py); everything else needs a session or a token.

What stays here:

    TrustedProxies       TANDEM_TRUSTED_PROXY: the proxies whose X-Real-IP and
                         X-Forwarded-Proto count (addresses and CIDR ranges)
    loopback_host        a Host header that names this machine's loopback
    LoginLimit           five failed sign-ins a minute from one address, then 429 for a minute;
                         the address is `login_key`'s, never a raw X-Real-IP from a stranger
    same_origin(_json)   a write the page itself sent (the engine keeps an equal copy;
                         tests/test_engine_api_contract.py holds the two equal)
    MIN_TOKEN            the shortest secret a pairing accepts
"""

from __future__ import annotations

import collections
import ipaddress
import threading
import time

MIN_TOKEN = 32
LOGIN_FAILS = 5
LOGIN_WINDOW_S = 60.0
LOGIN_MAX_KEYS = 10_000               # a memory bound on `_fails`, not a ban reset
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
TRUSTED_PROXY_ENV = "TANDEM_TRUSTED_PROXY"


class TrustedProxies:
    """TANDEM_TRUSTED_PROXY: the proxies whose X-Real-IP and X-Forwarded-Proto count, as a comma
    list of addresses and CIDR ranges, IPv4 and IPv6 (`192.0.2.10, 10.0.0.0/8, 2001:db8::/32`).
    The one reader of the variable: every check asks `peer in proxies`. An entry that is
    neither an address nor a range is ignored and kept in `bad` (the health page's
    trusted_proxy_invalid, a line in the start log); nothing here raises. An IPv4-mapped IPv6
    peer (`::ffff:192.0.2.10`, a dual-stack listener) matches its IPv4 entry."""

    def __init__(self, value=""):
        if isinstance(value, TrustedProxies):
            self.networks, self.bad = list(value.networks), list(value.bad)
            return
        items = value.split(",") if isinstance(value, str) else (value or ())
        self.networks: list = []
        self.bad: list[str] = []
        for item in items:
            s = str(item or "").strip()
            if not s:
                continue
            try:
                self.networks.append(ipaddress.ip_network(s.strip("[]"), strict=False))
            except ValueError:
                self.bad.append(s[:80])

    @classmethod
    def from_env(cls, env) -> "TrustedProxies":
        return cls(env.get(TRUSTED_PROXY_ENV) or "")

    def __contains__(self, peer) -> bool:
        try:
            ip = ipaddress.ip_address(str(peer or "").strip().strip("[]").split("%", 1)[0])
        except ValueError:
            return False
        ips = [ip]
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ips.append(ip.ipv4_mapped)
        return any(a in n for a in ips for n in self.networks if a.version == n.version)

    def __bool__(self) -> bool:
        return bool(self.networks)


def loopback_host(host: str) -> bool:
    """A Host header that names this machine's loopback: localhost, *.localhost, 127.x, ::1
    (with or without a port). An empty one (HTTP/1.0 tools) counts."""
    h = (host or "").strip().lower()
    if not h:
        return True
    if h.startswith("["):
        h = h[1:h.find("]")] if "]" in h else h
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    if h == "localhost" or h.endswith(".localhost") or h == "::1":
        return True
    return h.startswith("127.") and all(p.isdigit() for p in h.split(".")) and h.count(".") == 3


class LoginLimit:
    """Failed sign-ins per address: LOGIN_FAILS a minute (or `fails`), then `login_blocked` for
    the rest of the window. Used by proxy/accounts.py SignInLimits and its wrong-token limit."""

    def __init__(self, trusted_proxies=(), *, clock=time.time, fails: int = 0):
        self.clock = clock
        self.fails = fails or LOGIN_FAILS
        self.trusted_proxies = TrustedProxies(trusted_proxies)
        self._fails: dict = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def login_blocked(self, who: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._fails[who]
            while q and now - q[0] > LOGIN_WINDOW_S:
                q.popleft()
            return len(q) >= self.fails

    def login_key(self, peer: str, headers) -> str:
        """The throttle key for one login attempt -- the one rule BOTH servers run.
        `X-Real-IP` is the client's to write, so it counts ONLY from a proxy this box
        trusts: loopback by default (the documented single-box shape -- an nginx on this
        box fronts the server and every real client shares the 127.0.0.1 peer; keying on
        the peer there lets one user's five wrong passwords lock out every user), plus
        TANDEM_TRUSTED_PROXY naming a proxy's address or range for the LAN shape
        (TrustedProxies, docs/proxy.md).
        From any other peer the header is ignored and the peer itself is the key.
        Never both at once: a spoofable value must not merge with real IPs. A trusted
        proxy that sends no header leaves the attempt keyed on the peer."""
        if peer in self.trusted_proxies or peer in LOOPBACK:
            v = (headers.get("X-Real-IP") or "").strip() if headers is not None else ""
            if v:
                return v
        return peer

    def login_failed(self, who: str) -> None:
        now = self.clock()
        with self._lock:
            self._fails[who].append(now)
            if len(self._fails) <= LOGIN_MAX_KEYS:
                return
            # the old `self._fails.clear()` handed an attacker a free pass: a flood of
            # one-shot keys (one per rotating peer) wiped every ACTIVE ban at the moment
            # the table overflowed. Stale keys go first -- the window prunes them away --
            # then the OLDEST-SEEN keys, one bucket at a time; the cap is a memory bound,
            # never a reset of who is banned.
            for key, q in list(self._fails.items()):
                while q and now - q[0] > LOGIN_WINDOW_S:
                    q.popleft()
                if not q:
                    del self._fails[key]
            if len(self._fails) > LOGIN_MAX_KEYS:
                # one sweep per 10 % of the table, not one per login: at the cap every
                # further failed login would otherwise rescan and sort all 10k keys
                # under the lock (2 ms each, measured)
                banned = {k for k, q in self._fails.items() if len(q) >= LOGIN_FAILS}
                oldest = sorted(self._fails, key=lambda k: self._fails[k][0])
                target = LOGIN_MAX_KEYS - LOGIN_MAX_KEYS // 10
                excess = len(self._fails) - target
                drop = [k for k in oldest if k not in banned][:excess]   # spare the active
                seen = set(drop)   # a set: with most keys banned, scanning the list per
                drop += [k for k in oldest if k not in seen][:excess - len(drop)]  # key is
                for key in drop:                                         # millions of scans
                    del self._fails[key]


# ---------------------------------------------------------------------- a write from the page itself
# Copied from server/setup_api.py (SRV-747: TandemApp does not import the engine; the engine keeps
# its own for the Setup Guide's routes, and tests/test_engine_api_contract.py holds the two equal).

def same_origin_json(headers) -> tuple[int, str, str] | None:
    """None for a POST the page itself sent, else (status, code, message): the body must be
    declared JSON (a form or text/plain POST needs no CORS preflight), a browser's
    `Sec-Fetch-Site: cross-site` is refused, and an `Origin` must name this server's host (the
    `Host` the browser sent, which nginx passes on as $host, or `X-Forwarded-Host`). A tool
    without a browser sends no Origin and no Sec-Fetch-Site and passes on its JSON alone."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, "json_only", "the setup routes take a JSON body (Content-Type: application/json)"
    return same_origin(headers)


def same_origin(headers) -> tuple[int, str, str] | None:
    """same_origin_json without the body's type: for a write whose body may be empty (a DELETE)
    or that a tool sends without one (proxy/app.py, every /v1/fleet/* write)."""
    from urllib.parse import urlsplit
    if (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return 403, "cross_site", "a request from another site cannot change the setup"
    origin = headers.get("Origin")
    if origin is not None:
        o = urlsplit(origin.strip()) if origin.strip() != "null" else None
        hosts = {x.strip().lower() for x in (headers.get("Host"), headers.get("X-Forwarded-Host"))
                 if x}
        if o is None or not o.netloc or o.netloc.lower() not in hosts:
            return 403, "cross_origin", "a request from another origin cannot change the setup"
    return None
