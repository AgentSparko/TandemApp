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
    POST /v1/fleet/engines/connect/password/plan {host, port?, user, engine_dir?, name?}
    POST /v1/fleet/engines/connect/password/confirm {plan, password, host_key}
                                                  password once (SRV-831): the host key shown,
                                                  then confirmed, then the key's line installed
    POST /v1/fleet/engines/connect/key/new {host, port?, user, engine_dir?, from?, name?}
    POST /v1/fleet/engines/connect/key/check {plan, host_key}
                                                  paste a key (SRV-831): the line to paste, then
                                                  a check that waits for the first login
    POST /v1/fleet/engines/{id}/mode {mode?, when?, ext?}
                                                  the engine's mode (SRV-833): bare engine
                                                  (`mode set engine`) unless it is that already
    GET  /v1/fleet/engines/{id}/models            the Setup Guide's models (GET /v1/setup/models)
    POST /v1/fleet/engines/{id}/models/pull {model, profile?, dry_run?}
                                                  install a model: the Setup Guide's plan, then
                                                  its apply (the download is its fetch step)
    GET  /v1/fleet/engines/{id}/models/progress[?job=]
                                                  that job (GET /v1/setup/jobs/{id}), summed up

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

THE OTHER WAYS (SRV-831) pin the host key on first contact: the first call scans it
(`ssh-keyscan`, sshonce) and answers its fingerprint, `known` new, same (the fleet already pins it
for that address) or changed (refused: 409 host_key_changed, both fingerprints; only pairing by
code replaces a pinned key); the second call must send that fingerprint back as `host_key`
(else 409 host_key_unconfirmed), and every login takes exactly that key.
- Password once: `confirm` logs in once with the password through sshonce's askpass path (in this
  call's memory only, never stored, logged or answered) and runs INSTALL with bash on the host: it
  finds the engine's install (`engine_dir`, else ~/TandemLLM), checks ~/.ssh as the engine's
  pairing does (no link, the user's own, not writable by others; made 0700/0600 when missing) and
  appends the forced-command line (the engine's `key_line`, with --key-id) unless the very same line
  is there; the session ends with the script.
- Paste a key: `new` answers the line for a new key pair (`engine_dir` must be the install's
  absolute path; without one the answer guesses /home/<user>/TandemLLM and says so) and the
  address, port and user it will try; `check` tries that login (BatchMode, the key only) and
  answers `state` waiting with the key_not_installed finding until the host takes the key.
Then both, over the new login: `token rotate` (the token goes straight to the sealed table) and
`status`; only then are the key pair, the pin and the node kept (`via` ssh, `method` password or
key in the audit). A failure there undoes the pin and the token of a host that was not in the
fleet.

AFTER PAIRING (SRV-833). The engine view's `wizard` says what the first run does next: bare
engine is the preselected mode, and `needs_model` is true while `status` names no model. `mode`
reads `mode get` and changes nothing when the configured mode is already the asked one (not
inferred from an empty QSE_MODE); else `mode set` (an idle-gated restart by default) writes it
into the engine's managed overlay. A model comes through the engine's own Setup Guide over HTTP
with this controller's token: GET /v1/setup/models/{id}/current is the box's selection, `profile`
replaces its weights, POST /v1/setup/plan checks it (download size, errors) and POST
/v1/setup/apply starts the job whose steps download, check, write the config and restart;
`progress` reads the job. No file is edited by anyone but the engine's own tools. An HTTP call that
fails is told apart over SSH: the engine down (`engine_down`), its port blocked from here
(`http_blocked`), the token refused (`token_refused`), the Setup Guide locked by its admin
password (`setup_locked`).

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
import http.client
import json
import urllib.parse
import ipaddress
import os
import re
import secrets
import shlex
import threading
import time

from proxy import enginecodes, enginectl, enginepair, hostkeys, mdns, sshonce

CONTRACT = "1.0"
SERVICE = "_tandem._tcp.local"
BROWSE_S = 2.0
BROWSE_MAX_S = 5.0
PAIR_TTL_S = 600.0
PLAN_TTL_S = 600.0
KEY_TTL_S = 3600.0
PASSWORD_MAX = 1024
DEFAULT_DIR = "TandemLLM"
ENGINE_DIR_RE = re.compile(r"^(?:~|~/[A-Za-z0-9._/+@-]{0,1000}|/[A-Za-z0-9._/+@-]{0,1023})$")
#: password once: on the engine host, with bash, as the account's own shell runs it. The engine's
#: pairing (server/remote/keys.py) checks ~/.ssh the same way; the line is the engine's key_line.
INSTALL = r"""set -u
umask 077
fail() { echo "TANDEMENGINE-ERROR $1 $2" >&2; exit 65; }
dir=__DIR__
[ -n "$dir" ] || dir="$HOME/__DEFAULT__"
case "$dir" in "~") dir="$HOME" ;; "~/"*) dir="$HOME/${dir#"~/"}" ;; esac
if [ ! -f "$dir/bin/tandem" ]; then echo "TANDEMENGINE-ERROR not_installed $dir" >&2; exit 64; fi
dir=$(cd "$dir" 2>/dev/null && pwd -P) || { echo "TANDEMENGINE-ERROR not_installed $dir" >&2; exit 64; }
case "$dir" in /*) ;; *) fail bad_dir "$dir" ;; esac
case "$dir" in *[!A-Za-z0-9._/+@-]*) fail bad_dir "$dir" ;; esac
sd="$HOME/.ssh"
ak="$sd/authorized_keys"
check() {
  [ -L "$1" ] && fail "$2_link" "$1 is a symbolic link"
  if [ "$2" = ssh_dir ]; then [ -d "$1" ] || fail ssh_dir_type "$1 is not a directory"
  else [ -f "$1" ] || fail authorized_keys_type "$1 is not a regular file"; fi
  [ -O "$1" ] || fail "$2_owner" "$1 belongs to another user"
  case "$(ls -ld "$1" | cut -c1-10)" in ?????w????|????????w?) fail "$2_mode" "$1 is writable by group or others" ;; esac
}
if [ -e "$sd" ] || [ -L "$sd" ]; then check "$sd" ssh_dir; else mkdir -m 700 "$sd" || fail ssh_dir_create "$sd cannot be created"; fi
if [ -e "$ak" ] || [ -L "$ak" ]; then check "$ak" authorized_keys; fi
pre=__PRE__
post=__POST__
blob=__BLOB__
line="$pre$dir$post"
added=1
if [ -f "$ak" ] && grep -qF -e "$blob" "$ak"; then
  if grep -qxF -e "$line" "$ak"; then added=0; else fail key_present "the key already has another line"; fi
fi
if [ "$added" = 1 ]; then
  if [ ! -e "$ak" ]; then : > "$ak" && chmod 600 "$ak" || fail authorized_keys_open "$ak cannot be written"; fi
  printf '%s\n' "$line" >> "$ak" || fail authorized_keys_open "$ak cannot be written"
fi
echo "TANDEMENGINE-KEY $added $dir"
"""
KEY_PLACE = enginepair.KEY_PLACE
HTTP_TIMEOUT_S = 20.0
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
JOB_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
ENDED = ("done", "failed", "rolled_back", "cancelled")
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


def _login(v) -> str:
    if not isinstance(v, str) or not enginectl.USER_RE.match(v):
        raise ERR("bad_request", "user is a login name")
    return v


def _engine_dir(v, absolute: bool = False) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not ENGINE_DIR_RE.match(v) or "/../" in v + "/" or \
            (absolute and not v.startswith("/")):
        raise ERR("bad_request", "engine_dir is the engine's install folder, " +
                  ("an absolute path" if absolute else "an absolute path or ~/...") +
                  " of letters, digits and . _ / + @ -")
    return v.rstrip("/") or "/"


def install_script(public: str, key_id: str, engine_dir: str | None) -> str:
    """INSTALL for this key: its line split around the install's folder, found on the host."""
    pre, post = hostkeys.key_line(public, key_id, "/TANDEMDIR").split("/TANDEMDIR", 1)
    return (INSTALL.replace("__DIR__", shlex.quote(engine_dir or ""))
            .replace("__DEFAULT__", DEFAULT_DIR).replace("__PRE__", shlex.quote(pre))
            .replace("__POST__", shlex.quote(post)).replace("__BLOB__", shlex.quote(public.split()[1])))


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
        if method == "POST" and sub == ["connect", "password", "plan"]:
            return self.password_plan(body)
        if method == "POST" and sub == ["connect", "password", "confirm"]:
            return self.password_confirm(body, who)
        if method == "POST" and sub == ["connect", "key", "new"]:
            return self.key_new(body)
        if method == "POST" and sub == ["connect", "key", "check"]:
            return self.key_check(body, who)
        if method == "GET" and len(sub) == 1:
            return self.engine_view(sub[0])
        if method == "POST" and len(sub) == 2 and sub[1] == "mode":
            return self.set_mode(sub[0], body, who)
        if method == "GET" and len(sub) == 2 and sub[1] == "models":
            return self.models(sub[0])
        if method == "POST" and len(sub) == 3 and sub[1:] == ["models", "pull"]:
            return self.pull(sub[0], body, who)
        if method == "GET" and len(sub) == 3 and sub[1:] == ["models", "progress"]:
            return self.progress(sub[0], q.get("job"))
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

    @staticmethod
    def wizard(status: dict | None) -> dict:
        """What the first run does next (SRV-833): bare engine preselected, then a model."""
        eng = (status or {}).get("engine") or {}
        needs = status is not None and not eng.get("model")
        return {"mode_choice": "engine", "mode": (status or {}).get("mode"),
                "needs_model": needs, "next": None if status is None else "model" if needs else "chat"}

    def engine_view(self, hid: str) -> dict:
        row = self._row(hid)
        status, finding = self.status_of(row)
        return {"contract_version": CONTRACT, **self.view(row, status, finding),
                "wizard": self.wizard(status)}

    # ------------------------------------------------------------------ after pairing (SRV-833)
    def _ctl_call(self, row: dict, fn):
        t = row["trust"] or {}
        try:
            ctl = self.ctl(row)
        except enginectl.CtlError as exc:
            raise ctl_error(exc, host=t.get("host"), port=t.get("port"), user=t.get("user")) from None
        try:
            return fn(ctl)
        except enginectl.CtlError as exc:
            raise ctl_error(exc, host=t.get("host"), port=t.get("port"), user=t.get("user")) from None
        finally:
            ctl.close()

    def set_mode(self, hid: str, body, who: str) -> dict:
        b = _fields(body if body is not None else {}, {"mode", "when", "ext"})
        mode = b.get("mode", "engine")
        if mode not in ("engine", "chat"):
            raise ERR("bad_request", "mode is engine (bare engine, the default) or chat")
        when = b.get("when", "idle")
        if when not in ("now", "idle"):
            raise ERR("bad_request", "when is now or idle")
        ext = b.get("ext")
        if ext is not None and (mode != "chat" or not isinstance(ext, str)
                                or not enginectl.MODULE_RE.match(ext)):
            raise ERR("bad_request", "ext is the extension module chat mode runs (chat only)")
        row = self._row(hid)
        before = self._ctl_call(row, lambda c: c.mode_get())
        prev = {k: before.get(k) for k in ("mode", "ext", "source", "inferred")}
        if before.get("mode") == mode and not before.get("inferred") and ext in (None, before.get("ext")):
            return {"contract_version": CONTRACT, "id": hid, "mode": mode, "changed": False,
                    "previous": prev, "job": None}
        doc = self._ctl_call(row, lambda c: c.mode_set(mode, ext=ext, when=when))
        self.cfg.log_audit(who, "mode", "node", hid, detail={
            "mode": mode, "from": prev["mode"], "when": when,
            "text": f"engine mode set to {mode} ({when})"})
        self.log(f"[engines] {row['alias']} ({hid}): mode {prev['mode']} -> {mode} ({when})")
        return {"contract_version": CONTRACT, "id": hid, "mode": mode, "changed": True,
                "previous": prev, "job": doc.get("job")}

    # the engine's HTTP routes, with this controller's token
    def _raw(self, row: dict, method: str, path: str, body=None) -> tuple[int, dict]:
        t = row["trust"] or {}
        address, port = row["address"] or t.get("host"), t.get("http_port") or enginepair.DEFAULT_PORT
        token = self.cfg.engine_token(row["id"])
        if not token:
            raise ERR("token_missing")
        hdrs = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(address, port, timeout=HTTP_TIMEOUT_S)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            r = conn.getresponse()
            raw, st = r.read(4 << 20), r.status
        except (OSError, http.client.HTTPException):
            raise self._why_no_http(row, address, port) from None
        finally:
            token = hdrs = None
            conn.close()
        try:
            doc = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            doc = None
        if not isinstance(doc, dict):
            raise ERR("not_an_engine", f"HTTP {st}, not JSON", address=address, port=port)
        return st, doc

    def _http(self, row: dict, method: str, path: str, body=None, *, ok=(200,)) -> tuple[int, dict]:
        st, doc = self._raw(row, method, path, body)
        t = row["trust"] or {}
        address, port = row["address"] or t.get("host"), t.get("http_port") or enginepair.DEFAULT_PORT
        if st == 401:
            raise self._why_401(row)
        if st == 403 and str((doc.get("error") or {}).get("type", "")).startswith("forbidden"):
            raise ERR("not_allowed", None, address=address, port=port)
        if st not in ok:
            raise self._setup_refusal(st, doc)
        return st, doc

    def _why_no_http(self, row: dict, address: str, port: int) -> enginecodes.OnboardError:
        """No HTTP answer: over SSH, is the engine down or is its port blocked from here?"""
        status, finding = self.status_of(row)
        if status is None:
            return enginecodes.OnboardError(finding["code"], finding.get("detail"),
                                            **{k: v for k, v in finding.items()
                                               if k in enginecodes.FACTS})
        state = (status.get("engine") or {}).get("state")
        if state in ("down", "failed"):
            return ERR("engine_down", None, host=address, port=port)
        return ERR("http_blocked", None, address=address, port=port)

    def _why_401(self, row: dict) -> enginecodes.OnboardError:
        """401 from a Setup Guide route: the token (a route the token opens refuses it too), or
        the Setup Guide's own admin password?"""
        try:
            st, _ = self._raw(row, "GET", "/v1/models")
        except enginecodes.OnboardError as exc:
            return exc
        return ERR("token_refused") if st == 401 else ERR("setup_locked")

    @staticmethod
    def _setup_refusal(st: int, doc: dict) -> enginecodes.OnboardError:
        e = doc.get("error") if isinstance(doc.get("error"), dict) else {}
        kind, msg = str(e.get("type") or e.get("code") or ""), str(e.get("message") or "")[:300]
        if kind in ("busy", "job_running"):
            return ERR("busy", msg or kind)
        if kind in ("unknown_model", "not_found") and st in (400, 404):
            return ERR("unknown_model", msg or None)
        return ERR("setup_refused", msg or f"HTTP {st}", status=st if 400 <= st < 500 else 502)

    def _row_http(self, hid: str) -> dict:
        row = self._row(hid)
        if not (row["trust"] or {}).get("http_port") and not row["address"]:
            raise ERR("not_paired", "no HTTP port is known for this engine host")
        return row

    def models(self, hid: str) -> dict:
        row = self._row_http(hid)
        _, doc = self._http(row, "GET", "/v1/setup/models")
        return {"contract_version": CONTRACT, "id": hid,
                "models": doc.get("models") if isinstance(doc.get("models"), list) else [],
                "errors": doc.get("errors") if isinstance(doc.get("errors"), list) else []}

    @staticmethod
    def summary(job: dict | None) -> dict | None:
        """A Setup Guide job in a line: its state, the step that runs and its detail (the
        download's bytes so far), how many of its steps are done."""
        if not isinstance(job, dict):
            return None
        steps = [x for x in job.get("steps") or [] if isinstance(x, dict) and x.get("id") != "rollback"]
        cur = next((x for x in steps if x.get("state") == "running"), None)
        state = job.get("state")
        return {"state": state, "finished": state in ENDED,
                "step": cur.get("id") if cur else None, "label": cur.get("label") if cur else None,
                "detail": cur.get("detail") if cur else None,
                "downloading": bool(cur and cur.get("id") == "fetch"),
                "steps_done": sum(1 for x in steps if x.get("state") in ("done", "skipped")),
                "steps": len(steps), "message": job.get("message")}

    def pull(self, hid: str, body, who: str) -> dict:
        b = _fields(body, {"model", "profile", "dry_run"}, ("model",))
        model, profile = b["model"], b.get("profile")
        if not isinstance(model, str) or not MODEL_RE.match(model):
            raise ERR("bad_request", "model is a model id of the engine's Setup Guide")
        if profile is not None and (not isinstance(profile, str) or not PROFILE_RE.match(profile)):
            raise ERR("bad_request", "profile is a weights profile id of that model")
        if not isinstance(b.get("dry_run", False), bool):
            raise ERR("bad_request", "dry_run is true or false")
        row = self._row_http(hid)
        q = urllib.parse.quote(model, safe="")
        try:
            _, cur = self._http(row, "GET", f"/v1/setup/models/{q}/current")
        except enginecodes.OnboardError as exc:
            if exc.code in ("unknown_model", "setup_refused") and exc.status in (400, 404):
                raise ERR("unknown_model", exc.detail, model=model) from None
            raise
        sel = cur.get("selection") if isinstance(cur.get("selection"), dict) else None
        if sel is None:
            raise ERR("setup_refused", "the engine gave no selection for that model")
        if profile is not None:
            sel = dict(sel, profile=profile)
        _, plan = self._http(row, "POST", "/v1/setup/plan", {"selection": sel})
        facts = {"ok": bool(plan.get("ok")), "errors": plan.get("errors") or [],
                 "warnings": plan.get("warnings") or [],
                 "download_gb": (plan.get("profile") or {}).get("download_gb"),
                 "on_disk": (plan.get("profile") or {}).get("on_disk"),
                 "profile": (plan.get("profile") or {}).get("next") or sel.get("profile")}
        if not facts["ok"]:
            raise ERR("setup_refused", " ".join(str(x) for x in facts["errors"])[:300] or "the plan "
                      "has errors", model=model)
        out = {"contract_version": CONTRACT, "id": hid, "model": model, "profile": facts["profile"],
               "plan": facts, "job": None, "progress": None}
        if b.get("dry_run"):
            return out
        _, started = self._http(row, "POST", "/v1/setup/apply", {"selection": sel}, ok=(200, 202))
        job = started.get("job") if isinstance(started.get("job"), dict) else None
        self.cfg.log_audit(who, "model", "node", hid, detail={
            "model": model, "profile": facts["profile"], "job": (job or {}).get("id"),
            "text": f"model {model} ({facts['profile']}) installing"})
        self.log(f"[engines] {row['alias']} ({hid}): installing {model} ({facts['profile']}), "
                 f"{facts['download_gb']} GB to download, job {(job or {}).get('id')}")
        return dict(out, job=job, progress=self.summary(job))

    def progress(self, hid: str, jid) -> dict:
        row = self._row_http(hid)
        if jid is not None:
            if not isinstance(jid, str) or not JOB_RE.match(jid):
                raise ERR("bad_request", "job is a Setup Guide job id")
            _, job = self._http(row, "GET", f"/v1/setup/jobs/{jid}?format=json")
        else:
            _, st = self._http(row, "GET", "/v1/setup/status")
            job = st.get("job") if isinstance(st.get("job"), dict) else None
        return {"contract_version": CONTRACT, "id": hid, "job": job, "progress": self.summary(job)}

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
                "engine": dict(self.view(row, status, finding), wizard=self.wizard(status)),
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
        others = [k for k in (enginepair.parse_key(x) for x in ans["host_keys"])
                  if k[0] in enginectl.HOST_KEY_TYPES]
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

    # ------------------------------------------------------------------ the other ways
    @property
    def ssh_once(self) -> sshonce.Ssh:
        if getattr(self, "_ssh_once", None) is None:
            self._ssh_once = sshonce.Ssh(os.path.join(self.root, "known_hosts"))
        return self._ssh_once

    @ssh_once.setter
    def ssh_once(self, v) -> None:
        self._ssh_once = v

    def scan(self, host: str, port: int) -> dict:
        """The host key the host offers (the type an OpenSSH client prefers) against what the
        fleet pins for that address: {type, b64, fingerprint, known, stored}."""
        try:
            offered = self.ssh_once.scan(host, port)
        except sshonce.SshError as exc:
            raise ERR("ssh_unreachable", exc.message, host=host, port=port) from None
        kind = next(k for k in sshonce.PREFER + tuple(offered) if k in offered)
        fp = sshonce.fingerprint(offered[kind])
        there = next((n for n in self._ssh_rows() if (n["trust"] or {}).get("host") == host
                      and (n["trust"] or {}).get("port", 22) == port), None)
        stored = (there["trust"] or {}).get("host_key") if there else None
        known = "new" if stored is None else "same" if stored == fp else "changed"
        return {"type": kind, "b64": offered[kind], "fingerprint": fp, "known": known,
                "stored": stored}

    @staticmethod
    def _host_key_view(hk: dict | None) -> dict | None:
        return None if hk is None else {k: hk[k] for k in ("type", "fingerprint", "known", "stored")}

    def _confirmed(self, item: dict, given, hk: dict) -> None:
        """The host key `hk` offered now must be the one shown and the one the user sent back."""
        if hk["known"] == "changed":
            raise ERR("host_key_changed", None, pinned=hk["stored"], offered=hk["fingerprint"],
                      host=item["host"], port=item["port"])
        if given is None:
            raise ERR("host_key_unconfirmed", None, offered=hk["fingerprint"], host=item["host"])
        if given != hk["fingerprint"]:
            raise ERR("host_key_mismatch", "the host key the host offers now is not the one "
                      "confirmed", expected=given, offered=hk["fingerprint"], host=item["host"])

    def finish_ssh(self, item: dict, hk: dict, who: str, method: str) -> dict:
        """Over the new login: `token rotate` into the sealed table, then `status`; then the key
        pair, the pin and the node are kept. A failure undoes the pin and the token of a host
        that was not in the fleet, and is the control error's code."""
        host, port, user = item["host"], item["port"], item["user"]
        hid = self.host_id(hk["fingerprint"], host, port, proven=False)
        root = self.root
        new = self.cfg.node(hid) is None
        try:
            enginectl.pin_host_keys(root, hid, [(hk["type"], hk["b64"])], replace=new)
        except enginectl.CtlError as exc:
            raise ctl_error(exc, host=host, port=port, user=user) from None
        ctl = enginectl.SshControl(enginectl.SshHost(
            id=hid, host=host, port=port, user=user, key_id=item["key_id"],
            host_key_type=hk["type"], host_key=hk["fingerprint"]), root,
            key=lambda: item["pem"], log=self.fleet.log)
        token_set = False
        try:
            def store(tok):
                nonlocal token_set
                token_set = True
                return self.keys.put_http_token(hid, tok, who=who)
            ctl.token_rotate(store=store)
            status = ctl.status()
        except enginectl.CtlError as exc:
            if new:
                enginectl.forget_host(root, hid)
                if token_set:
                    self.cfg.delete_engine_token(hid, who=who)
            if method == "key" and isinstance(exc, enginectl.CtlProtocol):
                raise ERR("not_installed", exc.message[:200], dir=item.get("engine_dir"),
                          host=host) from None
            raise ctl_error(exc, host=host, port=port, user=user) from None
        finally:
            ctl.close()
        self.keys.put(hid, item["pem"], item["public"], who=who)
        http_port = ((status.get("engine") or {}).get("port")) if isinstance(status, dict) else None
        row = self.keep(hid, address=host, ssh_user=user, ssh_port=port, key_id=item["key_id"],
                        host_type=hk["type"], host_fp=hk["fingerprint"], via="ssh",
                        http_port=http_port if isinstance(http_port, int) else None,
                        name=item.get("name"), who=who, method=method)
        return {"contract_version": CONTRACT, "state": "paired", "via": method,
                "engine": dict(self.view(row, status, None), wizard=self.wizard(status)),
                "node": self.fleet.node_view(row, self.clock()), "finding": None}

    def _target(self, b: dict) -> tuple[str, int, str]:
        return _address(b["host"], "host"), _port(b.get("port"), 22), _login(b["user"])

    # password once
    def password_plan(self, body) -> dict:
        b = _fields(body, {"host", "port", "user", "engine_dir", "name"}, ("host", "user"))
        host, port, user = self._target(b)
        engine_dir = _engine_dir(b.get("engine_dir"))
        name = _name(b.get("name"))
        hk = self.scan(host, port)
        if hk["known"] == "changed":
            raise ERR("host_key_changed", None, pinned=hk["stored"], offered=hk["fingerprint"],
                      host=host, port=port)
        pem, pub = hostkeys.generate()
        kid = hostkeys.key_id_of(pub)
        pid = self._put("password", PLAN_TTL_S, host=host, port=port, user=user,
                        engine_dir=engine_dir, name=name, pem=pem, public=pub, key_id=kid,
                        host_key=hk)
        return {"contract_version": CONTRACT, "plan": pid, "host": host, "port": port, "user": user,
                "engine_dir": engine_dir, "host_key": self._host_key_view(hk), "key_id": kid,
                "expires_in": int(PLAN_TTL_S)}

    def password_confirm(self, body, who: str) -> dict:
        b = _fields(body, {"plan", "password", "host_key"}, ("plan", "password"))
        password = b.pop("password")
        try:
            if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX or \
                    "\n" in password:
                raise ERR("bad_request", "password is one line of text")
            item = self._get(b["plan"], "password")
            hk = item["host_key"]
            self._confirmed(item, _fp(b.get("host_key"), "host_key"), hk)
            self._rate()
            host, port, user = item["host"], item["port"], item["user"]
            try:
                rc, out, err = self.ssh_once.run(
                    host, port, user, "/bin/bash -s",
                    install_script(item["public"], item["key_id"], item["engine_dir"]).encode(),
                    kind=hk["type"], b64=hk["b64"], password=password)
            except sshonce.SshError as exc:
                raise ERR("ssh_unreachable" if exc.kind == "unreachable" else "ssh_failed",
                          exc.message, host=host, port=port) from None
        finally:
            password = None
        done = next((x for x in out.splitlines() if x.startswith("TANDEMENGINE-KEY ")), None)
        if rc != 0 or done is None:
            raise self._install_refusal(rc, err, item)
        parts = done.split(" ", 2)
        item = dict(item, engine_dir=parts[2] if len(parts) > 2 else item["engine_dir"])
        self.log(f"[engines] {user}@{host}:{port}: the key's line "
                 f"{'added' if parts[1] == '1' else 'was there'} by password once; session closed")
        out = self.finish_ssh(item, hk, who, "password")
        self._drop(b["plan"])
        return out

    def _install_refusal(self, rc: int, err: str, item: dict) -> enginecodes.OnboardError:
        host, port, user = item["host"], item["port"], item["user"]
        mark = next((x for x in err.splitlines() if x.startswith("TANDEMENGINE-ERROR ")), "")
        kind, _, why = mark[len("TANDEMENGINE-ERROR "):].partition(" ")
        if rc == 64 or kind == "not_installed":
            return ERR("not_installed", None, dir=why or item.get("engine_dir") or f"~/{DEFAULT_DIR}",
                       host=host)
        if kind == "bad_dir":
            return ERR("bad_engine_dir", why, host=host)
        if kind == "key_present":
            return ERR("key_present", why, host=host)
        if kind in KEY_PLACE:
            return ERR("key_refused", why, host=host)
        lines = [x for x in err.strip().splitlines() if x.strip()]
        last = (lines or [f"exit status {rc}"])[-1][:300]
        if "Host key verification failed" in err or "REMOTE HOST IDENTIFICATION" in err:
            return ERR("host_key_changed", None, pinned=item["host_key"]["fingerprint"],
                       offered="another", host=host, port=port)
        if rc == 255 and ("Permission denied" in err or "Authentication failed" in err):
            return ERR("auth_failed", None, host=host, user=user)
        if rc == 255:
            return ERR("ssh_unreachable", last, host=host, port=port)
        return ERR("ssh_failed", last, host=host)

    # paste a key
    def key_new(self, body) -> dict:
        b = _fields(body, {"host", "port", "user", "engine_dir", "from", "name"}, ("host", "user"))
        host, port, user = self._target(b)
        engine_dir = _engine_dir(b.get("engine_dir"), absolute=True)
        guessed = engine_dir is None
        if guessed:
            engine_dir = f"/home/{user}/{DEFAULT_DIR}"
        name = _name(b.get("name"))
        pem, pub = hostkeys.generate()
        kid = hostkeys.key_id_of(pub)
        try:
            line = hostkeys.key_line(pub, kid, engine_dir, b.get("from"))
        except hostkeys.HostKeyError as exc:
            raise ERR("bad_request", exc.message) from None
        hk, finding = None, None
        try:
            hk = self.scan(host, port)
        except enginecodes.OnboardError as exc:
            finding = exc.finding()
        if hk is not None and hk["known"] == "changed":
            raise ERR("host_key_changed", None, pinned=hk["stored"], offered=hk["fingerprint"],
                      host=host, port=port)
        pid = self._put("key", KEY_TTL_S, host=host, port=port, user=user, engine_dir=engine_dir,
                        name=name, pem=pem, public=pub, key_id=kid)
        return {"contract_version": CONTRACT, "plan": pid, "host": host, "port": port, "user": user,
                "engine_dir": engine_dir, "engine_dir_guessed": guessed, "line": line,
                "file": "~/.ssh/authorized_keys", "key_id": kid,
                "key_fingerprint": hostkeys.fingerprint_of(pub),
                "host_key": self._host_key_view(hk), "finding": finding,
                "expires_in": int(KEY_TTL_S)}

    def key_check(self, body, who: str) -> dict:
        b = _fields(body, {"plan", "host_key"}, ("plan",))
        item = self._get(b["plan"], "key")
        given = _fp(b.get("host_key"), "host_key")
        hk = self.scan(item["host"], item["port"])
        self._confirmed(item, given, hk)
        try:
            out = self.finish_ssh(item, hk, who, "key")
        except enginecodes.OnboardError as exc:
            if exc.code != "not_paired":
                raise
            return {"contract_version": CONTRACT, "state": "waiting", "via": "key", "engine": None,
                    "node": None, "finding": enginecodes.finding(
                        "key_not_installed", None, user=item["user"], host=item["host"])}
        self._drop(b["plan"])
        return out

    def pair_abandon(self, body) -> dict:
        b = _fields(body, {"pairing"}, ("pairing",))
        if not isinstance(b["pairing"], str) or not PENDING_RE.match(b["pairing"]):
            raise ERR("bad_request", "pairing is the id the first call answered")
        return {"contract_version": CONTRACT, "abandoned": self._drop(b["pairing"])}
