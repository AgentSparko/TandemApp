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
    sim           true: the settings verbs, status, start and restart run a small model of the
                  managed overlay (SRV-845): SIM_SCHEMA's keys, versions with `current` and `good`,
                  plan tokens bound to the base version as the engine's are, the second token for
                  a key that feeds the fingerprint, and a lifecycle job that each `status` call
                  moves one step along `sim_steps` (default waiting, acting, done; a step
                  `failed_start` fails the start and puts the version back, `timeout` fails the
                  wait with nothing changed); `sim_hold` true keeps the job where it is; a `start`
                  replaces a job that waits, and is busy while one acts

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


# ---------------------------------------------------------------------------- the overlay model
SIM_SCHEMA = {
    "MAX_QUEUE": {"default": "16", "type": "integer", "restart": "engine", "fingerprint": False,
                  "managed": True},
    "QSE_ALLOW_FROM": {"default": "", "type": "cidrs", "restart": "hot", "fingerprint": False,
                       "managed": True},
    "QWEN38_PREFIX_CHUNK": {"default": "2048", "type": "integer", "restart": "engine",
                            "fingerprint": True, "managed": True},
    "QSE_MODE": {"default": "engine", "type": "enum", "restart": "engine", "fingerprint": False,
                 "managed": True, "values": ["engine", "chat"]},
    "HF_TOKEN": {"default": None, "type": "string", "restart": "engine", "fingerprint": False,
                 "managed": False, "why": "may hold a secret"},
    "PORT": {"default": "8000", "type": "integer", "restart": "engine", "fingerprint": False,
             "managed": False, "why": "the install's"},
}
SIM_SECRET = "hf_" + "s3cr3t" * 6


def _sim(st: dict) -> dict:
    vs = st.get("sim_versions") or [{"n": 0, "at": CREATED, "note": "empty", "set": {},
                                     "sha256": "0" * 64}]
    return {"versions": vs, "current": st.get("sim_current", 0), "good": st.get("sim_good", 0),
            "job": st.get("sim_job"), "schema": st.get("sim_schema") or SIM_SCHEMA}


def _set_of(m: dict, n: int) -> dict:
    for v in m["versions"]:
        if v["n"] == n:
            return dict(v["set"])
    raise Refuse("unknown_version", f"no overlay version {n}", 3)


def _effective(m: dict, overlay: dict) -> dict:
    out = {}
    for k, spec in m["schema"].items():
        if k in overlay:
            out[k] = overlay[k]
        elif k == "HF_TOKEN":
            out[k] = SIM_SECRET
        elif spec["default"] is not None:
            out[k] = spec["default"]
    return out


def _tok(kind: str, base: int, values: dict) -> str:
    import hashlib
    blob = json.dumps([kind, base, sorted(values.items())], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _sim_plan(m: dict, new: dict) -> dict:
    base = m["current"]
    old = _set_of(m, base)
    if new == old:
        raise Refuse("no_change", "the overlay already holds exactly these values", 3)
    before, after = _effective(m, old), _effective(m, new)
    effect = []
    for k in sorted(m["schema"]):
        a, b = (k in before, before.get(k)), (k in after, after.get(k))
        if a != b:
            effect.append({"key": k, "from": before.get(k), "to": after.get(k), "was_set": a[0],
                           "is_set": b[0], "restart": m["schema"][k]["restart"],
                           "fingerprint": m["schema"][k]["fingerprint"]})
    fp = [e["key"] for e in effect if e["fingerprint"]]
    out = {"base": base, "overlay": [{"key": k, "from": old.get(k), "to": new.get(k)}
                                     for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)],
           "effect": effect, "shadowed": [], "fingerprint_keys": fp,
           "restart_needed": any(e["restart"] == "engine" for e in effect),
           "confirm": _tok("plan", base, new), "warnings": []}
    if fp:
        out["confirm_fingerprint"] = _tok("fingerprint", base, new)
        out["warnings"].append(f"{', '.join(fp)} feed the disk cache's fingerprint")
    return out


