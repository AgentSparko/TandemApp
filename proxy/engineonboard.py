"""Adding an engine host to the fleet (SRV-829): pairing by code, behind /v1/fleet/engines.

    o = Onboarding(fleet)                         one per controller (Fleet.engines())
    o.handle(method, parts, query, body, who)     the routes below; OnboardError for a refusal

    GET  /v1/fleet/engines                        the engine hosts this controller reaches over SSH
    GET  /v1/fleet/engines/{id}                   one of them, with its `status` over SSH
    POST /v1/fleet/engines/discover {seconds?, interface?}
                                                  browse the LAN for `_tandem._tcp` (SRV-827);
                                                  GET answers the last browse
    POST /v1/fleet/engines/probe {address, port?} one address typed by hand (a tailnet's too):
                                                  does an engine answer, is a window open
    POST /v1/fleet/engines/pair/start {address, port?, fingerprint?, name?}
                                                  the engine's pairing window read (GET
                                                  /v1/engine/pair); a pairing id and a new key
                                                  pair kept in this process's memory
    POST /v1/fleet/engines/pair/code {pairing, code}
                                                  the exchange (proxy/enginepair.py); on success
                                                  the host is in the fleet: `state` paired
    POST /v1/fleet/engines/pair/abandon {pairing} the started pairing and its key pair forgotten

FINDING THE ENGINE (SRV-827). `discover` asks the LAN once (proxy/mdns.py's Browser: a legacy
unicast query from an ephemeral port, so nothing binds 5353; IPv4) for `_tandem._tcp.local` and
lists each instance with what its TXT says: `name`, `version`, `port` (the engine's HTTP port) and
`fp` (the SSH host key's fingerprint, the one `tandem pair` prints), and the addresses its A records
name. Nothing else of the TXT is passed on. An advertisement proves nothing: each row is
`verified` false, and the fingerprint it shows is proven only by the code (pairing checks it
against the window's) or by the user (the other ways). `node` names the fleet node that already
pins that fingerprint. With macOS's Local Network permission off the answer says so (`lan`
denied, its finding) and the address field stays; `probe` reads one address the user typed (a
LAN or tailnet name or address, any port): whether an engine answers, its pairing window's state
and fingerprint, or the finding that says why not, in a 200 either way.

PAIRING BY CODE. `start` reads the window (open, its tries, the host key's fingerprint) and makes
this controller's ed25519 key pair for the host (proxy/hostkeys.py `generate`): it stays in memory
until the code comes (PAIR_TTL_S) and is written nowhere before the engine took it. `code` runs the
exchange with the fingerprint the user saw (the window's, or the advertisement's when `start` had
one and they agree). Once the sealed answer opens:
  1  the key pair is kept, sealed with the host id (`HostKeys.put`)
  2  every host key the answer names is pinned, the one it names first first
     (enginectl.pin_host_keys: the code proved them, so a reinstalled host's new key replaces the
     old one here and only here)
  3  the HTTP token goes to the sealed table (`put_http_token`)
  4  the fleet node is made or updated: transport ssh, its trust (address, SSH port and user, the
     key id, the host key's type and fingerprint, `via` code, the engine's HTTP port)
  5  `status` runs over SSH (enginectl): the login works, or the answer says why not (`finding`)
     while the host stays paired (the engine already took the key)
A wrong code answers `wrong_code` with the tries left; a spent, expired or closed window
`code_spent` (an engine refuses a network caller once no window is open, so `code` cannot tell
which); no window at `start` `no_window`; a host key that is not the one the user saw `host_key_mismatch` with both
fingerprints. In none of these is anything stored, and a host key mismatch also forgets the
started pairing.

THE HOST ID is that of the fleet node with the same pinned host key, else of the engine host at the
same address and SSH port (pairing by code replaces its key), else `eh-` and 10 hex digits of the
host key fingerprint's sha256: one id per machine.

SECRETS. The code is in this call only; the private key is in memory until it is sealed; the token
goes from the opened answer to the sealed table. No answer, error, log line, audit row or fleet
record holds any of them (tests/test_engine_pair.py).
"""

from __future__ import annotations

import collections
import hashlib
import ipaddress
import re
import secrets
import threading
import time

