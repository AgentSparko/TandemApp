"""Engine host control (SRV-839): the engine's control verbs behind one interface, whatever reaches
the engine host.

    ctl = SshControl(SshHost(...), state_dir, key=loader)   `tandem remote` over SSH (forced command)
    ctl = LocalControl(engine_dir)                          `bin/tandem remote` on this machine
    ctl = NodeControl(client, engine="engine")              a TandemNode agent (legacy, below)
    ctl = for_node(row, ...)                                the adapter a fleet record names

    ctl.status()                      ctl.mode_get()
    ctl.start() / stop()              ctl.mode_set(mode, ext=, when=, deadline=)
    ctl.restart(when=, deadline=)     ctl.settings_get(keys)
    ctl.logs(lines=, which=)          ctl.settings_plan(changes, unset=)
    ctl.follow_logs(lines=, which=)   ctl.settings_apply(changes, unset=, confirm=,
    ctl.doctor()                                         confirm_fingerprint=, when=, deadline=)
    ctl.unpair()                      ctl.settings_versions()
    ctl.close()                       ctl.settings_restore(n, confirm=, confirm_fingerprint=, ...)

THE CONTRACT is the engine's, contracts/engine-ctl-v1 (pinned, tests/test_contract_pin.py): one
command line in, one JSON document out (JSON lines for `logs --follow`), an exit status. VERBS below
is its verbs.json `verbs` table, held equal by tests/test_enginectl.py. Each method checks its
arguments against that grammar BEFORE anything runs (CtlBadCall), builds the command from the
checked tokens with shlex.join (never by interpolation), and returns the answer as a dict: for the
ssh and local adapters the engine's own document (`{ctl, version, verb, ok, ...}`, the verb's
schema), for the node adapter the fields TandemNode knows in the same places (`ctl` "tandemnode").
`doctor` returns its answer for exit 0 and 1 (`healthy` says which); everything else that is not
exit 0 raises.

ERRORS are typed and carry `kind`, `code`, `verb`, `exit` and `host` (`to_dict()` for an API):

    exit 1 CtlNotOk   2 CtlUsage   3 CtlRefused   4 CtlBusy (`job`)   5 CtlInternal
    CtlTimeout        the verb's time ran out (TIMEOUTS); the call's process group is killed
    CtlUnreachable    no SSH connection, or the agent does not answer
    CtlAuthRefused    the host refused this controller's key (not paired any more)
    HostKeyChanged    the host offers another key than the pinned one: `pinned` and `offered`
                      name both fingerprints; nothing ran
    CtlNotPaired      no pinned key or no private key here for that host
    CtlProtocol       an answer that is not one engine-ctl-v1 document, or disagrees with its exit
    CtlUnsupported    the adapter has no such verb (node), code `unsupported`

THE SSH ADAPTER runs the system's OpenSSH as proxy/sshonce.py does, never a Python SSH library.

- One connection per host: a master started with `-M -N -f` and ControlPersist, each call a
  client of it (`ControlMaster=no`; a dead master's socket makes ssh connect directly, so a call
  never fails for the multiplexing alone). The sockets sit in `<state>/ssh/cm` (0700); a path too
  long for a socket (ssh adds 17 characters while it binds) moves them to a 0700 directory of this
  user under the system's temporary directory, refused when another user owns it or it is a link.
- `-F none`: no ssh_config is read, so a `Host *` block (a ProxyCommand, ForwardAgent, a
  RemoteCommand) cannot change a control call; every option is on the command line.
- Host keys are pinned per host: `<state>/ssh/hosts/<host id>.known_hosts`, one line under the
  alias `tandem-engine-<host id>` (HostKeyAlias: a new address keeps the pin), StrictHostKeyChecking
  yes, HostKeyAlgorithms the pinned type, UpdateHostKeys no, no global file. Before each call the
  file's key must be the fleet record's fingerprint; a changed key is HostKeyChanged with both
  fingerprints (the offered one from ssh's own warning, else from ssh-keyscan).
- The private key: decrypted from the secret store (proxy/hostkeys.py) into a 0600 file in a 0700
  directory of this process, `<state>/ssh/run/<pid>/`, kept for the process's lifetime and removed
  by close() and at exit; a directory whose process is gone is removed at the next start. A
  process-scoped agent would keep it off the disk but adds a daemon to start, watch and kill, and
  holds the key for any process of the user just the same: the master key file beside the config
  database is readable to that user anyway (proxy/secrets.py). IdentitiesOnly and IdentityAgent
  none: only this key is offered, never the user's agent.
- BatchMode, publickey only, no forwarding of any kind, no terminal (-T), stdin from /dev/null.
- The remote command is ONE argument, shlex.join of the checked tokens; the engine splits it with
  shlex and no shell.

THE LOCAL ADAPTER runs `/bin/bash <engine dir>/bin/tandem remote TOKEN ...` (an argv, no shell), with
the SSH_* variables of this process's own login removed so the engine reads the arguments.

THE NODE ADAPTER keeps an engine controllable through its TandemNode agent until its host is paired
the new way: `status` is GET /node/engines/{id}, `start`, `stop` and `restart` (now only) are its
POSTs, `logs` (the engine's log, no follow) its relay of /v1/dashboard/logs. Every other verb, and
`restart` at idle or a time, `logs --follow` and `logs --which launcher`, is CtlUnsupported.

SECRETS. No call logs an argument's value: a log line names the host, the verb, the settings keys,
the exit and the time. The private key is never in an argv, an answer or an exception: what ssh
prints is scrubbed of it, and of the values a call carried, before it reaches a message.
"""

from __future__ import annotations

import atexit
import base64
import binascii
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from proxy import sshonce

CTL = "engine-ctl-v1"
#: the engine-ctl-v1 version this TandemApp speaks (verbs.json `version`; a later 1.x engine is fine)
SPEAKS = "1.0"

#: exit statuses (verbs.json `exit`)
EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_BUSY, EXIT_INTERNAL = 0, 1, 2, 3, 4, 5

MAX_LINE, MAX_TOKENS, MAX_TOKEN = 8192, 64, 1024
TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}$")
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
CONFIRM_RE = re.compile(r"^[0-9a-f]{16}$")
DEADLINE = (10, 86400)
LINES = (1, 5000)

