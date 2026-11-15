"""SRV-839: engine host control (proxy/enginectl.py), its key pairs (proxy/hostkeys.py) and the fleet's
transport `ssh`, on fakes.

The engine side is tests/fake_tandem_remote.py: a made-up install whose bin/tandem answers
contracts/engine-ctl-v1 (every answer here is held to the pinned schemas). SSH is
tests/fake_ssh.py first on PATH (or named): it checks the pinned host key and the -i key as
OpenSSH and sshd do, and runs the key line's forced command with SSH_ORIGINAL_COMMAND. The node
adapter talks to a fake agent over the same fake state. No network, no sshd, no engine.

  1  the grammar is the pinned contract's: verbs, options, answers, errors, exits, limits
                                                                  test_the_grammar_is_the_contract
  2  every bad argument is refused before anything runs, and a value with shell syntax arrives
     as one token, never run                                      test_bad_calls_run_nothing
  3  one suite, three adapters (local, ssh, node): status, lifecycle with idle and busy, mode,
     settings with its confirm, logs, the followed log, doctor (ok and not ok), unpair; the
     node adapter answers `unsupported` for what TandemNode cannot do
                                          test_suite_local, test_suite_ssh, test_suite_node
  4  the ssh command line: no config file, every safety option, one master reused, the key
     file 0600 in a 0700 directory of this process, `--` before the host, the command one
     argument (shlex.join), no agent or display handed on       test_ssh_command_line
  4b the system's own OpenSSH reads both command lines as meant (`ssh -G`, no connection)
                                                       test_real_openssh_takes_every_option
  5  host key pinning: a changed key names both fingerprints (from ssh, else from keyscan),
     another type, a disk pin that is not the fleet's, no pin; nothing ran on the engine
                                                                  test_host_key_pinning
  6  error mapping: unreachable, key refused, exits and answers that disagree, a newer error
     code, not ok, internal, a timeout that kills the call, a dead master  test_error_mapping
  7  the control socket's directory: short path here, long path in this user's 0700 directory
     under tmp, a link there refused; the key file's lifetime and a dead process's left-overs
                                                          test_sockets_and_key_files
  8  the key pairs: ed25519, key id, sealed with the host id as associated data, the
     authorized_keys line exactly as the engine's contract writes it   test_host_keys
  9  the fleet: an ssh node validates (nodes, node), pairs `via` ssh with its host key, has no
     agent; for_node picks each adapter; release unpairs and drops key and pin, force when it
     does not answer; a controller move's export carries no key pair  test_fleet_ssh_node
 10  no private key in any log line, exception, argv, answer, fleet record, audit row, ledger
     row or the config database's bytes; no secret-looking value in a log line or an error
                                                                  test_no_secret_anywhere

    python tests/test_enginectl.py
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_ssh  # noqa: E402
import fake_tandem_remote as ftr  # noqa: E402
from proxy import config as config_mod  # noqa: E402
from proxy import enginectl as E  # noqa: E402
from proxy import hostkeys, schema  # noqa: E402
from proxy import secrets as vault_mod  # noqa: E402

CTL_DIR = os.path.join(ROOT, "contracts", "engine-ctl-v1")
SECRET = "hf_FIXTURE_secret_value_0123456789"
HOST_ID = "eh-test-1"


def ctl_ok(doc: dict, name: str) -> dict:
    """`doc` holds to the pinned schema of verb `name`'s answer."""
    with open(os.path.join(CTL_DIR, E.VERBS[name][2])) as f:
        sch = json.load(f)
    errs = schema.validate(doc, sch)
    assert not errs, (name, errs[:5], doc)
    return doc