def _sim_target(m: dict, pos: list, unsets: list) -> dict:
    new = _set_of(m, m["current"])
    for k in unsets:
        if k not in m["schema"]:
            raise Refuse("unknown_key", f"not a settings key: {k}", 2)
        if k not in new:
            raise Refuse("not_in_overlay", f"--unset {k}: the overlay does not set it", 3)
        del new[k]
    for p in pos:
        k, _, v = p.partition("=")
        if k not in m["schema"]:
            raise Refuse("unknown_key", f"not a settings key: {k}", 2)
        if not m["schema"][k]["managed"]:
            raise Refuse("key_not_managed", f"{k} is not managed: {m['schema'][k].get('why')}", 3)
        new[k] = v
    return new


def _sim_check(p: dict, opts: dict) -> None:
    c = opts.get("--confirm")
    if c != p["confirm"]:
        raise Refuse("confirm_required" if c is None else "confirm_mismatch",
                     "apply needs --confirm with the token of a plan of exactly these changes", 3)
    if "confirm_fingerprint" in p and opts.get("--confirm-fingerprint") != p["confirm_fingerprint"]:
        raise Refuse("confirm_required", "these changes make the disk cache miss", 3)


def _sim_job(d: str, st: dict, m: dict, kind: str, when: str | None, deadline, overlay=None) -> dict:
    cur = m["job"]
    if cur and cur["state"] == "acting":
        raise Refuse("busy", "another lifecycle job is acting; tandem remote status shows it", 4,
                     job=cur)
    if cur and cur["state"] == "waiting":
        cur.update(state="cancelled", ended_at=CREATED, message=f"replaced by a {kind} call")
    made = int(st.get("sim_made", 0)) + 1
    j = job(kind, when, "waiting")
    j["id"] = f"20261115T10{made // 60:02d}{made % 60:02d}-abcdef"
    if deadline is not None and j["when"]["mode"] != "now":
        j["when"]["deadline_s"] = int(deadline)
    if overlay is not None:
        j["overlay"] = overlay
    steps = list(st.get("sim_steps") or ["waiting", "acting", "done"])
    if j["when"]["mode"] == "now" and steps and steps[0] == "waiting":
        steps = [x for x in steps if x != "waiting"] or ["done"]
    j["state"] = "acting" if steps[0] == "acting" else "waiting"
    set_state(d, sim_made=made, sim_job=dict(j, _steps=steps, _i=0),
              **({"sim_versions": m["versions"]} if "sim_versions" not in st else {}))
    return j


def _public(j: dict | None) -> dict | None:
    return None if j is None else {k: v for k, v in j.items() if not k.startswith("_")}


def _sim_step(d: str, st: dict) -> dict | None:
    m = _sim(st)
    j = m["job"]
    if j is None or j["state"] in ("done", "failed", "cancelled", "lost") or st.get("sim_hold"):
        return j
    i = min(j["_i"] + 1, len(j["_steps"]) - 1)
    step = j["_steps"][i]
    j["_i"] = i
    upd = {}
    if step in ("waiting", "acting"):
        j["state"] = step
    elif step == "timeout":
        j.update(state="failed", ended_at=CREATED, message="1 request still ran after the deadline "
                                                           "(600 s); nothing changed")
    elif step in ("done", "failed_start"):
        ov = j.get("overlay")
        vs = list(m["versions"])
        if ov is not None:
            n = max(v["n"] for v in vs) + 1
            vs.append({"n": n, "at": CREATED, "note": ov.get("note", "settings apply"),
                       "set": ov["values"], "sha256": "%064x" % n})
            ov["to"] = n
            upd.update(sim_versions=vs)
        if step == "done":
            j.update(state="done", ended_at=CREATED, message="healthy")
            if ov is not None:
                upd.update(sim_current=ov["to"], sim_good=ov["to"])
        else:
            back = ov["from"] if ov else m["good"]
            j.update(state="failed", ended_at=CREATED,
                     message=f"the engine did not reach /health with overlay version "
                             f"{(ov or {}).get('to')}; version {back} is back and the engine answers",
                     restored={"version": back, "healthy": True})
            upd.update(sim_current=back)
    set_state(d, sim_job=j, **upd)
    return j