_WHEN = {"--when": "when", "--deadline": f"int:{DEADLINE[0]}:{DEADLINE[1]}"}
_CONFIRM = {"--confirm": "confirm", "--confirm-fingerprint": "confirm"}
#: verb -> (options {name: kind}, positional spec, answer schema): verbs.json `verbs`
VERBS: dict = {
    "status": ({}, "none", "status.schema.json"),
    "start": ({}, "none", "job.schema.json"),
    "stop": ({}, "none", "job.schema.json"),
    "restart": (dict(_WHEN), "none", "job.schema.json"),
    "mode get": ({}, "none", "mode-get.schema.json"),
    "mode set": ({**_WHEN, "--ext": "module"}, "mode", "job.schema.json"),
    "settings get": ({}, "keys", "settings-get.schema.json"),
    "settings plan": ({"--unset": "key"}, "changes", "settings-plan.schema.json"),
    "settings apply": ({"--unset": "key", **_CONFIRM, **_WHEN}, "changes", "job.schema.json"),
    "settings versions": ({}, "none", "settings-versions.schema.json"),
    "settings restore": ({**_CONFIRM, **_WHEN}, "version", "job.schema.json"),
    "logs": ({"--follow": "flag", "--lines": f"int:{LINES[0]}:{LINES[1]}",
              "--which": "enum:engine|launcher"}, "none", "logs.schema.json"),
    "doctor": ({}, "none", "doctor.schema.json"),
    "unpair": ({}, "none", "unpair.schema.json"),
}
#: error code -> its exit status (verbs.json `errors`); a code of a later minor is taken as it comes
ERRORS: dict = {
    **{c: EXIT_USAGE for c in ("bad_command", "unknown_verb", "unknown_subcommand", "unknown_option",
                               "extra_argument", "missing_argument", "bad_value", "duplicate_option",
                               "unknown_key")},
    **{c: EXIT_REFUSED for c in ("no_command", "pty_refused", "forward_refused", "path_outside",
                                 "key_not_managed", "not_in_overlay", "no_change", "confirm_required",
                                 "confirm_mismatch", "hook_missing", "config_error", "unknown_version",
                                 "key_unknown", "key_ambiguous")},
    "busy": EXIT_BUSY,
    "internal": EXIT_INTERNAL,
}
#: seconds a verb may take, connection included (lifecycle verbs answer at once with their job)
TIMEOUTS: dict = {"status": 20.0, "start": 30.0, "stop": 30.0, "restart": 30.0, "mode get": 20.0,
                  "mode set": 30.0, "settings get": 30.0, "settings plan": 30.0,
                  "settings apply": 30.0, "settings versions": 20.0, "settings restore": 30.0,
                  "logs": 30.0, "doctor": 120.0, "unpair": 20.0}
#: a settings key whose value is never written to a log or an error (as the engine's journal)
SECRET_KEY = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY$|CREDENTIAL|BEARER|AUTH)")
#: the SSH host key types a pin may name (sshonce.PREFER)
HOST_KEY_TYPES = sshonce.PREFER
FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9][A-Za-z0-9-]{0,62}"
                         r"(?:\.[A-Za-z0-9][A-Za-z0-9-]{0,62})*\.?$")
IPV6_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}(%[A-Za-z0-9_.-]{1,32})?$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
#: ssh's own exit status for its own failures
SSH_FAILED = 255
#: how long a master connection stays after its last call
CONTROL_PERSIST_S = 300
CONNECT_TIMEOUT_S = 10
#: a control socket path at most this long (sun_path is 104 bytes on macOS; ssh binds the path plus
#: ".XXXXXXXXXXXXXXXX" first)
SOCKET_PATH_MAX = 80
#: the environment of this process's own SSH login: never handed to ssh or to the engine's CLI
SSH_ENV = ("SSH_ORIGINAL_COMMAND", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "SSH_AUTH_SOCK",
           "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_USER_AUTH", "DISPLAY")


# ======================================================================================== errors
class CtlError(Exception):
    """A verb that did not give its answer. `kind` names the class for an API."""

    kind = "error"

    def __init__(self, message: str, *, verb: str | None = None, code: str | None = None,
                 exit: int | None = None, host: str | None = None, answer: dict | None = None):
        super().__init__(message)
        self.message, self.verb, self.code, self.exit = message, verb, code, exit
        self.host, self.answer = host, answer

    def to_dict(self) -> dict:
        return {"type": self.kind, "code": self.code, "message": self.message, "verb": self.verb,
                "exit": self.exit, "host": self.host}


class CtlBadCall(CtlError, ValueError):
    """An argument this side refuses before anything runs."""
    kind = "bad_call"


class CtlNotOk(CtlError):
    kind = "not_ok"


class CtlUsage(CtlError):
    kind = "usage"


class CtlRefused(CtlError):
    kind = "refused"


class CtlBusy(CtlError):
    kind = "busy"

    @property
    def job(self) -> dict | None:
        err = (self.answer or {}).get("error") or {}
        return err.get("job") if isinstance(err.get("job"), dict) else None


class CtlInternal(CtlError):
    kind = "internal"


class CtlUnsupported(CtlError):
    kind = "unsupported"


class CtlTimeout(CtlError):
    kind = "timeout"


class CtlProtocol(CtlError):
    kind = "protocol"


class CtlTransport(CtlError):
    kind = "transport"


class CtlUnreachable(CtlTransport):
    kind = "unreachable"


class CtlAuthRefused(CtlTransport):
    kind = "auth_refused"


class CtlNotPaired(CtlTransport):
    kind = "not_paired"


class HostKeyChanged(CtlTransport):
    """The host offers another key than the pinned one. Nothing ran, and nothing connects anyway."""

    kind = "host_key_changed"

    def __init__(self, message: str, *, pinned: str | None, offered: str | None, **kw):
        super().__init__(message, code="host_key_changed", **kw)
        self.pinned, self.offered = pinned, offered

    def to_dict(self) -> dict:
        return {**super().to_dict(), "pinned": self.pinned, "offered": self.offered}


BY_EXIT = {EXIT_NOT_OK: CtlNotOk, EXIT_USAGE: CtlUsage, EXIT_REFUSED: CtlRefused,
           EXIT_BUSY: CtlBusy, EXIT_INTERNAL: CtlInternal}


# ======================================================================================== the grammar
@dataclass
class Call:
    """One checked verb: its name, the command's tokens, the values (for the node adapter), the
    settings keys it names (for the log) and the strings never to show (for messages)."""
    name: str
    tokens: list
    opts: dict = field(default_factory=dict)
    keys: list = field(default_factory=list)
    hide: list = field(default_factory=list)
    timeout: float = 30.0


def _bad(name: str, message: str) -> CtlBadCall:
    return CtlBadCall(f"{name}: {message}", verb=name, code="bad_call")