def raises(cls, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except cls as exc:
        return exc
    except Exception as exc:                                           # noqa: BLE001
        raise AssertionError(f"{fn}: expected {cls.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"{fn}: expected {cls.__name__}, nothing raised")


def host_pair() -> tuple[str, str]:
    """(type, b64) of a made-up SSH host key."""
    _pem, pub = hostkeys.generate()
    t, b64 = pub.split()[:2]
    return t, b64


class Rig:
    """A made-up engine install, a fake ssh, a config database with this controller's key for
    one engine host, the host key pinned, and the key's line in the fake server's
    authorized_keys."""

    def __init__(self, tmp: str, monkeypatch=None, short: bool = False):
        self.tmp = tmp
        self.engine = ftr.install(os.path.join(tmp, "engine"))
        self.bin = fake_ssh.install(os.path.join(tmp, "bin"))
        self.state = tempfile.mkdtemp(prefix="ectl-", dir="/tmp") if short else \
            os.path.join(tmp, "controller")
        os.makedirs(self.state, mode=0o700, exist_ok=True)
        self.cfg = config_mod.ConfigDB(os.path.join(tmp, "db", "config.sqlite3"), env={},
                                       use_keychain=False).open()
        self.keys = hostkeys.HostKeys(self.cfg)
        self.key = self.keys.create(HOST_ID)
        self.host_type, self.host_b64 = host_pair()
        self.fp = E.pin_host_key(self.state, HOST_ID, self.host_type, self.host_b64)
        self.ak = os.path.join(tmp, "authorized_keys")
        with open(self.ak, "w") as f:
            f.write(self.keys.authorized_line(HOST_ID, self.engine) + "\n")
        self.server = fake_ssh.server(os.path.join(tmp, "server.json"),
                                      host_key=[self.host_type, self.host_b64],
                                      authorized_keys=self.ak)
        self.sshlog = os.path.join(tmp, "ssh.log")
        self.logs: list[str] = []
        env = {"FAKE_SSH_SERVER": self.server, "FAKE_SSH_LOG": self.sshlog}
        for k, v in env.items():
            if monkeypatch is not None:
                monkeypatch.setenv(k, v)
            else:
                os.environ[k] = v

    def host(self, **kw) -> E.SshHost:
        args = dict(id=HOST_ID, host="engine-a.example", port=22, user="tandem",
                    key_id=self.key["key_id"], host_key_type=self.host_type, host_key=self.fp)
        args.update(kw)
        return E.SshHost(**args)

    def ssh_ctl(self, **kw) -> E.SshControl:
        kw.setdefault("keyscan", os.path.join(self.bin, "ssh-keyscan"))
        return E.SshControl(kw.pop("host", None) or self.host(), self.state,
                            key=lambda: self.keys.private(HOST_ID),
                            ssh=os.path.join(self.bin, "ssh"), log=self.logs.append, **kw)

    def local_ctl(self, **kw) -> E.LocalControl:
        return E.LocalControl(self.engine, log=self.logs.append, **kw)

    def node_ctl(self, **kw) -> E.NodeControl:
        return E.NodeControl(FakeAgent(self.engine), log=self.logs.append, **kw)

    def set(self, **kw):
        return ftr.set_state(self.engine, **kw)

    def calls(self) -> list[dict]:
        return ftr.calls(self.engine)

    def ssh_runs(self) -> list[dict]:
        return fake_ssh.log(self.sshlog)

    def pem(self) -> str:
        return self.keys.private(HOST_ID)


class FakeAgent:
    """A TandemNode agent's engine routes over the fake engine's state (proxy/nodeagent.py's
    shapes): GET /node/engines/{id}, POST .../start|stop|restart, the logs relay."""

    def __init__(self, engine: str, unreachable: bool = False):
        self.engine, self.unreachable = engine, unreachable
        self.seen: list = []

    def json(self, method, path, body=None, timeout=None):
        self.seen.append((method, path))
        if self.unreachable:
            raise ConnectionRefusedError("refused")
        st = ftr.state(self.engine)
        up = st.get("engine") == "healthy"
        if method == "GET" and path == "/node/engines/engine":
            return 200, {"id": "engine", "kind": "tandemllm", "port": 8000, "managed": True,
                         "state": "up" if up else "down", "pid": 4242 if up else None,
                         "health": {"http": 200, "status": "ok", "engine_api": "1.8",
                                    "version": "0.15.4", "model": "fake-model"} if up else None,
                         "job": None, "stage": None}
        if method == "POST" and path.startswith("/node/engines/engine/"):
            verb = path.rsplit("/", 1)[1]
            if st.get("busy"):
                return 409, {"error": {"type": "busy", "message": "engine: a restart runs"}}
            ftr.set_state(self.engine, engine="down" if verb == "stop" else "healthy")
            return 202, {"id": "engine-1", "action": verb, "state": "running", "who": "test",
                         "started_at": 1_800_000_000.0, "ended_at": None, "rc": None,
                         "message": None}
        if method == "GET" and path == "/node/engines/engine/v1/dashboard/logs?follow=0":
            return 200, {"contract_version": "1.0", "lines": [
                {"seq": i, "ts": "10:00:0%d" % i, "level": "info", "source": "server",
                 "msg": f"line {i}", "request_id": None} for i in range(1, 6)]}
        return 404, {"error": {"type": "not_found", "message": f"no route {method} {path}"}}


# ---------------------------------------------------------------------------- 1 the grammar

def test_the_grammar_is_the_contract():
    with open(os.path.join(CTL_DIR, "verbs.json")) as f:
        vj = json.load(f)
    assert vj["ctl"] == E.CTL and vj["version"] == E.SPEAKS, (vj["ctl"], vj["version"])
    assert set(vj["verbs"]) == set(E.VERBS), set(vj["verbs"]) ^ set(E.VERBS)
    for name, spec in vj["verbs"].items():
        opts, args, answer = E.VERBS[name]
        assert spec["options"] == opts, (name, spec["options"], opts)
        assert spec["arguments"] == args and spec["answer"] == answer, name
        assert os.path.isfile(os.path.join(CTL_DIR, answer)), answer
    by_class = {"usage": E.EXIT_USAGE, "refused": E.EXIT_REFUSED, "busy": E.EXIT_BUSY,
                "internal": E.EXIT_INTERNAL}
    want = {c: by_class[k] for k, codes in vj["errors"].items() for c in codes}
    assert want == E.ERRORS, set(want.items()) ^ set(E.ERRORS.items())
    assert sorted(vj["exit"]) == [str(i) for i in range(6)]
    line = vj["line"]
    assert (line["max_chars"], line["max_tokens"], line["max_token_chars"]) == \
        (E.MAX_LINE, E.MAX_TOKENS, E.MAX_TOKEN)
    assert set(E.TIMEOUTS) == set(E.VERBS)
    assert set(E.BY_EXIT) == {1, 2, 3, 4, 5}
    return f"{len(E.VERBS)} verbs, {len(E.ERRORS)} error codes, ctl {E.SPEAKS}"


# ---------------------------------------------------------------------------- 2 bad calls

def test_bad_calls_run_nothing(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    for ctl in (rig.ssh_ctl(), rig.local_ctl(), rig.node_ctl()):
        bad = [
            lambda: ctl.restart(when="soon"),
            lambda: ctl.restart(when=datetime(2026, 11, 16, 3, 0)),          # no zone
            lambda: ctl.restart(when="at=tomorrow"),
            lambda: ctl.restart(deadline=60),                               # no when
            lambda: ctl.restart(when="now", deadline=60),
            lambda: ctl.restart(when="idle", deadline=5),
            lambda: ctl.mode_set("turbo"),
            lambda: ctl.mode_set("engine", ext="tandemapp.ext"),
            lambda: ctl.mode_set("chat", ext="no-dashes"),
            lambda: ctl.settings_get(["lower"]),
            lambda: ctl.settings_plan({}),
            lambda: ctl.settings_plan({"MAX_QUEUE": "has space"}),
            lambda: ctl.settings_plan({"MAX_QUEUE": "nä"}),
            lambda: ctl.settings_plan({"MAX_QUEUE": True}),
            lambda: ctl.settings_plan({"A;B": "1"}),
            lambda: ctl.settings_plan({"MAX_QUEUE": "1"}, unset=["MAX_QUEUE"]),
            lambda: ctl.settings_plan({"MAX_QUEUE": "x" * 2000}),
            lambda: ctl.settings_apply({"MAX_QUEUE": "1"}, confirm="nothex"),
            lambda: ctl.settings_restore(-1, confirm="0123456789abcdef"),
            lambda: ctl.settings_restore(1, confirm="0123456789ABCDEF"),
            lambda: ctl.logs(lines=0),
            lambda: ctl.logs(lines=5001),
            lambda: ctl.logs(which="all"),
            lambda: ctl.follow_logs(which="kernel"),
        ]
        for i, fn in enumerate(bad):
            e = raises(E.CtlBadCall, fn)
            assert e.kind == "bad_call" and e.code == "bad_call", (i, e.to_dict())
    assert rig.calls() == [] and rig.ssh_runs() == [], "nothing ran"
    # shell syntax without a space is one token: it reaches the engine as written, never run
    marker = os.path.join(str(tmp_path), "PWNED")
    payload = f"$(touch${{IFS}}{marker})`id`;|&<>'\"\\"
    for ctl in (rig.local_ctl(), rig.ssh_ctl()):
        doc = ctl.settings_plan({"MAX_QUEUE": payload})
        ctl_ok(doc, "settings plan")
        ctl.close()
    got = [c["tokens"] for c in rig.calls()]
    assert got == [["settings", "plan", f"MAX_QUEUE={payload}"]] * 2, got
    assert not os.path.exists(marker)
    return f"{len(bad)} refusals x 3 adapters before anything ran; shell syntax arrived inert"


# ---------------------------------------------------------------------------- 3 the suite

def _suite(rig: Rig, ctl: E.EngineHostControl, kind: str) -> str:
    node = kind == "node"
    check = (lambda d, n: d) if node else ctl_ok
    st = check(ctl.status(), "status")
    assert st["ok"] is True and st["engine"]["state"] == "healthy", st
    assert st["ctl"] == ("tandemnode" if node else E.CTL)
    j = check(ctl.stop(), "stop")
    assert j["job"]["kind"] == "stop", j
    assert ctl.status()["engine"]["state"] == "down"
    check(ctl.start(), "start")
    assert ctl.status()["engine"]["state"] == "healthy"
    j = check(ctl.restart(), "restart")
    assert j["job"]["when"]["mode"] == "now", j
    if node:
        raises(E.CtlUnsupported, ctl.restart, when="idle", deadline=60)
    else:
        j = check(ctl.restart(when="idle", deadline=60), "restart")
        assert j["job"]["when"]["mode"] == "idle"
        at = datetime.fromisoformat("2030-01-01T03:00:00+00:00")
        j = check(ctl.restart(when=at), "restart")
        assert j["job"]["when"] == {"mode": "at", "at": "2030-01-01T03:00:00Z", "deadline_s": 600}
    # one lifecycle job at a time
    rig.set(busy=True)
    e = raises(E.CtlBusy, ctl.restart)
    assert e.kind == "busy" and e.code == "busy", e.to_dict()
    if not node:
        assert e.exit == E.EXIT_BUSY and e.job["state"] == "acting", e.to_dict()
    raises(E.CtlBusy, ctl.start)
    rig.set(busy=False)
    # mode and settings: the engine's own verbs
    if node:
        for fn in (ctl.mode_get, ctl.settings_versions, ctl.doctor, ctl.unpair,
                   lambda: ctl.mode_set("chat"), lambda: ctl.settings_get(["MAX_QUEUE"]),
                   lambda: ctl.settings_plan({"MAX_QUEUE": 4}),
                   lambda: ctl.settings_apply({"MAX_QUEUE": 4}, confirm="0123456789abcdef"),
                   lambda: ctl.settings_restore(0, confirm="0123456789abcdef"),
                   lambda: ctl.logs(which="launcher"), lambda: ctl.follow_logs()):
            e = raises(E.CtlUnsupported, fn)
            assert e.code == "unsupported" and "SSH" in e.message, e.to_dict()
        lg = ctl.logs(lines=3)
        assert lg["which"] == "engine" and len(lg["lines"]) == 3 and "line 5" in lg["lines"][-1], lg
        return "status, start, stop, restart now, busy and logs; the rest unsupported"
    m = ctl_ok(ctl.mode_get(), "mode get")
    assert m["mode"] == "engine"
    ctl_ok(ctl.mode_set("chat", ext="tandemapp.conductor", when="idle", deadline=120), "mode set")
    ctl_ok(ctl.settings_get(["MAX_QUEUE"]), "settings get")
    p = ctl_ok(ctl.settings_plan({"MAX_QUEUE": 4}, unset=["QSE_EXT"]), "settings plan")["plan"]
    a = ctl_ok(ctl.settings_apply({"MAX_QUEUE": 4}, unset=["QSE_EXT"], confirm=p["confirm"],
                                  when="idle"), "settings apply")
    assert a["job"]["kind"] == "settings"
    e = raises(E.CtlRefused, ctl.settings_apply, {"MAX_QUEUE": 4}, confirm="ffffffffffffffff")
    assert e.code == "confirm_mismatch" and e.exit == E.EXIT_REFUSED, e.to_dict()
    v = ctl_ok(ctl.settings_versions(), "settings versions")
    assert v["versions"][0]["n"] == 0
    ctl_ok(ctl.settings_restore(0, confirm=p["confirm"]), "settings restore")
    lg = ctl_ok(ctl.logs(lines=3, which="launcher"), "logs")
    assert lg["which"] == "launcher" and lg["lines"] == ["line 1", "line 2", "line 3"], lg
    # the followed log: every line, then the end; a waiting one ends on close()
    with ctl.follow_logs(lines=2) as s:
        got = list(s)
    assert [d["seq"] for d in got] == [1, 2, 3] and all(d["verb"] == "logs" for d in got), got
    for d in got:
        errs = schema.validate(d, json.load(open(os.path.join(CTL_DIR, "logs-line.schema.json"))))
        assert not errs, errs
    rig.set(follow_wait=True)
    s = ctl.follow_logs()
    it = iter(s)
    assert next(it)["seq"] == 1 and next(it)["seq"] == 2
    s.close()
    assert s.proc.poll() is not None, "close() ends the call"
    rig.set(follow_wait=False)
    d = ctl_ok(ctl.doctor(), "doctor")
    assert d["healthy"] is True and d["ok"] is True
    rig.set(doctor_ok=False)
    d = ctl_ok(ctl.doctor(), "doctor")                 # exit 1 is an answer for doctor
    assert d["healthy"] is False and d["ok"] is False and d["findings"][0]["code"] == "doctor.modes"
    rig.set(doctor_ok=True)
    if kind == "ssh":
        u = ctl_ok(ctl.unpair(), "unpair")
        assert u["removed"]["key_id"] == rig.key["key_id"], u
        assert not E._is_socket(ctl.control_path), "unpair ends the master"
        assert (rig.state, HOST_ID) not in E._KEYS, "and drops the key file"
    else:
        e = raises(E.CtlRefused, ctl.unpair)                 # no key id on this machine
        assert e.code == "key_unknown", e.to_dict()
    return "every verb answered to its schema"


def test_suite_local(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    out = _suite(rig, rig.local_ctl(), "local")
    assert all(c["original"] is None and c["argv"][:1] != ["--key-id"] for c in rig.calls())
    assert not any(set(c["env"]) & set(E.SSH_ENV) for c in rig.calls()), "no SSH_* reaches it"
    return out


def test_suite_ssh(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    monkeypatch.setenv("DISPLAY", ":0")
    ctl = rig.ssh_ctl()
    try:
        out = _suite(rig, ctl, "ssh")
    finally:
        ctl.close()
    calls = rig.calls()
    assert calls and all(c["argv"] == ["--key-id", rig.key["key_id"]] for c in calls), calls[:2]
    assert all(c["original"] == shlex.join(c["tokens"]) for c in calls)
    for r in rig.ssh_runs():
        assert "SSH_AUTH_SOCK" not in r["env"] and "DISPLAY" not in r["env"], r["env"]
    return out


def test_suite_node(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    ctl = rig.node_ctl()
    out = _suite(rig, ctl, "node")
    e = raises(E.CtlUnreachable, E.NodeControl(FakeAgent(rig.engine, unreachable=True)).status)
    assert e.code == "unreachable"
    e = raises(E.CtlRefused, E.NodeControl(FakeAgent(rig.engine), engine="other").status)
    assert e.code == "not_found", e.to_dict()
    assert rig.calls() == [], "the node adapter never runs the CLI"
    return out


# ---------------------------------------------------------------------------- 4 ssh command line

def test_ssh_command_line(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    ctl = rig.ssh_ctl()
    ctl.status()
    ctl.settings_plan({"MAX_QUEUE": "${HOME}/x"})
    runs = rig.ssh_runs()
    assert len(runs) == 3, runs
    master, first, second = runs
    assert "-M" in master["argv"] and "-N" in master["argv"] and "-f" in master["argv"]
    assert first["mux"] is True and second["mux"] is True, "both calls through the one master"
    assert "-M" not in first["argv"] and "-M" not in second["argv"]
    ident = None
    for r in runs:
        a = r["argv"]
        assert a[:2] == ["-F", "none"] and "-T" in a, a[:4]
        o = [a[i + 1] for i, x in enumerate(a) if x == "-o"]
        for want in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes",
                     "PreferredAuthentications=publickey", "PasswordAuthentication=no",
                     "KbdInteractiveAuthentication=no", "StrictHostKeyChecking=yes",
                     "GlobalKnownHostsFile=/dev/null", f"HostKeyAlias=tandem-engine-{HOST_ID}",
                     f"HostKeyAlgorithms={rig.host_type}", "UpdateHostKeys=no", "ForwardAgent=no",
                     "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no",
                     "RequestTTY=no", f"ControlPath={ctl.control_path}"):
            assert want in o, (want, o)
        assert f"UserKnownHostsFile={E.known_hosts_path(rig.state, HOST_ID)}" in o
        assert a[a.index("-p") + 1] == "22" and a[a.index("-l") + 1] == "tandem"
        ident = a[a.index("-i") + 1]
        i = a.index("--")
        assert a[i + 1] == "engine-a.example"
        if r is master:
            assert a[i + 2:] == [] and "ControlMaster=yes" in o and \
                f"ControlPersist={E.CONTROL_PERSIST_S}" in o
        else:
            assert len(a) == i + 3 and "ControlMaster=no" in o, a[i:]
    assert first["remote"] == "status"
    assert second["remote"] == shlex.join(["settings", "plan", "MAX_QUEUE=${HOME}/x"])
    assert rig.calls()[-1]["tokens"] == ["settings", "plan", "MAX_QUEUE=${HOME}/x"]
    # the key file: this process's, 0600 in 0700 directories; never the key in an argv
    assert ident == os.path.join(rig.state, "ssh", "run", str(os.getpid()), f"{HOST_ID}.key")
    assert os.stat(ident).st_mode & 0o777 == 0o600
    for d in (os.path.dirname(ident), os.path.join(rig.state, "ssh"),
              os.path.dirname(ctl.control_path)):
        assert os.stat(d).st_mode & 0o777 == 0o700, d
    pem = rig.pem()
    for r in runs:
        blob = json.dumps(r)
        assert "PRIVATE KEY" not in blob and all(
            ln not in blob for ln in pem.splitlines() if len(ln) > 20)
    with open(E.known_hosts_path(rig.state, HOST_ID)) as f:
        assert f.read() == f"tandem-engine-{HOST_ID} {rig.host_type} {rig.host_b64}\n"
    ctl.close()
    assert not os.path.exists(ident) and not E._is_socket(ctl.control_path)
    return "-F none, 20 options, one master for two calls, key 0600, command one argument"


def test_real_openssh_takes_every_option(tmp_path):
    """The system's own ssh reads the master's and a call's command line as meant (`ssh -G`
    prints the resulting configuration and connects to nothing)."""
    import shutil
    real = shutil.which("ssh", path="/usr/bin:/bin:/usr/local/bin")
    if real is None:
        from scriptmode import Skip
        raise Skip("no OpenSSH client here")
    d = str(tmp_path)
    t, b = host_pair()
    fp = E.pin_host_key(d, HOST_ID, t, b)
    h = E.SshHost(id=HOST_ID, host="engine-a.example", user="tandem", port=2222,
                  key_id="tandemapp-0123456789", host_key_type=t, host_key=fp)
    ctl = E.SshControl(h, d, key=lambda: "", ssh=real)
    kh = E.known_hosts_path(d, HOST_ID)
    want = ("batchmode yes", "stricthostkeychecking true", "identitiesonly yes",
            "identityagent none", "updatehostkeys false", "forwardagent no", "forwardx11 no",
            "clearallforwardings yes", f"hostkeyalias tandem-engine-{HOST_ID}",
            f"hostkeyalgorithms {t}", "globalknownhostsfile /dev/null", f"userknownhostsfile {kh}",
            "passwordauthentication no", "kbdinteractiveauthentication no",
            "permitlocalcommand no", "requesttty false", "port 2222", "user tandem",
            "hostname engine-a.example", "checkhostip no", f"controlpath {ctl.control_path}")
    for argv, cm, persist in ((ctl.master_argv(kh, "/nonexistent/key"), "true", "300"),
                              (ctl.call_argv(E.build("status"), kh, "/nonexistent/key"), "false",
                               "no")):
        r = subprocess.run([argv[0], "-G", *argv[1:]], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        lines = set(r.stdout.lower().splitlines())
        missing = [w for w in want if w.lower() not in lines]
        assert not missing, missing
        assert f"controlmaster {cm}" in lines and f"controlpersist {persist}" in lines
    version = subprocess.run([real, "-V"], capture_output=True, text=True).stderr.strip()
    return f"{version.split(',')[0]} takes all {len(want)} settings"


# ---------------------------------------------------------------------------- 5 host keys

def test_host_key_pinning(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    nt, nb = host_pair()
    new_fp = E.sshonce.fingerprint(nb)
    fake_ssh.server(rig.server, host_key=[nt, nb])
    # the offered fingerprint from ssh's own words (no keyscan to ask)
    blind = rig.ssh_ctl(keyscan=os.path.join(str(tmp_path), "no-keyscan"))
    e = raises(E.HostKeyChanged, blind.status)
    assert e.pinned == rig.fp and e.offered == new_fp, e.to_dict()
    assert rig.fp in e.message and new_fp in e.message and "nothing ran" in e.message
    assert e.to_dict()["pinned"] == rig.fp and e.to_dict()["offered"] == new_fp
    # ssh without the fingerprint in its words: keyscan names it, else it is unknown
    fake_ssh.server(rig.server, print_fp=False)
    e = raises(E.HostKeyChanged, blind.status)
    assert e.offered is None and "offered unknown" in e.message, e.to_dict()
    ctl = rig.ssh_ctl()
    e = raises(E.HostKeyChanged, ctl.status)
    assert e.offered == new_fp, e.to_dict()
    assert any(r["tool"] == "ssh" for r in rig.ssh_runs())
    # another key type than the pinned one
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pub = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode().split()
    fake_ssh.server(rig.server, host_key=pub[:2], print_fp=True)
    e = raises(E.HostKeyChanged, ctl.status)
    assert e.offered == E.sshonce.fingerprint(pub[1]), e.to_dict()
    assert rig.calls() == [], "nothing ran on the engine"
    # the disk's pin is not the fleet's: refused before ssh runs
    fake_ssh.server(rig.server, host_key=[rig.host_type, rig.host_b64])
    n = len(rig.ssh_runs())
    other = rig.ssh_ctl(host=rig.host(host_key=new_fp))
    e = raises(E.HostKeyChanged, other.status)
    assert e.pinned == new_fp and e.offered == rig.fp, e.to_dict()
    assert len(rig.ssh_runs()) == n
    # no pin at all
    nopin = rig.ssh_ctl(host=rig.host(id="eh-test-2"))
    e = raises(E.CtlNotPaired, nopin.status)
    assert e.code == "not_pinned", e.to_dict()
    # a pin is replaced only by a new pairing
    e = raises(E.HostKeyChanged, E.pin_host_key, rig.state, HOST_ID, nt, nb)
    assert e.pinned == rig.fp and e.offered == new_fp
    assert E.pinned_key(rig.state, HOST_ID) == (rig.host_type, rig.host_b64)
    assert E.pin_host_key(rig.state, HOST_ID, rig.host_type, rig.host_b64) == rig.fp   # the same
    raises(E.CtlBadCall, E.pin_host_key, rig.state, HOST_ID, "ssh-dss", rig.host_b64)
    raises(E.CtlBadCall, E.pin_host_key, rig.state, HOST_ID, rig.host_type, "not base64!")
    assert len(rig.ssh_runs()) == n
    # the right key again: it works
    ctl_ok(ctl.status(), "status")
    assert E.pin_host_key(rig.state, HOST_ID, nt, nb, replace=True) == new_fp
    ctl.close()
    return "changed (both fingerprints), other type, disk not the fleet's, no pin, no replace"


# ---------------------------------------------------------------------------- 6 errors

def test_error_mapping(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch)
    doc = {"ctl": E.CTL, "version": "1.0", "verb": "status"}

    def script(stdout, rc, sleep=0, verb="status"):
        rig.set(script={verb: {"exit": rc, "stdout": stdout, "sleep": sleep}})

    for name, mk in (("local", rig.local_ctl), ("ssh", rig.ssh_ctl)):
        ctl = mk()
        cases = [
            (json.dumps({**doc, "ok": True}) + "\n", 7, E.CtlProtocol, "exited 7"),
            (json.dumps({**doc, "ok": False}) + "\n", 0, E.CtlProtocol, "not ok"),
            (json.dumps({**doc, "ok": False, "error": {"code": "bad_value", "message": "x"}}), 4,
             E.CtlProtocol, "disagree"),
            ("not json\n", 0, E.CtlProtocol, "not JSON"),
            (json.dumps({**doc, "ok": True}) + "\n" + json.dumps({**doc, "ok": True}) + "\n", 0,
             E.CtlProtocol, "2 answer lines"),
            ("", 0, E.CtlProtocol, "0 answer lines"),
            (json.dumps({**doc, "verb": "start", "ok": True}), 0, E.CtlProtocol, "for 'start'"),
            (json.dumps({**doc, "ctl": "other", "ok": True}), 0, E.CtlProtocol, "not an"),
            (json.dumps({**doc, "ok": False, "error": {"code": "later_code", "message": "m"}}), 3,
             E.CtlRefused, "later_code"),
            (json.dumps({**doc, "ok": False, "error": {"code": "usage_x"}}), 2, E.CtlProtocol,
             "without an error"),
            (json.dumps({**doc, "ok": False}), 1, E.CtlNotOk, "not ok"),
            (json.dumps({**doc, "ok": False, "error": {"code": "internal", "message": "boom"}}), 5,
             E.CtlInternal, "boom"),
            (json.dumps({**doc, "ok": False, "error": {"code": "unknown_option", "message": "u"}}),
             2, E.CtlUsage, "unknown_option"),
        ]
        for out, rc, cls, words in cases:
            script(out, rc)
            e = raises(cls, ctl.status)
            assert words in e.message, (name, rc, words, e.message)
            assert e.verb == "status" and e.host is not None, e.to_dict()
        # another major; status alone carries the engine's release in `version` (its schema)
        rig.set(script={"mode get": {"exit": 0, "stdout": json.dumps(
            {**doc, "verb": "mode get", "version": "2.0", "ok": True})}})
        e = raises(E.CtlProtocol, ctl.mode_get)
        assert "speaks" in e.message and "'2.0'" in e.message, e.message
        script(json.dumps({**doc, "version": "0.15.4", "ok": True, "engine": {}}), 0)
        assert ctl.status()["version"] == "0.15.4"
        script(json.dumps({**doc, "version": "0.15.4", "ok": False,
                           "error": {"code": "internal", "message": "m"}}), 5)
        e = raises(E.CtlProtocol, ctl.status)
        assert "speaks" in e.message, e.message
        # a timeout ends the call and its processes
        script(json.dumps({**doc, "ok": True}), 0, sleep=30)
        slow = mk(timeouts={"status": 1.5})
        t0 = time.monotonic()
        e = raises(E.CtlTimeout, slow.status)
        assert time.monotonic() - t0 < 8, "the timeout held"
        assert e.code == "timeout"
        pid = rig.calls()[-1]["pid"]
        end = time.monotonic() + 5
        while time.monotonic() < end and _alive(pid):
            time.sleep(0.05)
        assert not _alive(pid), "the slow call was killed"
        rig.set(script={})
        ctl.close()
        slow.close()
    # ssh's own failures
    ctl = rig.ssh_ctl()
    fake_ssh.server(rig.server, unreachable=True)
    e = raises(E.CtlUnreachable, ctl.status)
    assert e.code == "unreachable" and e.exit == 255 and "engine-a.example:22" in e.message
    fake_ssh.server(rig.server, unreachable=False)
    with open(rig.ak, "w") as f:
        f.write("")
    e = raises(E.CtlAuthRefused, ctl.status)
    assert e.code == "auth_refused" and "pair it again" in e.message
    with open(rig.ak, "w") as f:
        f.write(rig.keys.authorized_line(HOST_ID, rig.engine) + "\n")
    # a master that is gone: the call connects directly, the next starts a new master
    ctl_ok(ctl.status(), "status")
    assert E._is_socket(ctl.control_path)
    fake_ssh.server(rig.server, dead_master=True)
    ctl_ok(ctl.status(), "status")
    assert not os.path.lexists(ctl.control_path), "the dead master's socket is gone"
    fake_ssh.server(rig.server, dead_master=False)
    n = len(rig.ssh_runs())
    ctl_ok(ctl.status(), "status")
    assert "-M" in rig.ssh_runs()[n]["argv"], "a new master"
    # the key cannot be read, or there is none
    gone = rig.ssh_ctl(host=rig.host())
    gone._key = lambda: (_ for _ in ()).throw(vault_mod.SecretError("tag mismatch"))
    gone.close()
    e = raises(E.CtlNotPaired, gone.status)
    assert e.code == "no_key"
    rig.keys.delete(HOST_ID)
    E.drop_key_file(rig.state, HOST_ID)
    e = raises(E.CtlNotPaired, rig.ssh_ctl().status)
    assert e.code == "no_key"
    ctl.close()
    # the node adapter's agent errors
    class Odd(FakeAgent):
        def json(self, method, path, body=None, timeout=None):
            if self.mode == "slow":
                raise TimeoutError("timed out")
            return {"down": (503, {"error": {"type": "engine_down", "message": "down"}}),
                    "broken": (500, None)}[self.mode]
    for mode, cls in (("down", E.CtlUnreachable), ("broken", E.CtlInternal),
                      ("slow", E.CtlTimeout)):
        a = Odd(rig.engine)
        a.mode = mode
        raises(cls, E.NodeControl(a).status)
    return "protocol x 12, refused, not ok, internal, usage, timeout killed, unreachable, " \
           "key refused, dead master, no key"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


# ---------------------------------------------------------------------------- 7 sockets, key files

def test_sockets_and_key_files(tmp_path, monkeypatch):
    rig = Rig(str(tmp_path), monkeypatch, short=True)
    try:
        ctl = rig.ssh_ctl()
        ctl.status()
        assert os.path.dirname(ctl.control_path) == os.path.join(rig.state, "ssh", "cm")
        assert len(ctl.control_path) + 17 < 104
        ctl.close()
        # a long state path: this user's own directory under tmp
        tmpd = tempfile.mkdtemp(prefix="ectl-t-", dir="/tmp")
        monkeypatch.setattr(tempfile, "tempdir", tmpd)
        deep = os.path.join(str(tmp_path), "x" * 60, "controller")
        os.makedirs(deep)
        E.pin_host_key(deep, HOST_ID, rig.host_type, rig.host_b64)
        long_ctl = E.SshControl(rig.host(), deep, key=rig.pem, ssh=os.path.join(rig.bin, "ssh"))
        ctl_ok(long_ctl.status(), "status")
        cm = os.path.join(tmpd, f"tandem-cm-{os.getuid()}")
        assert os.path.dirname(long_ctl.control_path) == cm
        st = os.stat(cm)
        assert st.st_mode & 0o777 == 0o700 and st.st_uid == os.getuid()
        long_ctl.close()
        # a link where that directory goes is refused
        os.rename(cm, cm + ".old")
        os.symlink(cm + ".old", cm)
        e = raises(E.CtlBadCall, E.SshControl(rig.host(), deep, key=rig.pem).status)
        assert e.code == "unsafe_dir"
        os.remove(cm)
        # key files: this process's, removed by close(); a gone process's removed at the next use
        run = os.path.join(rig.state, "ssh", "run")
        dead = 999_999
        while _alive(dead):
            dead -= 1
        os.makedirs(os.path.join(run, str(dead)), mode=0o700)
        with open(os.path.join(run, str(dead), f"{HOST_ID}.key"), "w") as f:
            f.write("left over\n")
        live = os.getppid()
        os.makedirs(os.path.join(run, str(live)), mode=0o700, exist_ok=True)
        ctl = rig.ssh_ctl()
        ctl.status()
        kf = os.path.join(run, str(os.getpid()), f"{HOST_ID}.key")
        assert os.path.isfile(kf) and os.stat(kf).st_mode & 0o777 == 0o600
        assert not os.path.exists(os.path.join(run, str(dead))), "a gone process's files went"
        assert os.path.isdir(os.path.join(run, str(live))), "a live process's stay"
        ctl.close()
        assert not os.path.exists(kf)
        E._drop_all()
        assert not os.path.exists(os.path.join(run, str(os.getpid())))
    finally:
        import shutil
        shutil.rmtree(rig.state, ignore_errors=True)
        if "tmpd" in locals():
            shutil.rmtree(tmpd, ignore_errors=True)
    return "socket dir short and long, link refused, key file lifetime, left-overs swept"


# ---------------------------------------------------------------------------- 8 key pairs

def test_host_keys(tmp_path):
    from cryptography.hazmat.primitives import serialization
    cfg = config_mod.ConfigDB(os.path.join(str(tmp_path), "config.sqlite3"), env={},
                              use_keychain=False).open()
    keys = hostkeys.HostKeys(cfg)
    assert keys.get("eh-a") is None
    k = keys.create("eh-a")
    assert k["public"].startswith("ssh-ed25519 AAAA") and k["public"].endswith(k["key_id"])
    assert len(k["key_id"]) == 20 and k["key_id"].startswith("tandemapp-")
    assert E.KEY_ID_RE.match(k["key_id"]) and E.FINGERPRINT_RE.fullmatch(k["fingerprint"])
    assert keys.create("eh-a") == k, "one pair per host"
    pem = keys.private("eh-a")
    priv = serialization.load_ssh_private_key(pem.encode(), password=None)
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                         serialization.PublicFormat.OpenSSH).decode()
    assert k["public"].split()[:2] == pub.split()[:2]
    k2 = keys.create("eh-a", replace=True)
    assert k2["key_id"] != k["key_id"] and keys.private("eh-a") != pem
    # sealed: another host's row does not open with it
    keys.create("eh-b")
    c = cfg._conn
    row = c.execute("SELECT ciphertext, nonce, master_key_id FROM engine_host_keys WHERE "
                    "host_id = 'eh-a'").fetchone()
    c.execute("UPDATE engine_host_keys SET ciphertext = ?, nonce = ?, master_key_id = ? WHERE "
              "host_id = 'eh-b'", row)
    c.commit()
    raises(vault_mod.SecretError, keys.private, "eh-b")
    # the line the engine's contract writes
    with open(os.path.join(CTL_DIR, "verbs.json")) as f:
        tpl = json.load(f)["key_line"]
    k = keys.get("eh-a")
    line = keys.authorized_line("eh-a", "/home/u/TandemLLM")
    want = tpl.replace("DIR", "/home/u/TandemLLM").replace("ID", k["key_id"], 1) \
        .replace('[,from="..."] ', " ").replace("ssh-ed25519 AAAA... COMMENT", k["public"])
    assert line == want, (line, want)
    line = keys.authorized_line("eh-a", "/opt/t/", from_=["192.0.2.0/24", "*.lan"])
    assert line.startswith('command="/bin/bash /opt/t/bin/tandem remote --key-id ') and \
        ',no-X11-forwarding,from="192.0.2.0/24,*.lan" ssh-ed25519 ' in line, line
    for bad in ("rel/path", "/a b", '/a"b', "/a/../b", "/a,b", "/a$b", "/a\\b", "/a\nb"):
        raises(hostkeys.HostKeyError, keys.authorized_line, "eh-a", bad)
    for bad in ('a"b', "a b", ["ok", 'x"']):
        raises(hostkeys.HostKeyError, keys.authorized_line, "eh-a", "/opt/t", bad)
    raises(hostkeys.HostKeyError, keys.authorized_line, "eh-zzz", "/opt/t")
    raises(hostkeys.HostKeyError, keys.create, "bad id/../x")
    assert keys.delete("eh-a") and keys.get("eh-a") is None and not keys.delete("eh-a")
    e = raises(E.CtlNotPaired, keys.private, "eh-a")
    assert e.code == "no_key"
    audit = c.execute("SELECT action, object, object_id, fields, detail FROM audit WHERE object = "
                      "'engine_host_key'").fetchall()
    assert [a[0] for a in audit] == ["create", "create", "create", "delete"], audit
    cfg.close()
    return "ed25519, key id, sealed per host, the contract's line, refusals, audit"


# ---------------------------------------------------------------------------- 9 the fleet

def test_fleet_ssh_node(tmp_path, monkeypatch):
    import test_fleet as tf
    from proxy import ctlmove
    from proxy import fleet as fleet_mod
    from proxy_util import Proxy
    with Proxy() as px:
        f = tf.fleet_on(px, tf.local_node(tf.fake_root()))
        cfg = f.cfg
        root = f.trust().root
        rig = Rig(str(tmp_path), monkeypatch)
        keys = hostkeys.HostKeys(cfg)
        monkeypatch.setenv("PATH", rig.bin + os.pathsep + os.environ["PATH"])

        def add(hid: str, alias: str) -> dict:
            k = keys.create(hid)
            fp = E.pin_host_key(root, hid, rig.host_type, rig.host_b64)
            with open(rig.ak, "a") as fh:
                fh.write(keys.authorized_line(hid, rig.engine) + "\n")
            t = E.ssh_trust(host="engine-a.example", user="tandem", key_id=k["key_id"],
                            host_key_type=rig.host_type, host_key=fp)
            return cfg.add_node({"id": hid, "alias": alias, "transport": "ssh",
                                 "endpoint": "ssh://tandem@engine-a.example:22",
                                 "address": "engine-a.example", "trust": t})

        row = add("eh-1", "engine-one")
        node = json.loads(px.request("GET", "/v1/fleet/nodes/eh-1")[2])
        tf.ok(node, "node")
        assert node["transport"] == "ssh" and node["status"] == "off", node
        assert "SSH" in node["status_text"], node["status_text"]
        p = node["pairing"]
        assert p["state"] == "paired" and p["via"] == "ssh" and p["fingerprint"] is None
        assert p["host_key"] == row["trust"]["host_key"]
        nodes = json.loads(px.request("GET", "/v1/fleet/nodes")[2])
        tf.ok(nodes, "nodes")
        sample = json.loads(px.request("GET", "/v1/fleet/nodes/eh-1/sample")[2])
        assert sample["via"] == "none", sample
        assert fleet_mod.pairing_view(row)["state"] == "paired"
        # for_node: the adapter each record names
        ctl = E.for_node(row, state_dir=root, keys=keys, log=px.logs.append)
        assert isinstance(ctl, E.SshControl) and ctl.host.key_id == row["trust"]["key_id"]
        ctl_ok(ctl.status(), "status")
        ctl.close()
        assert isinstance(E.for_node({"id": "n", "endpoint": "local", "trust": {}}, state_dir=root,
                                     engine_dir=rig.engine), E.LocalControl)
        assert isinstance(E.for_node({"id": "n", "endpoint": "https://x", "trust": {"kind": "mtls"}},
                                     state_dir=root, agent=FakeAgent(rig.engine)), E.NodeControl)
        raises(E.CtlNotPaired, E.for_node, {"id": "n", "endpoint": "https://x", "trust": {}},
               state_dir=root)
        raises(E.CtlNotPaired, E.for_node, row, state_dir=root)          # no key store
        bad = dict(row, trust={**row["trust"], "host": "-oProxyCommand=x"})
        raises(E.CtlBadCall, E.for_node, bad, state_dir=root, keys=keys)
        raises(E.CtlBadCall, E.ssh_trust, host="h", user="u", key_id="k", host_key_type="ssh-ed25519",
               host_key=row["trust"]["host_key"], via="paste")
        # a move's export carries no key pair; the plan keeps the ssh node here
        bundle = ctlmove.export(cfg)
        db = base64.b64decode(bundle["db"])
        path = os.path.join(str(tmp_path), "export.sqlite3")
        with open(path, "wb") as fh:
            fh.write(db)
        c = sqlite3.connect(path)
        assert c.execute("SELECT COUNT(*) FROM engine_host_keys").fetchone()[0] == 0
        c.close()
        assert keys.private("eh-1").splitlines()[1].encode() not in db
        # release: the host removes this key's line, then the pair and the pin go here
        n = len(rig.calls())
        st, _, out = px.request("DELETE", "/v1/fleet/nodes/eh-1")
        assert st == 200, out
        assert json.loads(out)["node"] == "unpinned"
        assert [c["tokens"] for c in rig.calls()[n:]] == [["unpair"]]
        assert keys.get("eh-1") is None and E.pinned_key(root, "eh-1") is None
        assert cfg.node("eh-1") is None
        # one that does not answer: refused, then forgotten with force
        add("eh-2", "engine-two")
        fake_ssh.server(rig.server, unreachable=True)
        st, _, out = px.request("DELETE", "/v1/fleet/nodes/eh-2")
        assert st == 502 and json.loads(out)["error"]["type"] == "unreachable", out
        assert keys.get("eh-2") is not None
        st, _, out = px.request("DELETE", "/v1/fleet/nodes/eh-2?force=1")
        assert st == 200, out
        assert keys.get("eh-2") is None and E.pinned_key(root, "eh-2") is None
        # one whose host key changed: refused with both fingerprints, then forgotten with force
        fake_ssh.server(rig.server, unreachable=False)
        add("eh-3", "engine-three")
        nt, nb = host_pair()
        fake_ssh.server(rig.server, host_key=[nt, nb])
        st, _, out = px.request("DELETE", "/v1/fleet/nodes/eh-3")
        err = json.loads(out)["error"]
        assert st == 409 and err["type"] == "host_key_changed", out
        assert E.sshonce.fingerprint(nb) in err["message"] and rig.fp in err["message"], err
        st, _, out = px.request("DELETE", "/v1/fleet/nodes/eh-3?force=1")
        assert st == 200 and keys.get("eh-3") is None, out
        E._drop_all()
    return "node and nodes validate, paired via ssh, no agent, for_node, release and force, export"


# ---------------------------------------------------------------------------- 10 no secret

def test_no_secret_anywhere(tmp_path, monkeypatch):
    """The private key is in nothing: no log line, error, argv, answer, record or database byte.
    A secret-looking settings value goes to the engine (it must: the answer and the command carry
    it) but never into a log line or an error's text."""
    rig = Rig(str(tmp_path), monkeypatch)
    pem = rig.pem()
    errors: list[str] = []            # what a page or a log would show of a failure
    other: list[str] = []             # answers, argv, records, audit rows
    ssh = rig.ssh_ctl()
    for ctl in (rig.local_ctl(), ssh):
        other.append(json.dumps(ctl.settings_plan({"HF_TOKEN": SECRET, "MAX_QUEUE": 4})))
        # an engine that echoes the value back in a refusal
        rig.set(script={"settings apply": {"exit": 3, "stdout": json.dumps({
            "ctl": E.CTL, "version": "1.0", "verb": "settings apply", "ok": False,
            "error": {"code": "not_in_overlay", "message": f"HF_TOKEN={SECRET} is not managed"}})}})
        e = raises(E.CtlRefused, ctl.settings_apply, {"HF_TOKEN": SECRET},
                   confirm="0123456789abcdef")
        assert "HF_TOKEN=*** is not managed" in e.message, e.message
        errors += [str(e), e.message, json.dumps(e.to_dict())]
        rig.set(script={})
        fake_ssh.server(rig.server, unreachable=True)
        for fn in (ctl.status, lambda: ctl.settings_plan({"HF_TOKEN": SECRET})):
            try:
                other.append(json.dumps(fn()))
            except E.CtlError as exc:
                errors += [str(exc), json.dumps(exc.to_dict())]
        fake_ssh.server(rig.server, unreachable=False)
    # the host refuses the key, the host key changed: ssh's words, scrubbed
    with open(rig.ak, "w") as f:
        f.write("")
    ssh.close()
    errors.append(str(raises(E.CtlAuthRefused, ssh.status)))
    with open(rig.ak, "w") as f:
        f.write(rig.keys.authorized_line(HOST_ID, rig.engine) + "\n")
    with ssh.follow_logs() as st:
        other += [json.dumps(d) for d in st]
    other.append(json.dumps(ssh.status()))
    assert any("HF_TOKEN" in ln for ln in rig.logs), "the log names the key"
    errors += rig.logs
    other += [json.dumps(r) for r in rig.ssh_runs()]
    other += [json.dumps(c) for c in rig.calls()]
    other.append(json.dumps(E.ssh_trust(host="h", user="u", key_id=rig.key["key_id"],
                                        host_key_type=rig.host_type, host_key=rig.fp)))
    other.append(json.dumps(rig.keys.get(HOST_ID)))
    for r in rig.cfg._conn.execute("SELECT * FROM audit").fetchall():
        other.append(repr(r))
    ssh.close()
    rig.cfg.close()
    raw = b""
    for name in os.listdir(os.path.dirname(rig.cfg.path)):
        with open(os.path.join(os.path.dirname(rig.cfg.path), name), "rb") as fh:
            raw += fh.read()
    pieces = [ln for ln in pem.splitlines() if len(ln) >= 16 and "PRIVATE KEY" not in ln]
    assert pieces
    for t in errors:
        assert SECRET not in t, t[:300]
    for t in errors + other:
        assert "PRIVATE KEY" not in t and not any(p in t for p in pieces), t[:300]
    assert not any(p.encode() in raw for p in pieces) and b"PRIVATE KEY" not in raw
    # the ledger of a proxy (enginectl writes no row): no key there either
    from proxy_util import Proxy
    with Proxy() as px:
        rows = json.dumps(px.rows(), default=str)
    assert SECRET not in rows and not any(p in rows for p in pieces)
    return f"{len(errors)} log lines and errors, {len(other)} answers and records, the " \
           f"database's bytes: no key; no secret value in a log or an error"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
