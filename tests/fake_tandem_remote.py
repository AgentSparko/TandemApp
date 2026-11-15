"""A fake `tandem remote` (contracts/engine-ctl-v1) for tests: the engine's control CLI without an
engine. Standard library only; it imports nothing of TandemApp or the engine.

    python fake_tandem_remote.py DIR [--key-id ID] [VERB ...]

DIR is a made-up install (`install(dir)` writes it): `DIR/bin/tandem` runs this file for
`remote`, as the engine's own bin/tandem does. As the engine's CLI does, the command is
SSH_ORIGINAL_COMMAND when set (then the arguments may be --key-id only), else the arguments; it is
split with shlex, never by a shell; one JSON document goes to stdout (JSON lines for
`logs --follow`) with the contract's exit status.

DIR/fake-state.json steers it (`state(dir)`, `set_state(dir, **kw)`):

    engine        healthy | down               what status says, start and stop change it
    busy          true: a lifecycle verb answers busy (exit 4) with the acting job
    doctor_ok     false: doctor answers its findings with exit 1
    confirm       the token plan gives and apply and restore take
    follow_lines  how many lines `logs --follow` writes; follow_wait: then it waits to be ended
    script        {verb: {exit, stdout, sleep}}: that verb answers exactly this (a wrong exit, a
                  broken answer, a slow one)
    install_fails true: ext install reads and checks the wheel, then answers install_failed (exit 1)
    token         the HTTP token `token rotate` answers (contract 1.1)
    mode          the mode `mode get` and `status` say (engine); `mode set` writes it
    inferred      true: `mode get` says the mode was read from an empty QSE_MODE
    model         the model `status` says a healthy engine serves ("fake-model"; null: none yet)

The 1.1 verbs that read stdin (ext install, upgrade plan --sha256) check the bytes' sha256 against
--sha256 as the engine does (hash_mismatch, exit 3) and record {sha256, bytes} in DIR/stdin.jsonl,
never the bytes.

DIR/calls.jsonl gets one line per call: {argv, original (SSH_ORIGINAL_COMMAND), tokens, env (the
SSH_* names that were set), pid}.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time

CTL, VERSION = "engine-ctl-v1", "1.1"
VERBS = {"status": None, "start": None, "stop": None, "restart": None, "mode": ("get", "set"),
         "settings": ("get", "plan", "apply", "versions", "restore"), "logs": None,
         "doctor": None, "unpair": None, "ext": ("list", "install", "remove"),
         "upgrade": ("plan", "apply"), "token": ("rotate",)}
TOKEN = "fake-http-token-" + "0123456789abcdef" * 3
CREATED = "2026-11-15T10:00:00Z"
JOB_ID = "20261115T100000-abcdef"


def install(d: str, python: str | None = None) -> str:
    """A made-up engine install at `d` whose bin/tandem runs this fake; `d`."""
    os.makedirs(os.path.join(d, "bin"), exist_ok=True)
    tandem = os.path.join(d, "bin", "tandem")
    with open(tandem, "w") as f:
        f.write("#!/bin/bash\n"
                'if [ "$1" != remote ]; then echo "fake: remote only" >&2; exit 64; fi\n'
                "shift\n"
                f"exec {shlex.quote(python or sys.executable)} "
                f"{shlex.quote(os.path.abspath(__file__))} {shlex.quote(d)} \"$@\"\n")
    os.chmod(tandem, 0o755)
    set_state(d, engine="healthy", busy=False, doctor_ok=True, confirm="0123456789abcdef",
              follow_lines=3, follow_wait=False, script={}, install_fails=False, token=TOKEN)
    return d


def state(d: str) -> dict:
    try:
        with open(os.path.join(d, "fake-state.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def set_state(d: str, **kw) -> dict:
    s = state(d)
    s.update(kw)
    with open(os.path.join(d, "fake-state.json"), "w") as f:
        json.dump(s, f)
    return s


def stdin_seen(d: str) -> list[dict]:
    try:
        with open(os.path.join(d, "stdin.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


def _read_stdin(d: str, want: str, limit: int) -> dict:
    import hashlib
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise Refuse("too_large", f"more than {limit >> 20} MiB on stdin", 3)
    if not data:
        raise Refuse("missing_argument", "nothing on stdin: the file goes there", 2)
    got = hashlib.sha256(data).hexdigest()
    with open(os.path.join(d, "stdin.jsonl"), "a") as f:
        f.write(json.dumps({"sha256": got, "bytes": len(data)}) + "\n")
    if got != want:
        raise Refuse("hash_mismatch", f"the {len(data)} bytes on stdin have sha256 {got}, not "
                                      f"{want}: nothing was written", 3,
                     checked={"sha256": got, "bytes": len(data)})
    return {"sha256": got, "bytes": len(data)}


def calls(d: str) -> list[dict]:
    try:
        with open(os.path.join(d, "calls.jsonl")) as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------------------------------ answers
class Refuse(Exception):
    def __init__(self, code, message, rc, **extra):
        super().__init__(message)
        self.code, self.rc, self.extra = code, rc, extra


def emit(doc: dict) -> None:
    sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
    sys.stdout.flush()


def job(kind: str, when: str | None, state_: str = "waiting") -> dict:
    w = {"mode": "now"}
    if when == "idle":
        w = {"mode": "idle", "deadline_s": 600}
    elif when and when.startswith("at="):
        w = {"mode": "at", "at": when[3:], "deadline_s": 600}
    return {"id": JOB_ID, "kind": kind, "state": state_, "when": w, "created_at": CREATED,
            "ended_at": None, "message": None}


def opts_of(rest: list[str]) -> tuple[dict, list]:
    opts, pos, i = {}, [], 0
    while i < len(rest):
        t = rest[i]
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if name in ("--follow",):
                opts[name] = True
            elif name == "--unset":
                i += 1
                opts.setdefault("--unset", []).append(rest[i] if i < len(rest) else None)
            else:
                if not eq:
                    i += 1
                    if i >= len(rest):
                        raise Refuse("missing_argument", f"{name} needs a value", 2)
                    val = rest[i]
                opts[name] = val
        else:
            pos.append(t)
        i += 1
    return opts, pos


def answer(d: str, st: dict, name: str, opts: dict, pos: list, key_id: str | None) -> tuple[dict, int]:
    lifecycle = ("start", "stop", "restart", "mode set", "settings apply", "settings restore")
    if name in lifecycle and st.get("busy"):
        raise Refuse("busy", "another lifecycle job is acting; tandem remote status shows it", 4,
                     job=job("restart", "now", "acting"))
    if name in lifecycle + ("upgrade apply",) and st.get("busy"):
        raise Refuse("busy", "another lifecycle job is acting; tandem remote status shows it", 4,
                     job=job("restart", "now", "acting"))
    if name == "status":
        healthy = st.get("engine") == "healthy"
        return {"engine": {"state": st.get("engine", "down"), "pid": 4242 if healthy else None,
                           "port": 8000, "health_code": 200 if healthy else None,
                           "engine_api": "1.9" if healthy else None,
                           "version": "0.15.4" if healthy else None,
                           "model": st.get("model", "fake-model") if healthy else None,
                           "running_mode": "engine" if healthy else None,
                           "stopped_on_purpose": st.get("engine") == "down"},
                "mode": st.get("mode", "engine"), "profile": None, "engine_version": "0.15.4",
                "engine_api": "1.9",
                "http": {"token": bool(st.get("rotated")), "allow_from": None},
                "install": True, "overlay": {"current": 0, "good": 0, "versions": 1, "file": False},
                "hook": True, "job": None}, 0
    if name in ("start", "stop", "restart"):
        set_state(d, engine="down" if name == "stop" else "healthy")
        return {"job": job(name, opts.get("--when")), "follow": "tandem remote status"}, 0
    if name == "mode get":
        return {"mode": st.get("mode", "engine"), "ext": None,
                "source": "inferred" if st.get("inferred") else "managed",
                "inferred": bool(st.get("inferred"))}, 0
    if name == "mode set":
        set_state(d, mode=pos[0] if pos else "engine", inferred=False)
        return {"job": job("mode", opts.get("--when", "idle")), "follow": "tandem remote status"}, 0
    if name == "settings get":
        keys = pos or ["MAX_QUEUE", "PORT"]
        return {"keys": [{"key": k, "set": True, "value": "4", "source": "managed", "type": "integer",
                          "managed": True, "restart": "engine", "fingerprint": False} for k in keys],
                "overlay": {"current": 0, "good": 0, "versions": 1, "file": False}, "hook": True}, 0
    if name in ("settings plan", "settings apply"):
        changes = [p for p in pos if "=" in p]
        plan = {"base": 0, "overlay": [{"key": c.split("=")[0], "from": None, "to": c.split("=", 1)[1]}
                                       for c in changes],
                "effect": [], "shadowed": [], "restart_needed": True, "fingerprint_keys": [],
                "confirm": st["confirm"], "warnings": []}
        if name == "settings plan":
            return {"plan": plan}, 0
        if "--confirm" not in opts:
            raise Refuse("confirm_required", "settings apply needs --confirm with the plan's token", 3)
        if opts["--confirm"] != st["confirm"]:
            raise Refuse("confirm_mismatch", "the confirm token is not this plan's", 3)
        return {"job": job("settings", opts.get("--when", "idle")), "follow": "tandem remote status",
                "plan": plan}, 0
    if name == "settings versions":
        return {"current": 0, "good": 0, "versions": [
            {"n": 0, "at": CREATED, "note": "empty", "set": {}, "sha256": "0" * 64}]}, 0
    if name == "settings restore":
        if opts.get("--confirm") != st["confirm"]:
            raise Refuse("confirm_mismatch", "the confirm token is not this plan's", 3)
        return {"job": job("restore", opts.get("--when", "idle")), "follow": "tandem remote status"}, 0
    if name == "logs":
        n = int(opts.get("--lines", 100))
        which = opts.get("--which", "engine")
        return {"which": which, "file": f"logs/{which}.log",
                "lines": [f"line {i}" for i in range(1, min(n, 5) + 1)]}, 0
    if name == "doctor":
        if st.get("doctor_ok", True):
            return {"healthy": True, "findings": [], "checks": ["config", "modes"]}, 0
        return {"healthy": False, "checks": ["config", "modes"],
                "findings": [{"code": "doctor.modes", "state": "problem", "text": "bin/tandem is "
                              "not executable", "fix": "tandem doctor restores the execute bits"}]}, 1
    if name == "ext list":
        ext = st.get("ext") or {}
        return {"extensions": [{"module": v["module"], "source": "remote", "dist": k,
                                "dist_version": v["version"], "name": k, "version": v["version"],
                                "ext_api": "3.1", "imports": True, "accepted": True, "why": None,
                                "configured": False, "wheel": v["wheel"], "sha256": v["sha256"],
                                "installed_at": CREATED} for k, v in sorted(ext.items())],
                "mode": "engine", "configured": None, "host_ext_api": "3.1"}, 0
    if name == "ext install":
        checked = _read_stdin(d, opts.get("--sha256", ""), 256 << 20)
        wheel = opts.get("--name", "")
        dist, version = wheel.split("-")[0], wheel.split("-")[1]
        if st.get("install_fails"):
            raise Refuse("install_failed", "ops/venv-wheel.sh install exited 1: no space left", 1,
                         checked=checked)
        mod = opts.get("--module") or dist.replace("-", "_")
        ext = dict(st.get("ext") or {})
        ext[dist] = {"module": mod, "version": version, "wheel": wheel, "sha256": checked["sha256"]}
        set_state(d, ext=ext)
        return {"installed": {"dist": dist, "version": version, "module": mod, "wheel": wheel,
                              "sha256": checked["sha256"]},
                "extension": {"name": dist, "version": version, "ext_api": "3.1", "imports": True,
                              "accepted": True, "why": None},
                "configured": False, "restart_needed": True, "checked": checked}, 0
    if name == "ext remove":
        dist = pos[0] if pos else ""
        ext = dict(st.get("ext") or {})
        if dist not in ext:
            raise Refuse("not_installed", f"{dist} is not installed", 3)
        gone = ext.pop(dist)
        set_state(d, ext=ext)
        return {"removed": {"dist": dist, "version": gone["version"], "module": gone["module"],
                            "in_use": False, "registry": True}, "restart_needed": True}, 0
    if name in ("upgrade plan", "upgrade apply"):
        if ("--ref" in opts) == ("--sha256" in opts):
            raise Refuse("missing_argument", f"{name} takes --ref REF or --sha256 HEX", 2)
        checked = None
        if name == "upgrade plan" and "--sha256" in opts:
            checked = _read_stdin(d, opts["--sha256"], 512 << 20)
        to = {"source": "ref", "ref": opts["--ref"]} if "--ref" in opts else \
            {"source": "archive", "sha256": opts["--sha256"]}
        plan = {"from": {"commit": "a" * 40, "version": "0.15.4", "engine_api": "1.9",
                         "ctl": "1.1", "lock": None, "venv_lock": None},
                "to": {**to, "commit": "b" * 40, "version": "0.15.5", "engine_api": "1.9",
                       "ctl": "1.1", "lock": None},
                "venv": "kept", "keeps": ["run/local.env"], "steps": ["fetch", "install", "start"],
                "rollback": "the previous tree", "warnings": [], "confirm": st["confirm"]}
        if name == "upgrade plan":
            return {"plan": plan, **({"checked": checked} if checked else {})}, 0
        if opts.get("--confirm") != st["confirm"]:
            raise Refuse("confirm_mismatch", "the confirm token is not this plan's", 3)
        j = job("upgrade", opts.get("--when", "idle"))
        j["upgrade"] = {"source": to["source"], "from_commit": "a" * 40, "venv": "kept"}
        return {"job": j, "follow": "tandem remote status", "upgrade": plan}, 0
    if name == "token rotate":
        set_state(d, rotated=True)
        return {"token": st.get("token") or TOKEN, "rotated": True, "file": "run/http-token",
                "effective": "within a second"}, 0
    if name == "unpair":
        if not key_id:
            raise Refuse("key_unknown", "this call's key is not known: nothing was removed", 3)
        return {"removed": {"type": "ssh-ed25519", "comment": key_id, "key_id": key_id}, "left": 0}, 0
    raise Refuse("unknown_verb", f"unknown verb {name!r}", 2)


def follow(st: dict) -> int:
    stop = []

    def _end(*_):
        stop.append(1)
        raise SystemExit(0)
    signal.signal(signal.SIGHUP, _end)
    signal.signal(signal.SIGTERM, _end)
    for i in range(1, int(st.get("follow_lines", 3)) + 1):
        emit({"ctl": CTL, "verb": "logs", "which": "engine", "seq": i, "line": f"line {i}"})
    while st.get("follow_wait") and not stop:
        time.sleep(0.05)
    return 0


def main(argv: list[str]) -> int:
    d, args = argv[0], argv[1:]
    env = os.environ
    st = state(d)
    key_id = None
    if args[:1] == ["--key-id"]:
        key_id, args = (args[1] if len(args) > 1 else None), args[2:]
    original = env.get("SSH_ORIGINAL_COMMAND")
    name, rc = None, 5
    tokens: list = []
    try:
        if original is not None:
            if args:
                raise Refuse("bad_command", "the forced command takes --key-id only", 3)
            for var in ("SSH_TTY", "SSH_AUTH_SOCK", "DISPLAY"):
                if env.get(var):
                    raise Refuse("pty_refused" if var == "SSH_TTY" else "forward_refused",
                                 f"this session holds {var}", 3)
            lex = shlex.shlex(original, posix=True)
            lex.whitespace_split = True
            lex.commenters = ""
            tokens = list(lex)
        else:
            tokens = list(args)
        with open(os.path.join(d, "calls.jsonl"), "a") as f:
            f.write(json.dumps({"argv": argv[1:], "original": original, "tokens": tokens,
                                "env": sorted(k for k in env if k.startswith("SSH_")),
                                "pid": os.getpid()}) + "\n")
        if not tokens:
            raise Refuse("no_command", "no command: this key runs tandem remote verbs only", 3)
        for t in tokens:
            if not t or any(not 0x21 <= ord(c) <= 0x7e for c in t):
                raise Refuse("bad_command", "an argument holds a space or a control character", 2)
        verb = tokens[0]
        if verb not in VERBS:
            raise Refuse("unknown_verb", f"unknown verb {verb[:40]!r}", 2)
        rest = tokens[1:]
        name = verb
        if VERBS[verb]:
            if not rest or rest[0] not in VERBS[verb]:
                raise Refuse("unknown_subcommand", f"{verb} needs one of {', '.join(VERBS[verb])}", 2)
            name, rest = f"{verb} {rest[0]}", rest[1:]
        script = (st.get("script") or {}).get(name)
        if script:
            time.sleep(float(script.get("sleep", 0)))
            if "stdout" in script:
                sys.stdout.write(script["stdout"])
                sys.stdout.flush()
            return int(script.get("exit", 0))
        opts, pos = opts_of(rest)
        if name == "logs" and opts.get("--follow"):
            return follow(st)
        body, rc = answer(d, st, name, opts, pos, key_id)
        doc = {"ctl": CTL, "version": VERSION, "verb": name, "ok": rc == 0, **body}
        emit(doc)
        return rc
    except Refuse as r:
        emit({"ctl": CTL, "version": VERSION, "verb": name, "ok": False,
              "error": {"code": r.code, "message": str(r), **r.extra}})
        return r.rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