def check_when(value, name: str = "restart") -> str:
    """`now`, `idle`, `at=<ISO 8601 time with a zone>`, or an aware datetime (sent as `at=...Z`)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise _bad(name, "when: a time needs a zone")
        return "at=" + value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if value in ("now", "idle"):
        return value
    if isinstance(value, str) and value.startswith("at=") and TOKEN_RE.match(value):
        raw = value[3:]
        try:
            at = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            raise _bad(name, f"when: at= takes an ISO 8601 time") from None
        if at.tzinfo is None:
            raise _bad(name, "when: the time needs a zone (Z or +hh:mm)")
        return value
    raise _bad(name, "when is now, idle, at=<ISO time with a zone> or an aware datetime")


def _int(name: str, opt: str, v, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise _bad(name, f"{opt} takes a whole number {lo}..{hi}")
    return v


def _key(name: str, k) -> str:
    if not isinstance(k, str) or not KEY_RE.match(k):
        raise _bad(name, "a settings key is A-Z, 0-9 and _ (a letter first, 64 at most)")
    return k


def _value(name: str, k: str, v) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise _bad(name, f"{k}: a value is a string or a number")
    v = str(v)
    tok = f"{k}={v}"
    if len(tok) > MAX_TOKEN or (v and not TOKEN_RE.match(v)):
        raise _bad(name, f"{k}: a value is printable ASCII without a space, at most "
                         f"{MAX_TOKEN - len(k) - 1} characters")
    return v


def _confirm(name: str, opt: str, v) -> str:
    if not isinstance(v, str) or not CONFIRM_RE.match(v):
        raise _bad(name, f"{opt} takes the 16 hex digits a plan gave")
    return v


def _when_opts(name: str, when, deadline, toks: list, opts: dict) -> None:
    if when is not None:
        opts["when"] = check_when(when, name)
        toks += ["--when", opts["when"]]
    if deadline is not None:
        if when is None or opts["when"] == "now":
            raise _bad(name, "deadline is the wait for idle: it goes with when idle or at=")
        opts["deadline"] = _int(name, "deadline", deadline, *DEADLINE)
        toks += ["--deadline", str(deadline)]


def _changes(name: str, changes, unset) -> tuple[list, dict, list, list]:
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise _bad(name, "changes is a {KEY: value} mapping")
    unset = list(unset or ())
    toks, keys, hide = [], [], []
    seen = set()
    for k in unset:
        _key(name, k)
        if k in seen:
            raise _bad(name, f"{k} is given twice")
        seen.add(k)
        toks += ["--unset", k]
        keys.append(k)
    sets = {}
    for k, v in changes.items():
        _key(name, k)
        if k in seen:
            raise _bad(name, f"{k} is changed and unset")
        seen.add(k)
        sets[k] = _value(name, k, v)
        toks.append(f"{k}={sets[k]}")
        keys.append(k)
        if SECRET_KEY.search(k) and sets[k]:
            hide.append(sets[k])
    if not sets and not unset:
        raise _bad(name, "at least one change or unset")
    return toks, {"set": sets, "unset": unset}, keys, hide


def build(name: str, *, when=None, deadline=None, ext=None, mode=None, keys=None, changes=None,
          unset=None, confirm=None, confirm_fingerprint=None, version=None, follow=False,
          lines=None, which=None) -> Call:
    """The checked Call of verb `name`; CtlBadCall names what is wrong. Nothing runs here."""
    if name not in VERBS:
        raise _bad(name, f"no verb {name!r}")
    toks = name.split()
    opts: dict = {}
    names: list = []
    hide: list = []
    if name == "restart":
        _when_opts(name, when, deadline, toks, opts)
    elif name == "mode set":
        if mode not in ("engine", "chat"):
            raise _bad(name, "mode is engine or chat")
        opts["mode"] = mode
        toks.append(mode)
        if ext is not None:
            if mode != "chat":
                raise _bad(name, "ext goes with mode chat")
            if not isinstance(ext, str) or not MODULE_RE.match(ext):
                raise _bad(name, "ext is a dotted module name")
            opts["ext"] = ext
            toks += ["--ext", ext]
        _when_opts(name, when, deadline, toks, opts)
    elif name == "settings get":
        names = [_key(name, k) for k in (keys or ())]
        toks += names
        opts["keys"] = names
    elif name in ("settings plan", "settings apply"):
        ct, opts["changes"], names, hide = _changes(name, changes, unset)
        toks += ct
        if name == "settings apply":
            toks += ["--confirm", _confirm(name, "confirm", confirm)]
            if confirm_fingerprint is not None:
                toks += ["--confirm-fingerprint",
                         _confirm(name, "confirm_fingerprint", confirm_fingerprint)]
            _when_opts(name, when, deadline, toks, opts)
    elif name == "settings restore":
        opts["version"] = _int(name, "version", version, 0, 999999)
        toks.append(str(version))
        toks += ["--confirm", _confirm(name, "confirm", confirm)]
        if confirm_fingerprint is not None:
            toks += ["--confirm-fingerprint",
                     _confirm(name, "confirm_fingerprint", confirm_fingerprint)]
        _when_opts(name, when, deadline, toks, opts)
    elif name == "logs":
        if follow:
            toks.append("--follow")
            opts["follow"] = True
        if lines is not None:
            opts["lines"] = _int(name, "lines", lines, *LINES)
            toks += ["--lines", str(lines)]
        if which is not None:
            if which not in ("engine", "launcher"):
                raise _bad(name, "which is engine or launcher")
            opts["which"] = which
            toks += ["--which", which]
    if len(toks) > MAX_TOKENS:
        raise _bad(name, f"more than {MAX_TOKENS} arguments")
    for t in toks:
        if len(t) > MAX_TOKEN or not TOKEN_RE.match(t):
            raise _bad(name, "an argument holds a space, a non-ASCII or a control character")
    if len(shlex.join(toks)) > MAX_LINE:
        raise _bad(name, f"the command is longer than {MAX_LINE} characters")
    return Call(name=name, tokens=toks, opts=opts, keys=names, hide=hide,
                timeout=TIMEOUTS.get(name, 30.0))


def scrub(text: str, hide: list) -> str:
    """`text` with every hidden string (a private key's lines, a secret value) cut out."""
    return sshonce.scrub(text or "", [h for h in hide if h])


# ======================================================================================== answers
def parse_answer(name: str, rc: int, out: str, *, host: str | None = None,
                 hide: list | None = None) -> dict:
    """The engine's document for exit `rc`, or the typed error. Exit 0 is an answer; exit 1 is one
    for doctor (CtlNotOk otherwise); 2 to 5 carry `error` {code, message}. Anything else, or an
    answer that disagrees with its exit, is CtlProtocol."""
    hide = hide or []
    ctx = {"verb": name, "exit": rc, "host": host}
    if rc not in (EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_BUSY, EXIT_INTERNAL):
        raise CtlProtocol(f"{name}: tandem remote exited {rc}, not a status engine-ctl-v1 knows "
                          f"(is the engine's control CLI installed there?)", **ctx)
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if len(lines) != 1:
        raise CtlProtocol(f"{name}: exit {rc} with {len(lines)} answer lines, not one JSON document",
                          **ctx)
    try:
        doc = json.loads(lines[0])
    except ValueError:
        raise CtlProtocol(f"{name}: the answer is not JSON", **ctx) from None
    _envelope(name, doc, ctx)
    if rc == EXIT_OK:
        if doc["ok"] is not True or "error" in doc:
            raise CtlProtocol(f"{name}: exit 0 with an answer that is not ok", **ctx)
        return doc
    if rc == EXIT_NOT_OK:
        if doc["ok"] is not False or "error" in doc:
            raise CtlProtocol(f"{name}: exit 1 with an answer that is ok or an error", **ctx)
        if name == "doctor":
            return doc
        raise CtlNotOk(f"{name}: answered, not ok", answer=doc, **ctx)
    err = doc.get("error")
    if doc["ok"] is not False or not isinstance(err, dict) or not isinstance(err.get("code"), str) \
            or not isinstance(err.get("message"), str):
        raise CtlProtocol(f"{name}: exit {rc} without an error {{code, message}}", **ctx)
    code = err["code"]
    if ERRORS.get(code, rc) != rc:
        raise CtlProtocol(f"{name}: exit {rc} with error {code}, which the contract gives exit "
                          f"{ERRORS[code]}: the answer and the status disagree", code=code, **ctx)
    msg = scrub(err["message"], hide)[:500]
    raise BY_EXIT[rc](f"{name}: {msg} ({code})", code=code, answer=doc, **ctx)


def _envelope(name: str, doc, ctx: dict) -> None:
    if not isinstance(doc, dict) or doc.get("ctl") != CTL or not isinstance(doc.get("ok"), bool):
        raise CtlProtocol(f"{name}: the answer is not an {CTL} document", **ctx)
    v = doc.get("version")
    if name == "status" and doc.get("ok") is True:
        pass        # status's body puts the engine's release in `version` (its schema: string|null)
    elif not isinstance(v, str) or not re.fullmatch(r"1\.[0-9]+", v):
        raise CtlProtocol(f"{name}: the answer speaks {CTL} {str(v)[:20]!r}; this TandemApp speaks "
                          f"{SPEAKS} (any 1.x)", **ctx)
    if doc.get("verb") not in (name, None):
        raise CtlProtocol(f"{name}: the answer is for {str(doc.get('verb'))[:40]!r}", **ctx)


def parse_line(name: str, text: str, *, host: str | None = None) -> dict:
    """One line of `logs --follow`: {ctl, verb logs, which, seq, line}."""
    ctx = {"verb": name, "host": host}
    try:
        doc = json.loads(text)
    except ValueError:
        raise CtlProtocol(f"{name}: a followed line is not JSON", **ctx) from None
    if not isinstance(doc, dict) or doc.get("ctl") != CTL or doc.get("verb") != "logs" or \
            not isinstance(doc.get("seq"), int) or not isinstance(doc.get("line"), str):
        raise CtlProtocol(f"{name}: a followed line is not a logs line", **ctx)
    return doc


# ======================================================================================== the interface
class EngineHostControl:
    """The verbs as methods. An adapter gives `_run(call)` (and `_follow(call)` for a stream)."""

    transport = "?"

    def __init__(self, *, label: str | None = None, log=None, timeouts: dict | None = None):
        self.label = label or self.transport
        self._log = log
        self.timeouts = dict(TIMEOUTS, **(timeouts or {}))

    # ------------------------------------------------------------------ the verbs
    def status(self) -> dict:
        return self._do(build("status"))

    def start(self) -> dict:
        return self._do(build("start"))

    def stop(self) -> dict:
        return self._do(build("stop"))

    def restart(self, when=None, deadline: int | None = None) -> dict:
        return self._do(build("restart", when=when, deadline=deadline))

    def mode_get(self) -> dict:
        return self._do(build("mode get"))

    def mode_set(self, mode: str, ext: str | None = None, when=None,
                 deadline: int | None = None) -> dict:
        return self._do(build("mode set", mode=mode, ext=ext, when=when, deadline=deadline))

    def settings_get(self, keys=()) -> dict:
        return self._do(build("settings get", keys=keys))

    def settings_plan(self, changes: dict | None = None, unset=()) -> dict:
        return self._do(build("settings plan", changes=changes, unset=unset))

    def settings_apply(self, changes: dict | None = None, unset=(), *, confirm: str,
                       confirm_fingerprint: str | None = None, when=None,
                       deadline: int | None = None) -> dict:
        return self._do(build("settings apply", changes=changes, unset=unset, confirm=confirm,
                              confirm_fingerprint=confirm_fingerprint, when=when,
                              deadline=deadline))

    def settings_versions(self) -> dict:
        return self._do(build("settings versions"))

    def settings_restore(self, version: int, *, confirm: str,
                         confirm_fingerprint: str | None = None, when=None,
                         deadline: int | None = None) -> dict:
        return self._do(build("settings restore", version=version, confirm=confirm,
                              confirm_fingerprint=confirm_fingerprint, when=when,
                              deadline=deadline))

    def logs(self, lines: int | None = None, which: str | None = None) -> dict:
        return self._do(build("logs", lines=lines, which=which))

    def follow_logs(self, lines: int | None = None, which: str | None = None) -> "LogStream":
        """The log as it grows: iterate for each line's document; close() (or `with`) ends it."""
        call = build("logs", follow=True, lines=lines, which=which)
        self.log(f"[enginectl] {self.label}: logs --follow")
        return self._follow(call)

    def doctor(self) -> dict:
        return self._do(build("doctor"))

    def unpair(self) -> dict:
        return self._do(build("unpair"))

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ the call
    def log(self, line: str) -> None:
        if self._log is not None:
            self._log(line)

    def _do(self, call: Call) -> dict:
        call.timeout = self.timeouts.get(call.name, call.timeout)
        t0 = time.monotonic()
        what = call.name + (f" ({', '.join(call.keys)})" if call.keys else "")
        try:
            doc = self._run(call)
        except CtlError as exc:
            self.log(f"[enginectl] {self.label}: {what} -> {exc.kind}"
                     f"{' ' + exc.code if exc.code else ''}"
                     f"{f' exit {exc.exit}' if exc.exit is not None else ''} "
                     f"({int((time.monotonic() - t0) * 1000)} ms)")
            raise
        self.log(f"[enginectl] {self.label}: {what} -> ok={doc.get('ok')} "
                 f"({int((time.monotonic() - t0) * 1000)} ms)")
        return doc

    def _run(self, call: Call) -> dict:                                 # pragma: no cover
        raise NotImplementedError

    def _follow(self, call: Call) -> "LogStream":                      # pragma: no cover
        raise NotImplementedError


# ======================================================================================== processes
def _env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in SSH_ENV}


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        p.wait()