from proxy import enginecodes, enginectl, enginepair, hostkeys, mdns

CONTRACT = "1.0"
SERVICE = "_tandem._tcp.local"
BROWSE_S = 2.0
BROWSE_MAX_S = 5.0
PAIR_TTL_S = 600.0
TRIES_PER_MIN = 10
MAX_PENDING = 32
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*\.?$")
PENDING_RE = re.compile(r"^[0-9a-f]{32}$")
ERR = enginecodes.error


def _address(v, what: str = "address") -> str:
    """A host name or an IP address (a tailnet's included); OnboardError otherwise."""
    if not isinstance(v, str) or not v or v.startswith("-") or len(v) > 253:
        raise ERR("bad_request", f"{what} is a host name or an IP address")
    s = v.strip().strip("[]")
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        pass
    if not HOST_RE.match(s):
        raise ERR("bad_request", f"{what} is a host name or an IP address")
    return s.rstrip(".").lower()


def _port(v, default: int) -> int:
    if v is None:
        return default
    if not isinstance(v, int) or isinstance(v, bool) or not 0 < v < 65536:
        raise ERR("bad_request", "port is a whole number from 1 to 65535")
    return v


def _fields(body, allowed: set, required: tuple = ()) -> dict:
    if not isinstance(body, dict):
        raise ERR("bad_request", "the body is a JSON object")
    extra = sorted(set(body) - allowed)
    if extra:
        raise ERR("bad_request", f"unknown field {extra[0]!r}")
    for k in required:
        if body.get(k) is None:
            raise ERR("bad_request", f"{k} is missing")
    return body


def _fp(v, what: str = "fingerprint") -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not enginepair.FP_RE.match(v):
        raise ERR("bad_request", f"{what} is an SSH SHA256 fingerprint (SHA256:...)")
    return v