def sim_answer(d: str, st: dict, name: str, opts: dict, pos: list) -> tuple[dict, int]:
    m = _sim(st)
    if name == "status":
        j = _sim_step(d, st)
        m = _sim(state(d))
        healthy = st.get("engine", "healthy") == "healthy"
        return {"engine": {"state": "healthy" if healthy else "down", "pid": 4242 if healthy else None,
                           "port": 8000, "health_code": 200 if healthy else None,
                           "engine_api": "1.9", "version": "0.15.4",
                           "model": st.get("model", "fake-model") if healthy else None,
                           "running_mode": "engine" if healthy else None, "stopped_on_purpose": False},
                "mode": "engine", "profile": None, "engine_version": "0.15.4", "engine_api": "1.9",
                "http": {"token": True, "allow_from": None}, "install": True,
                "overlay": {"current": m["current"], "good": m["good"],
                            "versions": len(m["versions"]), "file": m["current"] > 0},
                "hook": True, "job": _public(j)}, 0
    if name in ("start", "restart"):
        j = _sim_job(d, st, m, name, opts.get("--when"), None)
        cur = state(d)["sim_job"]
        cur.update(state="done", ended_at=CREATED, message="already healthy")
        set_state(d, sim_job=cur)
        return {"job": _public(cur), "follow": "tandem remote status"}, 0
    if name == "settings get":
        eff = _effective(m, _set_of(m, m["current"]))
        over = _set_of(m, m["current"])
        keys = pos or sorted(m["schema"])
        rows = []
        for k in keys:
            if k not in m["schema"]:
                raise Refuse("unknown_key", f"not a settings key: {k}", 2)
            spec = m["schema"][k]
            row = {"key": k, "set": k in eff, "value": eff.get(k),
                   "source": "managed" if k in over else "local" if k == "HF_TOKEN" else
                   "install" if k == "PORT" else "default", "type": spec["type"],
                   "managed": spec["managed"], "restart": spec["restart"],
                   "fingerprint": spec["fingerprint"]}
            if spec["default"] is not None:
                row["default"] = spec["default"]
            if spec.get("values"):
                row["values"] = spec["values"]
            if spec.get("why"):
                row["why"] = spec["why"]
            rows.append(row)
        return {"keys": rows, "overlay": {"current": m["current"], "good": m["good"],
                                          "versions": len(m["versions"]), "file": m["current"] > 0},
                "hook": True}, 0
    if name == "settings plan":
        return {"plan": _sim_plan(m, _sim_target(m, pos, opts.get("--unset") or []))}, 0
    if name == "settings apply":
        new = _sim_target(m, pos, opts.get("--unset") or [])
        p = _sim_plan(m, new)
        _sim_check(p, opts)
        j = _sim_job(d, st, m, "settings", opts.get("--when", "idle"), opts.get("--deadline"),
                     overlay={"from": m["current"], "values": new, "note": "settings apply"})
        return {"job": j, "follow": "tandem remote status", "plan": p}, 0
    if name == "settings versions":
        return {"current": m["current"], "good": m["good"], "versions": m["versions"]}, 0
    if name == "settings restore":
        n = int(pos[0]) if pos else -1
        new = _set_of(m, n)
        p = _sim_plan(m, new)
        _sim_check(p, opts)
        j = _sim_job(d, st, m, "restore", opts.get("--when", "idle"), opts.get("--deadline"),
                     overlay={"from": m["current"], "values": new, "note": f"restore of version {n}"})
        return {"job": j, "follow": "tandem remote status"}, 0
    raise Refuse("unknown_verb", f"unknown verb {name!r}", 2)


SIM_VERBS = ("status", "start", "restart", "settings get", "settings plan", "settings apply",
             "settings versions", "settings restore")


def answer(d: str, st: dict, name: str, opts: dict, pos: list, key_id: str | None) -> tuple[dict, int]:
    if st.get("sim") and name in SIM_VERBS:
        return sim_answer(d, st, name, opts, pos)
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