def run_process(argv: list, *, timeout: float, env: dict, name: str,
                host: str | None = None) -> tuple[int, str, str]:
    """argv in its own session with stdin from /dev/null: (exit, stdout, stderr), or CtlTimeout
    after `timeout` seconds with the whole process group killed."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as exc:
        raise CtlUnreachable(f"{name}: {os.path.basename(argv[0])} could not start "
                             f"({type(exc).__name__})", verb=name, host=host) from None
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        try:
            p.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass                                  # something outside the group holds a pipe
        raise CtlTimeout(f"{name}: no answer within {timeout:.0f} s; the call was ended",
                         verb=name, host=host, code="timeout") from None
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class LogStream:
    """`logs --follow` as it arrives: iterating yields each line's document. A refusal before the
    first line raises its typed error. close() ends the call (the remote side sees the channel
    close and stops)."""

    def __init__(self, proc: subprocess.Popen, call: Call, *, host: str | None = None,
                 on_fail=None, hide: list | None = None):
        self.proc, self.call, self.host = proc, call, host
        self.on_fail = on_fail                      # (rc, stderr) -> CtlError | None, for ssh
        self.hide = hide or []
        self._err = bytearray()
        self._closed = False
        self._drain = threading.Thread(target=self._read_err, name="enginectl-follow-err",
                                       daemon=True)
        self._drain.start()

    def _read_err(self) -> None:
        try:
            for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
                if len(self._err) < 65536:
                    self._err += chunk
        except (OSError, ValueError):
            pass

    def __iter__(self):
        name = self.call.name
        first = True
        while True:
            try:
                raw = self.proc.stdout.readline()
            except (OSError, ValueError):
                raw = b""
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            if first:
                first = False
                try:
                    doc = json.loads(text)
                except ValueError:
                    doc = None
                if isinstance(doc, dict) and "ok" in doc:
                    # a whole answer, not a line: the follow was refused before it began
                    parse_answer(name, self.proc.wait(), text, host=self.host, hide=self.hide)
                    raise CtlProtocol(f"{name}: the follow answered one document, not lines",
                                      verb=name, host=self.host)
            yield parse_line(name, text, host=self.host)
        if self._closed:
            return
        rc = self.proc.wait()
        self._drain.join(timeout=2)
        if self._closed or rc == 0:
            return
        err = scrub(bytes(self._err).decode("utf-8", "replace"), self.hide)
        got = self.on_fail(rc, err) if self.on_fail else None
        raise got or CtlProtocol(f"{name}: the follow ended with exit {rc}", verb=name, exit=rc,
                                 host=self.host)

    def close(self) -> None:
        self._closed = True
        if self.proc.poll() is None:
            _kill(self.proc)
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _popen(argv: list, env: dict, name: str, host: str | None) -> subprocess.Popen:
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
    except OSError as exc:
        raise CtlUnreachable(f"{name}: {os.path.basename(argv[0])} could not start "
                             f"({type(exc).__name__})", verb=name, host=host) from None


# ======================================================================================== local
class LocalControl(EngineHostControl):
    """`bin/tandem remote` of an engine installed on this machine, as an argv."""

    transport = "local"

    def __init__(self, engine_dir: str, *, bash: str = "/bin/bash", **kw):
        super().__init__(**kw)
        if not isinstance(engine_dir, str) or not os.path.isabs(engine_dir):
            raise CtlBadCall("the engine's directory is an absolute path", code="bad_call")
        self.engine_dir = os.path.normpath(engine_dir)
        self.tandem = os.path.join(self.engine_dir, "bin", "tandem")
        self.bash = bash

    def argv(self, call: Call) -> list:
        return [self.bash, self.tandem, "remote", *call.tokens]

    def _check(self, call: Call) -> None:
        if not os.path.isfile(self.tandem):
            raise CtlNotPaired(f"{call.name}: no engine at {self.engine_dir} (bin/tandem is "
                               f"missing)", verb=call.name, code="no_engine", host=self.label)

    def _run(self, call: Call) -> dict:
        self._check(call)
        rc, out, _err = run_process(self.argv(call), timeout=call.timeout, env=_env(),
                                    name=call.name, host=self.label)
        return parse_answer(call.name, rc, out, host=self.label, hide=call.hide)

    def _follow(self, call: Call) -> LogStream:
        self._check(call)
        return LogStream(_popen(self.argv(call), _env(), call.name, self.label), call,
                         host=self.label, hide=call.hide)


# ======================================================================================== ssh
@dataclass(frozen=True)
class SshHost:
    """An engine host as the fleet record names it (its `trust`, kind ssh): no secret in here."""
    id: str
    host: str
    user: str
    key_id: str
    host_key_type: str
    host_key: str                      # the pinned host key's SHA256 fingerprint
    port: int = 22

    def __post_init__(self):
        check_ssh_host(self)

    @property
    def alias(self) -> str:
        return f"tandem-engine-{self.id}"

    @classmethod
    def from_trust(cls, host_id: str, trust: dict) -> "SshHost":
        t = trust or {}
        if t.get("kind") != "ssh":
            raise CtlNotPaired(f"{host_id} is not an engine host over SSH", code="not_ssh",
                               host=host_id)
        try:
            return cls(id=host_id, host=t["host"], port=t.get("port", 22), user=t["user"],
                       key_id=t["key_id"], host_key_type=t["host_key_type"],
                       host_key=t["host_key"])
        except KeyError as exc:
            raise CtlNotPaired(f"{host_id}: its SSH record lacks {exc.args[0]}", code="not_paired",
                               host=host_id) from None


def check_ssh_host(h: SshHost) -> None:
    def bad(what):
        raise CtlBadCall(f"engine host {str(h.id)[:64]!r}: {what}", code="bad_call")
    if not isinstance(h.id, str) or not HOST_ID_RE.match(h.id):
        bad("the id is letters, digits, . _ - (64 at most)")
    if not isinstance(h.host, str) or h.host.startswith("-") or not (
            HOSTNAME_RE.match(h.host) or (":" in h.host and IPV6_RE.match(h.host))):
        bad("the address is a host name or an IP address")
    if not isinstance(h.port, int) or isinstance(h.port, bool) or not 0 < h.port < 65536:
        bad("the port is 1..65535")
    if not isinstance(h.user, str) or not USER_RE.match(h.user):
        bad("the user is a login name")
    if not isinstance(h.key_id, str) or not KEY_ID_RE.match(h.key_id):
        bad("the key id is letters, digits, . _ : - (64 at most)")
    if h.host_key_type not in HOST_KEY_TYPES:
        bad(f"the host key type is one of {', '.join(HOST_KEY_TYPES)}")
    if not isinstance(h.host_key, str) or not FINGERPRINT_RE.fullmatch(h.host_key):
        bad("the host key is its SHA256 fingerprint")


def ssh_trust(*, host: str, user: str, key_id: str, host_key_type: str, host_key: str,
              port: int = 22, via: str = "ssh", paired_at: float | None = None) -> dict:
    """A fleet record's `trust` for an engine host over SSH (transport `ssh`). Public facts only:
    the address, the login, this controller's key id there and the pinned host key's fingerprint."""
    t = {"kind": "ssh", "via": via, "host": host, "port": port, "user": user, "key_id": key_id,
         "host_key_type": host_key_type, "host_key": host_key,
         "paired_at": time.time() if paired_at is None else paired_at}
    SshHost.from_trust("check", t)
    if via not in ("code", "ssh"):
        raise CtlBadCall("via is code or ssh", code="bad_call")
    return t


def ssh_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "ssh")


def known_hosts_path(state_dir: str, host_id: str) -> str:
    if not HOST_ID_RE.match(host_id or ""):
        raise CtlBadCall("bad engine host id", code="bad_call")
    return os.path.join(ssh_dir(state_dir), "hosts", f"{host_id}.known_hosts")


def _private_dir(path: str) -> str:
    """`path` as a directory of this user with mode 0700, made when missing; refused when it is a
    link, another user's, or open to others."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise CtlBadCall(f"{path} is not a directory of this user", code="unsafe_dir")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _write_private(path: str, text: str) -> None:
    """`text` to `path` (0600), through a new file in the same 0700 directory and a rename."""
    d = _private_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pinned_key(state_dir: str, host_id: str) -> tuple[str, str] | None:
    """(type, base64 blob) of the host key pinned for `host_id`, or None."""
    try:
        with open(known_hosts_path(state_dir, host_id)) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == f"tandem-engine-{host_id}":
                    return parts[1], parts[2]
    except FileNotFoundError:
        return None
    return None