def _name(v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or len(v) > 64:
        raise ERR("bad_request", "name is a text of 64 characters at most")
    s = "".join(c for c in v if c.isprintable()).strip()
    return s or None


def host_id_of(host_fp: str) -> str:
    return "eh-" + hashlib.sha256(host_fp.encode()).hexdigest()[:10]


def ctl_error(exc: enginectl.CtlError, *, host: str | None = None, port: int | None = None,
              user: str | None = None) -> enginecodes.OnboardError:
    """An engine control error (proxy/enginectl.py) as one of enginecodes'; `detail` is its
    message, which enginectl scrubbed of every value a call carried."""
    f = {"host": host, "port": port, "user": user}
    why = (exc.message or "")[:200] or None
    if isinstance(exc, enginectl.HostKeyChanged):
        return ERR("host_key_changed", None, pinned=exc.pinned, offered=exc.offered or "unknown", **f)
    if isinstance(exc, (enginectl.CtlAuthRefused, enginectl.CtlNotPaired)):
        return ERR("not_paired", why, **f)
    if isinstance(exc, (enginectl.CtlUnreachable, enginectl.CtlTimeout, enginectl.CtlTransport)):
        return ERR("ssh_unreachable", why, **f)
    if isinstance(exc, enginectl.CtlUsage) and exc.code in ("unknown_verb", "unknown_subcommand"):
        return ERR("engine_too_old", why, **f)
    if isinstance(exc, enginectl.CtlBusy):
        return ERR("busy", why, **f)
    return ERR("ctl_failed", why, **f)


class Onboarding:
    def __init__(self, fleet, *, clock=time.time, window=None, browser=None):
        self.fleet = fleet
        self.clock = clock
        self.window = window or (lambda address, port: enginepair.Window(address, port))
        self.browser = browser or mdns.Browser()
        self._found: dict = {"engines": [], "browsed_at": None, "seconds": None, "lan": "not_run",
                             "finding": None}
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._tries: collections.deque = collections.deque()

    @property
    def cfg(self):
        return self.fleet.cfg

    @property
    def root(self) -> str:
        """The controller's state directory: the SSH pins and key files live under it."""
        return self.fleet.trust().root

    @property
    def keys(self) -> hostkeys.HostKeys:
        return hostkeys.HostKeys(self.cfg)

    def log(self, msg: str) -> None:
        self.fleet.log(msg)

    # ------------------------------------------------------------------ the pending steps
    def _put(self, kind: str, ttl: float, **item) -> str:
        now = self.clock()
        with self._lock:
            for k in [k for k, v in self._pending.items() if v["until"] <= now]:
                del self._pending[k]
            while len(self._pending) >= MAX_PENDING:
                del self._pending[min(self._pending, key=lambda k: self._pending[k]["until"])]
            pid = secrets.token_hex(16)
            self._pending[pid] = dict(item, kind=kind, until=now + ttl)
        return pid

    def _get(self, pid, kind: str) -> dict:
        if not isinstance(pid, str) or not PENDING_RE.match(pid):
            raise ERR("bad_request", f"{'pairing' if kind == 'pair' else 'plan'} is the id the "
                                     f"first call answered")
        with self._lock:
            item = self._pending.get(pid)
            if item is None or item["kind"] != kind or item["until"] <= self.clock():
                self._pending.pop(pid, None)
                raise ERR("not_found")
            return item

    def _drop(self, pid) -> bool:
        with self._lock:
            return self._pending.pop(pid, None) is not None

    def _rate(self) -> None:
        now = self.clock()
        with self._lock:
            while self._tries and now - self._tries[0] > 60:
                self._tries.popleft()
            if len(self._tries) >= TRIES_PER_MIN:
                raise ERR("too_many")
            self._tries.append(now)

    # ------------------------------------------------------------------ the routes
    def handle(self, method: str, parts: list[str], q: dict, body, who: str) -> dict:
        sub = parts[1:]
        if method == "GET" and sub == []:
            return self.list_view()
        if sub == ["discover"] and method in ("GET", "POST"):
            return self.discover(body if method == "POST" else None, run=method == "POST")
        if method == "POST" and sub == ["probe"]:
            return self.probe(body)
        if method == "POST" and sub == ["pair", "start"]:
            return self.pair_start(body, who)
        if method == "POST" and sub == ["pair", "code"]:
            return self.pair_code(body, who)
        if method == "POST" and sub == ["pair", "abandon"]:
            return self.pair_abandon(body)
        if method == "GET" and len(sub) == 1:
            return self.engine_view(sub[0])
        raise ERR("not_found", status=404)

    # ------------------------------------------------------------------ the fleet's engine hosts
    def _ssh_rows(self) -> list[dict]:
        return [n for n in self.cfg.nodes()
                if n["transport"] == "ssh" or (n["trust"] or {}).get("kind") == "ssh"]

    def _row(self, hid: str) -> dict:
        row = self.cfg.node(hid) if isinstance(hid, str) else None
        if row is None:
            raise ERR("not_found")
        if row["transport"] != "ssh" and (row["trust"] or {}).get("kind") != "ssh":
            raise ERR("not_ssh", node=row["alias"])
        return row

    def view(self, row: dict, status: dict | None = None, finding: dict | None = None) -> dict:
        """An engine host as the page shows it: no secret, the token's hint only."""
        t = row["trust"] or {}
        eng = (status or {}).get("engine") or {}
        return {"id": row["id"], "alias": row["alias"], "address": row["address"] or t.get("host"),
                "ssh": {"host": t.get("host"), "port": t.get("port", 22), "user": t.get("user"),
                        "key_id": t.get("key_id")},
                "host_key": t.get("host_key"), "host_key_type": t.get("host_key_type"),
                "http": {"port": t.get("http_port"),
                         "token": self.keys.http_token_hint(row["id"])},
                "via": t.get("via"), "paired_at": t.get("paired_at"),
                "engine_version": (status or {}).get("engine_version") or eng.get("version"),
                "mode": (status or {}).get("mode"),
                "status": status, "finding": finding}

    def list_view(self) -> dict:
        return {"contract_version": CONTRACT, "engines": [self.view(r) for r in self._ssh_rows()]}

    def ctl(self, row: dict) -> enginectl.EngineHostControl:
        return enginectl.for_node(row, state_dir=self.root, keys=self.keys, log=self.fleet.log)

    def status_of(self, row: dict) -> tuple[dict | None, dict | None]:
        """(`status` over SSH, None) or (None, the finding that says why there is none)."""
        t = row["trust"] or {}
        try:
            ctl = self.ctl(row)
        except enginectl.CtlError as exc:
            return None, ctl_error(exc, host=t.get("host"), port=t.get("port"),
                                   user=t.get("user")).finding()
        try:
            return ctl.status(), None
        except enginectl.CtlError as exc:
            return None, ctl_error(exc, host=t.get("host"), port=t.get("port"),
                                   user=t.get("user")).finding()
        finally:
            ctl.close()

    def engine_view(self, hid: str) -> dict:
        row = self._row(hid)
        status, finding = self.status_of(row)
        return {"contract_version": CONTRACT, **self.view(row, status, finding)}

    # ------------------------------------------------------------------ keeping a paired host
    def host_id(self, host_fp: str, address: str, ssh_port: int, *, proven: bool) -> str:
        """The id of the engine host behind `host_fp` (the module's THE HOST ID). A host at the
        same address whose pinned key differs is host_key_changed unless the code `proven` it."""
        rows = self._ssh_rows()
        same = next((n for n in rows if (n["trust"] or {}).get("host_key") == host_fp), None)
        if same is not None:
            return same["id"]
        there = next((n for n in rows if (n["trust"] or {}).get("host") == address
                      and (n["trust"] or {}).get("port", 22) == ssh_port), None)
        if there is not None:
            if not proven:
                raise ERR("host_key_changed", None, pinned=there["trust"].get("host_key"),
                          offered=host_fp, host=address, port=ssh_port)
            return there["id"]
        return host_id_of(host_fp)

    def keep(self, hid: str, *, address: str, ssh_user: str, ssh_port: int, key_id: str,
             host_type: str, host_fp: str, via: str, http_port: int | None, name: str | None,
             who: str, method: str) -> dict:
        """The fleet node of an engine host just paired (made, or updated in place), audited."""
        t = enginectl.ssh_trust(host=address, user=ssh_user, key_id=key_id, host_key_type=host_type,
                                host_key=host_fp, port=ssh_port, via=via,
                                paired_at=round(self.clock(), 3))
        if http_port is not None:
            t["http_port"] = http_port
        h = f"[{address}]" if ":" in address else address
        fields = {"transport": "ssh", "endpoint": f"ssh://{ssh_user}@{h}:{ssh_port}",
                  "address": address, "trust": t}
        row = self.cfg.node(hid)
        if row is None:
            taken = {n["alias"].lower() for n in self.cfg.nodes()}
            base = name or address
            alias, i = base, 2
            while alias.lower() in taken:
                alias, i = f"{base} {i}", i + 1
            row = self.cfg.add_node(dict(fields, id=hid, alias=alias), who=who)
        else:
            row = self.cfg.update_node(hid, fields, who=who)
        text = f"engine host paired by {method} at {address}"
        self.cfg.log_audit(who, "pair", "node", hid, detail={
            "via": via, "method": method, "host_key": host_fp, "address": address,
            "key_id": key_id, "text": text})
        self.log(f"[engines] {row['alias']} ({hid}) {text}; SSH host key {host_fp} pinned")
        try:
            self.fleet.store.add_event("node", hid, f"{row['alias']} paired by {method}",
                                       f"{text}; SSH host key {host_fp} pinned")
        except Exception as exc:                                     # noqa: BLE001
            self.log(f"[engines] the fleet's event log did not take the pairing: "
                     f"{type(exc).__name__}")
        return row

    def paired(self, row: dict, via: str) -> dict:
        status, finding = self.status_of(row)
        return {"contract_version": CONTRACT, "state": "paired", "via": via,
                "engine": self.view(row, status, finding),
                "node": self.fleet.node_view(row, self.clock()), "finding": None}

    # ------------------------------------------------------------------ finding the engine
    def _pinned(self, fp: str | None) -> dict | None:
        if not fp:
            return None
        return next((n for n in self._ssh_rows() if (n["trust"] or {}).get("host_key") == fp), None)

    def advert(self, svc: dict) -> dict | None:
        """One browse answer as the page lists it: the TXT's name, version, port and fingerprint
        and the A records' addresses, nothing else; None for an instance with no address."""
        txt = svc.get("txt") or {}
        addrs = []
        for a in svc.get("addresses") or []:
            try:
                addrs.append(str(ipaddress.IPv4Address(a)))
            except ValueError:
                continue
        if not addrs:
            return None
        try:
            port = int(txt.get("port") or svc.get("port") or enginepair.DEFAULT_PORT)
        except (TypeError, ValueError):
            port = svc.get("port") or enginepair.DEFAULT_PORT
        if not 0 < port < 65536:
            return None
        fp = txt.get("fp")
        fp = fp if isinstance(fp, str) and enginepair.FP_RE.match(fp) else None
        name = "".join(c for c in str(txt.get("name") or svc.get("instance") or "")
                       if c.isprintable())[:63] or addrs[0]
        version = txt.get("version")
        version = "".join(c for c in version if c.isprintable())[:32] if isinstance(version, str) \
            else None
        node = self._pinned(fp)
        return {"name": name, "instance": str(svc.get("instance") or "")[:63], "version": version,
                "address": addrs[0], "addresses": addrs, "port": port, "fingerprint": fp,
                "verified": False, "node": node["id"] if node else None,
                "alias": node["alias"] if node else None}

    def discover(self, body, *, run: bool) -> dict:
        if not run:
            return {"contract_version": CONTRACT, **self._found}
        b = _fields(body if body is not None else {}, {"seconds", "interface"})
        secs = b.get("seconds", BROWSE_S)
        if not isinstance(secs, (int, float)) or isinstance(secs, bool) or \
                not 0.2 <= secs <= BROWSE_MAX_S:
            raise ERR("bad_request", f"seconds is from 0.2 to {BROWSE_MAX_S:g}")
        iface = b.get("interface")
        if iface is not None:
            try:
                iface = str(ipaddress.IPv4Address(iface))
            except (ValueError, TypeError):
                raise ERR("bad_request", "interface is the IPv4 address of a LAN interface") from None
        lan, finding, found = "ok", None, []
        try:
            got = self.browser.browse([SERVICE], float(secs), iface=iface)
        except mdns.LanDenied:
            lan, finding, got = "denied", enginecodes.finding("lan_denied"), []
        except OSError as exc:
            lan, finding, got = "failed", enginecodes.finding("lan_failed", type(exc).__name__), []
        seen = set()
        for svc in got:
            a = self.advert(svc)
            if a is not None and (a["address"], a["port"]) not in seen:
                seen.add((a["address"], a["port"]))
                found.append(a)
        self._found = {"engines": found, "browsed_at": round(self.clock(), 3), "seconds": secs,
                       "lan": lan, "finding": finding}
        self.log(f"[engines] LAN browse for {SERVICE}: {len(found)} found ({lan})")
        return {"contract_version": CONTRACT, **self._found}

    def probe(self, body) -> dict:
        b = _fields(body, {"address", "port"}, ("address",))
        address = _address(b["address"])
        port = _port(b.get("port"), enginepair.DEFAULT_PORT)
        out = {"contract_version": CONTRACT, "address": address, "port": port, "reachable": False,
               "engine": False, "pairing": None, "fingerprint": None, "node": None, "finding": None}
        w = self.window(address, port)
        try:
            st = w.status()
        except enginecodes.OnboardError as exc:
            out["reachable"] = exc.code != "unreachable"
            if exc.code == "pairing_unsupported" and w.health() is None:
                exc = ERR("not_an_engine", exc.detail, address=address, port=port)
            out["engine"] = exc.code in ("pairing_unsupported", "no_window", "rate_limited")
            out["finding"] = exc.finding()
            return out
        out.update(reachable=True, engine=True, fingerprint=st.get("fingerprint"),
                   pairing={"open": st["open"], "state": st["state"],
                            "expires_in": st.get("expires_in"), "tries_left": st.get("tries_left"),
                            "fingerprint": st.get("fingerprint")})
        node = self._pinned(st.get("fingerprint"))
        out["node"] = node["id"] if node else None
        if not st["open"]:
            out["finding"] = enginecodes.finding(
                "code_spent" if st["state"] in ("expired", "spent", "paired", "confirming")
                else "no_window", state=st["state"], address=address, port=port)
        return out

    # ------------------------------------------------------------------ pairing by code
    def pair_start(self, body, who: str) -> dict:
        b = _fields(body, {"address", "port", "fingerprint", "name"}, ("address",))
        address = _address(b["address"])
        port = _port(b.get("port"), enginepair.DEFAULT_PORT)
        advertised = _fp(b.get("fingerprint"))
        name = _name(b.get("name"))
        w = self.window(address, port)
        try:
            st = w.status()
        except enginecodes.OnboardError as exc:
            if exc.code == "pairing_unsupported" and w.health() is None:
                raise ERR("not_an_engine", exc.detail, address=address, port=port) from None
            raise
        if not st["open"]:
            if st["state"] in ("expired", "spent", "paired", "confirming"):
                raise ERR("code_spent", None, state=st["state"], address=address, port=port)
            raise ERR("no_window", None, state=st["state"], address=address, port=port)
        fp = st.get("fingerprint")
        if advertised and fp and advertised != fp:
            raise ERR("host_key_mismatch", "the advertisement and the pairing window disagree",
                      expected=advertised, offered=fp, address=address, port=port)
        pem, pub = hostkeys.generate()
        kid = hostkeys.key_id_of(pub)
        pid = self._put("pair", PAIR_TTL_S, address=address, port=port, expect=fp or advertised,
                        pem=pem, public=pub, key_id=kid, name=name)
        self.log(f"[engines] pairing with {address}:{port} started; window {st['state']}, "
                 f"{st.get('tries_left')} tries left")
        return {"contract_version": CONTRACT, "pairing": pid, "address": address, "port": port,
                "window": {"open": st["open"], "state": st["state"],
                           "expires_in": st.get("expires_in"), "tries_left": st.get("tries_left"),
                           "fingerprint": fp},
                "fingerprint": fp or advertised, "advertised": advertised, "key_id": kid,
                "expires_in": int(PAIR_TTL_S)}

    def pair_code(self, body, who: str) -> dict:
        b = _fields(body, {"pairing", "code"}, ("pairing", "code"))
        item = self._get(b["pairing"], "pair")
        code = b["code"]
        if not isinstance(code, str) or not enginepair.CODE_RE.match(code):
            raise ERR("bad_code")
        self._rate()
        address, port = item["address"], item["port"]
        try:
            ans = enginepair.run(self.window(address, port), code, item["public"], item["key_id"],
                                 expect=item["expect"], name=self.fleet.trust().name)
        except enginecodes.OnboardError as exc:
            if exc.code == "host_key_mismatch":
                self._drop(b["pairing"])
            self.log(f"[engines] pairing with {address}:{port} refused: {exc.code}")
            if exc.code == "no_window":
                # `start` saw it open: the window expired, was spent or closed since (an engine
                # answers a network caller 401 once no window is open, whichever ended it)
                raise ERR("code_spent", exc.detail, state="closed", address=address,
                          port=port) from None
            raise
        finally:
            code = b["code"] = None
        primary = enginepair.parse_key(ans["host_key"])
        others = [enginepair.parse_key(k) for k in ans["host_keys"]]
        host_fp = enginepair.fingerprint(primary[1])
        hid = self.host_id(host_fp, address, ans["ssh_port"], proven=True)
        self.keys.put(hid, item["pem"], item["public"], who=who)
        enginectl.pin_host_keys(self.root, hid, [primary] + [k for k in others if k != primary],
                                replace=True)
        self.keys.put_http_token(hid, ans["http_token"], who=who)
        ans["http_token"] = None
        row = self.keep(hid, address=address, ssh_user=ans["ssh_user"], ssh_port=ans["ssh_port"],
                        key_id=item["key_id"], host_type=primary[0], host_fp=host_fp, via="code",
                        http_port=ans["http_port"], name=item.get("name"), who=who, method="code")
        self._drop(b["pairing"])
        return self.paired(row, "code")

    def pair_abandon(self, body) -> dict:
        b = _fields(body, {"pairing"}, ("pairing",))
        if not isinstance(b["pairing"], str) or not PENDING_RE.match(b["pairing"]):
            raise ERR("bad_request", "pairing is the id the first call answered")
        return {"contract_version": CONTRACT, "abandoned": self._drop(b["pairing"])}