def pin_host_key(state_dir: str, host_id: str, kind: str, b64: str, *,
                 replace: bool = False) -> str:
    """Pin `kind`/`b64` as `host_id`'s host key (its own known_hosts file); its fingerprint. A
    different key already pinned is HostKeyChanged unless `replace` (a new pairing)."""
    if kind not in HOST_KEY_TYPES:
        raise CtlBadCall(f"host key type {str(kind)[:40]!r} is not one of {', '.join(HOST_KEY_TYPES)}",
                         code="bad_call")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise CtlBadCall("the host key does not decode", code="bad_call") from None
    if not raw or len(raw) > 4096:
        raise CtlBadCall("the host key does not decode", code="bad_call")
    fp = sshonce.fingerprint(b64)
    old = pinned_key(state_dir, host_id)
    if old is not None and old != (kind, b64) and not replace:
        raise HostKeyChanged(f"{host_id}: another host key is pinned ({sshonce.fingerprint(old[1])}); "
                             f"pair the host again to replace it", pinned=sshonce.fingerprint(old[1]),
                             offered=fp, host=host_id)
    _private_dir(ssh_dir(state_dir))
    _write_private(known_hosts_path(state_dir, host_id), f"tandem-engine-{host_id} {kind} {b64}\n")
    return fp


def forget_host(state_dir: str, host_id: str) -> None:
    """The pin of `host_id` goes (the host left the fleet)."""
    try:
        os.unlink(known_hosts_path(state_dir, host_id))
    except FileNotFoundError:
        pass


# a private key file per (state dir, host) for this process's lifetime
_KEYS_LOCK = threading.Lock()
_KEYS: dict = {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_dir(state_dir: str) -> str:
    base = _private_dir(os.path.join(_private_dir(ssh_dir(state_dir)), "run"))
    mine = str(os.getpid())
    for name in os.listdir(base):
        if name == mine or not name.isdigit():
            continue
        p = os.path.join(base, name)
        st = os.lstat(p)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not _alive(int(name)):
            shutil.rmtree(p, ignore_errors=True)          # a process that is gone left it
    return _private_dir(os.path.join(base, mine))


def key_file(state_dir: str, host_id: str, pem: str) -> str:
    """The 0600 file of `host_id`'s private key for this process; rewritten when the key changes."""
    digest = hashlib.sha256(pem.encode()).hexdigest()
    with _KEYS_LOCK:
        got = _KEYS.get((state_dir, host_id))
        if got and got[1] == digest and os.path.isfile(got[0]):
            return got[0]
        path = os.path.join(_run_dir(state_dir), f"{host_id}.key")
        _write_private(path, pem.strip() + "\n")
        _KEYS[(state_dir, host_id)] = (path, digest)
        return path


def drop_key_file(state_dir: str, host_id: str) -> None:
    with _KEYS_LOCK:
        got = _KEYS.pop((state_dir, host_id), None)
    if got:
        for f in (os.unlink, os.rmdir):                  # the file, then its directory if empty
            try:
                f(got[0] if f is os.unlink else os.path.dirname(got[0]))
            except OSError:
                pass


@atexit.register
def _drop_all() -> None:
    with _KEYS_LOCK:
        items = list(_KEYS.items())
        _KEYS.clear()
    for (state_dir, _h), (path, _d) in items:
        try:
            os.unlink(path)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


def control_dir(state_dir: str) -> str:
    """Where the master sockets live: `<state>/ssh/cm`, or this user's 0700 directory under the
    system's temporary directory when that path is too long for a socket."""
    d = os.path.join(ssh_dir(state_dir), "cm")
    if len(d) + 1 + 18 <= SOCKET_PATH_MAX:
        _private_dir(ssh_dir(state_dir))
        return _private_dir(d)
    base = tempfile.gettempdir()
    if len(base) > 40:
        base = "/tmp"
    return _private_dir(os.path.join(base, f"tandem-cm-{os.getuid()}"))


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


#: what ssh says when the key it got is not the one it was told to expect
_HOSTKEY_SIGNS = ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed",
                  "no matching host key type found", "host key for")
_AUTH_SIGNS = ("Permission denied", "Too many authentication failures")
_NET_SIGNS = ("Connection refused", "Connection timed out", "timed out", "No route to host",
              "Could not resolve", "Name or service not known", "Network is unreachable",
              "Connection closed", "Connection reset", "kex_exchange_identification",
              "Host is down")


class SshControl(EngineHostControl):
    """`tandem remote` on an engine host over SSH: one multiplexed connection, a pinned host key,
    this controller's key for that host. `key` is a callable that gives the private key (OpenSSH
    PEM) when a call needs it (proxy/hostkeys.py `private`)."""

    transport = "ssh"

    def __init__(self, host: SshHost, state_dir: str, *, key, ssh: str = "ssh",
                 keyscan: str = "ssh-keyscan", **kw):
        kw.setdefault("label", host.id)
        super().__init__(**kw)
        self.host, self.state_dir = host, state_dir
        self._key = key
        self.ssh, self.keyscan = ssh, keyscan
        self._lock = threading.Lock()
        self._sock = None

    # ------------------------------------------------------------------ the pieces
    @property
    def control_path(self) -> str:
        if self._sock is None:
            h = hashlib.sha256(f"{self.state_dir}\0{self.host.id}".encode()).hexdigest()[:16]
            self._sock = os.path.join(control_dir(self.state_dir), f"h-{h}")
        return self._sock

    def _known_hosts(self, name: str) -> str:
        path = known_hosts_path(self.state_dir, self.host.id)
        got = pinned_key(self.state_dir, self.host.id)
        if got is None:
            raise CtlNotPaired(f"{name}: no host key is pinned for {self.host.id}: pair it",
                               verb=name, code="not_pinned", host=self.label)
        kind, b64 = got
        fp = sshonce.fingerprint(b64)
        if kind != self.host.host_key_type or fp != self.host.host_key:
            raise HostKeyChanged(f"{name}: the host key pinned on disk ({fp}) is not the fleet's "
                                 f"({self.host.host_key}); nothing ran", pinned=self.host.host_key,
                                 offered=fp, verb=name, host=self.label)
        return path

    def _identity(self, name: str) -> tuple[str, str]:
        try:
            pem = self._key()
        except CtlError:
            raise
        except Exception as exc:                                       # noqa: BLE001
            raise CtlNotPaired(f"{name}: this controller's key for {self.host.id} cannot be read "
                               f"({type(exc).__name__})", verb=name, code="no_key",
                               host=self.label) from None
        if not pem or "PRIVATE KEY" not in pem:
            raise CtlNotPaired(f"{name}: no key of this controller for {self.host.id}: pair it",
                               verb=name, code="no_key", host=self.label)
        return key_file(self.state_dir, self.host.id, pem), pem

    def base_argv(self, known_hosts: str, identity: str) -> list:
        h = self.host
        return [self.ssh, "-F", "none", "-T", "-p", str(h.port), "-l", h.user, "-i", identity,
                "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
                "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", f"HostKeyAlias={h.alias}",
                "-o", f"HostKeyAlgorithms={_algos(h.host_key_type)}", "-o", "UpdateHostKeys=no",
                "-o", "CheckHostIP=no", "-o", "VerifyHostKeyDNS=no",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
                "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no",
                "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3", "-o", "LogLevel=ERROR"]

    def master_argv(self, known_hosts: str, identity: str) -> list:
        return self.base_argv(known_hosts, identity) + [
            "-M", "-N", "-f", "-o", "ControlMaster=yes", "-o", f"ControlPath={self.control_path}",
            "-o", f"ControlPersist={CONTROL_PERSIST_S}", "--", self.host.host]

    def call_argv(self, call: Call, known_hosts: str, identity: str) -> list:
        return self.base_argv(known_hosts, identity) + [
            "-o", "ControlMaster=no", "-o", f"ControlPath={self.control_path}",
            "--", self.host.host, shlex.join(call.tokens)]

    # ------------------------------------------------------------------ the connection
    def _ensure_master(self, kh: str, identity: str, pem: str, name: str, timeout: float) -> None:
        if _is_socket(self.control_path):
            return
        with self._lock:
            if _is_socket(self.control_path):
                return
            if os.path.lexists(self.control_path):
                os.unlink(self.control_path)             # not a socket: ours to replace
            with tempfile.TemporaryFile() as errf:
                try:
                    p = subprocess.run(self.master_argv(kh, identity), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=errf, env=_env(),
                                       timeout=timeout, start_new_session=True)
                except subprocess.TimeoutExpired:
                    raise CtlTimeout(f"{name}: no SSH connection to {self.host.id} within "
                                     f"{timeout:.0f} s", verb=name, code="timeout",
                                     host=self.label) from None
                except OSError as exc:
                    raise CtlUnreachable(f"{name}: ssh could not start ({type(exc).__name__})",
                                         verb=name, host=self.label) from None
                errf.seek(0)
                err = errf.read(65536).decode("utf-8", "replace")
            if p.returncode != 0:
                raise self._ssh_error(p.returncode, scrub(err, [pem]), name) or CtlUnreachable(
                    f"{name}: ssh to {self.host.id} failed (exit {p.returncode})", verb=name,
                    exit=p.returncode, host=self.label)

    def _ssh_error(self, rc: int, err: str, name: str) -> CtlError | None:
        """The typed error of ssh's own failure (exit 255), None for another status."""
        if rc != SSH_FAILED:
            return None
        ctx = {"verb": name, "exit": rc, "host": self.label}
        last = " ".join(ln.strip() for ln in err.splitlines()[-3:] if ln.strip())[:300]
        if any(s.lower() in err.lower() for s in _HOSTKEY_SIGNS):
            seen = [fp for fp in FINGERPRINT_RE.findall(err) if fp != self.host.host_key]
            offered = seen[0] if seen else self._offered()
            return HostKeyChanged(
                f"{name}: {self.host.id} offers another SSH host key than the one pinned: pinned "
                f"{self.host.host_key}, offered {offered or 'unknown'}; nothing ran. If the host "
                f"was reinstalled, pair it again", pinned=self.host.host_key, offered=offered, **ctx)
        if any(s in err for s in _AUTH_SIGNS):
            return CtlAuthRefused(f"{name}: {self.host.id} refused this controller's key "
                                  f"({last or 'permission denied'}); pair it again",
                                  code="auth_refused", **ctx)
        if any(s in err for s in _NET_SIGNS):
            return CtlUnreachable(f"{name}: no SSH connection to {self.host.id} at "
                                  f"{self.host.host}:{self.host.port} ({last})",
                                  code="unreachable", **ctx)
        return CtlUnreachable(f"{name}: ssh to {self.host.id} failed ({last or 'exit 255'})",
                              code="ssh_failed", **ctx)

    def _offered(self) -> str | None:
        """The fingerprint the host offers for the pinned type, read with ssh-keyscan."""
        try:
            keys = sshonce.Ssh(os.devnull, keyscan=self.keyscan).scan(self.host.host,
                                                                       self.host.port)
        except sshonce.SshError:
            return None
        b64 = keys.get(self.host.host_key_type) or next(iter(keys.values()), None)
        return sshonce.fingerprint(b64) if b64 else None

    def _prepare(self, call: Call) -> tuple[list, list]:
        kh = self._known_hosts(call.name)
        identity, pem = self._identity(call.name)
        self._ensure_master(kh, identity, pem, call.name, call.timeout)
        return self.call_argv(call, kh, identity), [pem, *call.hide]

    # ------------------------------------------------------------------ the verbs
    def _run(self, call: Call) -> dict:
        t0 = time.monotonic()
        argv, hide = self._prepare(call)
        left = max(1.0, call.timeout - (time.monotonic() - t0))
        rc, out, err = run_process(argv, timeout=left, env=_env(), name=call.name,
                                   host=self.label)
        err = scrub(err, hide)
        if "Control socket connect" in err and _is_socket(self.control_path):
            # the master is gone (ssh connected directly): the next call starts a new one
            try:
                os.unlink(self.control_path)
            except OSError:
                pass
        if rc == SSH_FAILED:
            raise self._ssh_error(rc, err, call.name)
        doc = parse_answer(call.name, rc, scrub(out, [hide[0]]), host=self.label, hide=hide)
        if call.name == "unpair":
            self.close()                                 # this key no longer opens the host
        return doc

    def _follow(self, call: Call) -> LogStream:
        argv, hide = self._prepare(call)
        p = _popen(argv, _env(), call.name, self.label)
        return LogStream(p, call, host=self.label, hide=hide,
                         on_fail=lambda rc, err: self._ssh_error(rc, err, call.name))

    def close(self) -> None:
        """Ends the master connection and removes this process's copy of the key."""
        path = self._sock
        if path and _is_socket(path):
            try:
                subprocess.run([self.ssh, "-F", "none", "-o", f"ControlPath={path}", "-O", "exit",
                                "--", self.host.host], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10, env=_env(),
                               start_new_session=True)
            except (OSError, subprocess.SubprocessError):
                pass
        drop_key_file(self.state_dir, self.host.id)


def _algos(kind: str) -> str:
    return "rsa-sha2-512,rsa-sha2-256,ssh-rsa" if kind == "ssh-rsa" else kind


# ======================================================================================== node
#: TandemNode's engine state -> engine-ctl-v1's
_NODE_STATE = {"up": "healthy", "down": "down", "starting": "loading", "restarting": "loading",
               "stopping": "draining", "unhealthy": "failed"}
_NODE_JOB = {"running": "acting", "done": "done", "failed": "failed"}


def _iso(t) -> str | None:
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class NodeControl(EngineHostControl):
    """An engine through its host's TandemNode agent (`client`: proxy/nodeclient.py's Client, or
    anything with its `json(method, path, body=None, timeout=None)`)."""

    transport = "node"

    def __init__(self, client, engine: str = "engine", **kw):
        super().__init__(**kw)
        if not isinstance(engine, str) or not HOST_ID_RE.match(engine):
            raise CtlBadCall("the engine id is letters, digits, . _ -", code="bad_call")
        self.client, self.engine = client, engine

    def _get(self, call: Call, method: str, path: str) -> tuple[int, dict | None]:
        try:
            st, doc = self.client.json(method, path, None, timeout=call.timeout)
        except TimeoutError:
            raise CtlTimeout(f"{call.name}: the node's agent gave no answer within "
                             f"{call.timeout:.0f} s", verb=call.name, code="timeout",
                             host=self.label) from None
        except Exception as exc:                                       # noqa: BLE001
            raise CtlUnreachable(f"{call.name}: the node's agent does not answer "
                                 f"({type(exc).__name__})", verb=call.name, code="unreachable",
                                 host=self.label) from None
        if st >= 400:
            err = (doc or {}).get("error") or {}
            kind = str(err.get("type") or f"http_{st}")[:40]
            msg = str(err.get("message") or "")[:300]
            cls = CtlBusy if kind == "busy" else CtlRefused if st in (403, 404, 409) else \
                CtlUnreachable if st in (502, 503, 504) else CtlInternal
            raise cls(f"{call.name}: the node's agent answered {st} {kind}"
                      f"{': ' + msg if msg else ''}", verb=call.name, code=kind, host=self.label)
        return st, doc if isinstance(doc, dict) else None

    def _unsupported(self, call: Call, what: str | None = None) -> CtlUnsupported:
        return CtlUnsupported(f"{call.name}: {what or 'TandemNode has no such verb'}; pair the engine "
                              f"host over SSH to use it", verb=call.name, code="unsupported",
                              host=self.label)

    def _answer(self, call: Call, body: dict) -> dict:
        return {"ctl": "tandemnode", "version": None, "verb": call.name, "ok": True, **body}

    def _job(self, call: Call, job: dict | None) -> dict:
        j = job or {}
        return {"id": str(j.get("id") or ""), "kind": j.get("action") or call.name,
                "state": _NODE_JOB.get(j.get("state"), "acting"), "when": {"mode": "now"},
                "created_at": _iso(j.get("started_at")), "ended_at": _iso(j.get("ended_at")),
                "message": j.get("message")}

    def _run(self, call: Call) -> dict:
        base = f"/node/engines/{self.engine}"
        if call.name == "status":
            _st, v = self._get(call, "GET", base)
            v = v or {}
            h = v.get("health") or {}
            state = _NODE_STATE.get(v.get("state"), "down")
            if v.get("state") == "unhealthy" and h.get("http") == 503:
                state = "draining"
            job = v.get("job")
            return self._answer(call, {
                "engine": {"state": state, "pid": v.get("pid"), "port": v.get("port"),
                           "health_code": h.get("http"), "engine_api": h.get("engine_api"),
                           "version": h.get("version"), "model": h.get("model"),
                           "running_mode": None, "stopped_on_purpose": False},
                "managed": bool(v.get("managed")),
                "job": self._job(call, job) if job else None})
        if call.name in ("start", "stop", "restart"):
            if call.opts.get("when", "now") != "now":
                raise self._unsupported(call, "TandemNode restarts now only")
            _st, job = self._get(call, "POST", f"{base}/{call.name}")
            return self._answer(call, {"job": self._job(call, job), "follow": "status"})
        if call.name == "logs":
            if call.opts.get("follow"):
                raise self._unsupported(call, "TandemNode does not follow the log here")
            if call.opts.get("which", "engine") != "engine":
                raise self._unsupported(call, "TandemNode keeps no launcher log")
            _st, doc = self._get(call, "GET", f"{base}/v1/dashboard/logs?follow=0")
            rows = [r for r in ((doc or {}).get("lines") or []) if isinstance(r, dict)]
            n = call.opts.get("lines", 100)
            lines = [f"{r.get('ts', '')} {r.get('level', '')} [{r.get('source', '')}] "
                     f"{r.get('msg', '')}".strip() for r in rows[-n:]]
            return self._answer(call, {"which": "engine", "file": None, "lines": lines})
        raise self._unsupported(call)

    def _follow(self, call: Call) -> LogStream:
        raise self._unsupported(call, "TandemNode does not follow the log here")


# ======================================================================================== the factory
def for_node(row: dict, *, state_dir: str, keys=None, engine_dir: str | None = None,
             agent=None, engine: str = "engine", log=None, **kw) -> EngineHostControl:
    """The adapter a fleet record names: transport `ssh` (its trust; `keys` is proxy/hostkeys.py's
    store), this machine's engine (`endpoint` local with `engine_dir`), else the node's TandemNode
    agent (`agent`: its nodesource source or a client)."""
    t = row.get("trust") or {}
    if row.get("transport") == "ssh" or t.get("kind") == "ssh":
        if keys is None:
            raise CtlNotPaired(f"{row.get('id')}: no key store", code="no_key", host=row.get("id"))
        host = SshHost.from_trust(row["id"], t)
        return SshControl(host, state_dir, key=lambda: keys.private(row["id"]), log=log, **kw)
    if row.get("endpoint") == "local" and engine_dir:
        return LocalControl(engine_dir, label=row.get("id"), log=log, **kw)
    client = getattr(agent, "client", agent)
    if client is None or not hasattr(client, "json"):
        raise CtlNotPaired(f"{row.get('id')}: no TandemNode agent answers for it and it is not "
                           f"paired over SSH", code="not_paired", host=row.get("id"))
    return NodeControl(client, engine, label=row.get("id"), log=log, **kw)
