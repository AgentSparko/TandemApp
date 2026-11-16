"""The health page (SRV-835): what is wrong with each engine host and with this controller, in
words, and the fixes TandemApp can run itself.

    h = Health(fleet)                                    one per controller (Fleet.health())
    h.handle(method, parts, query, body, who)            the routes below; OnboardError refuses

    GET  /v1/fleet/health[?fresh=1]                      the controller, every engine host and
                                                         the pairing codes refused lately
    GET  /v1/fleet/health/{id}[?fresh=1]                 one of them (`controller`: this one)
    POST /v1/fleet/health/{id}/fix {action, ref?, confirm?, when?, instance?}
                                                         one fix, then that target checked again

A FINDING is one code of proxy/enginecodes.py (contracts/fleet-v1/engine-codes.json, `findings`
there gives each its severity and action):

    {code, severity, sentence, fix, action, facts, detail?}

`sentence` and `fix` are the table's, filled from the facts (the address and port tried, both
fingerprints, the free space, the path); `facts` holds them as values; `detail` is the other
side's own words when there are some (ssh's last line, already scrubbed by proxy/enginectl.py).
`severity` is error (it does not work), warning (it works, worse) or info (it will pass).
`action` is null or {id, label, changes, confirm}: what the button does. A finding for a changed
host key never offers a way to connect anyway: its only action shows how to pair again.

THE ENGINE HOST CHECKS, cheap and in this order (each a row of `checks`, ok true, false, or null
when an earlier one made it moot):

    ssh       `tandem remote status` over SSH (proxy/enginectl.py, STATUS_S): ssh_unreachable
              (retry; the address and port tried), host_key_changed (both fingerprints; nothing
              connects), not_paired (the key is refused: how to pair again), engine_too_old (the
              engine knows no such verb), ctl_failed
    engine    the status's engine state: engine_down (Start; `state` down or failed),
              engine_loading (loading or draining)
    version   its engine API and control versions against this TandemApp's pins
              (proxy/engineapi.py SPEAKS, enginectl.SPEAKS): another major is
              engine_api_unsupported; an older minor engine_too_old (Upgrade: a plan first, the
              apply needs its confirm)
    model     a healthy engine serves no model: no_model (the page opens the model step)
    http      GET /health on the engine's HTTP port from here: http_blocked when SSH says the
              engine runs but its port does not answer here (retry: both sides checked again),
              not_allowed when its allow-from list leaves this machine out
    token     GET /v1/models with the sealed token: token_missing or token_refused (Rotate:
              `tandem remote token rotate`, the new token straight into the sealed table)

When SSH fails but the engine serves over HTTP with the token, the SSH finding is a warning:
inference works, control does not. A changed host key and a refused key stay errors whatever
the HTTP port answers: the token is never sent there, so that the engine serves is not proven.

A REFUSED PAIRING CODE (`pairings`) is no target: no fix runs for it, so its finding carries no
action; the page's way on is Add engine, with a new code.

THE CONTROLLER'S CHECKS (`controller`): chatd (own: running and answering, else chatd_down or
chatd_silent with Restart; external: QSE_CHAT_URL answers, else chat_unreachable; off, OPS-288:
Chat is switched off, so chatd, the sandbox and the memory cgroup are not checked and say
nothing), the sandboxes as chatd reports them (its /health `sandbox`: the backend, docker or
kubernetes, and ready, unavailable or unknown; ready passes whatever the runtime). For an own
chatd with a container socket that is not reported ready, the socket answers /_ping and holds the
image (sandbox_off, sandbox_unreachable, sandbox_no_image; sandbox_unavailable when the socket is
fine but chatd still says no); in a cluster only chatd's cluster check counts (unavailable:
sandbox_unavailable with its words; running or not said: not checked); an external chatd's
unavailable is sandbox_unavailable, the memory cgroup on that host (Linux: the kernel's memory
controller, and its delegation to the user whose /run/user/<uid> holds the socket:
no_memory_cgroup), the free space where the state and chat data live (disk_low under DISK_WARN_GB,
an error under DISK_ERROR_GB), each enabled backend that the background poll found down
(backend_down; retry probes them now), and no_engine while the first run's wizard still waits
(`setup.needs_setup`).

THE FIXES. `retry` checks again (and probes the down backends); `start`, `rotate_token` and
`upgrade` run through EngineHostControl (proxy/enginectl.py) with this controller's key;
`restart_chatd` through proxy/appchat.py and waits for its job; `re_pair`, `pick_model` and
`add_engine` change nothing and answer how (`guide`). An action is run only when the target's
current findings offer it (`retry` always), else 409 not_offered. `upgrade` without `confirm`
answers the engine's plan (`needs_confirm` true); with the plan's `confirm` it applies, idle-gated
unless `when` is now. Each fix that changes something is audited. A fix answers for each
INSTANCE it was for, a finding of its code and, where a target has several of one code, the
backend or plugin it names (`instances`: each with `cleared`); `instance` in the body narrows the
fix's `cleared` and `finding` to that one (two backends down, one back: that one cleared). The
rights (proxy/app.py) are ACTIONS' `route`: a read's for retry and the guides, the Setup Guide's
for Start, pairing's for the token and an upgrade, the conductor's writes for chatd.

LIMITS. A target's result is kept CACHE_S and answered `cached` with its `age_s`; `fresh=1` checks
again unless the last check is younger than MIN_GAP_S (then `limited`). One check of a target runs
at a time; the engine hosts are checked in parallel (at most PARALLEL). At most FIXES_PER_MIN fixes
a target a minute (429 too_many_fixes, with `retry_after`: the seconds until one may run).

SECRETS. No finding, fact, guide or log line holds the HTTP token, a private key, a password or a
pairing code: the facts are enginecodes.FACTS only, the token goes only into an Authorization
header, and a rotated token goes from the engine's answer into the sealed table.
"""

from __future__ import annotations

import collections
import concurrent.futures
import http.client
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.parse

from proxy import enginecodes, enginectl, engineapi, enginepair

CONTRACT = "1.0"
CONTROLLER = "controller"
CACHE_S = 15.0
MIN_GAP_S = 3.0
PARALLEL = 8
FIXES_PER_MIN = 6
STATUS_S = 12.0
HTTP_S = 5.0
CHAT_S = 2.0
SOCKET_S = 3.0
DISK_WARN_GB = 5.0
DISK_ERROR_GB = 1.0
CHATD_WAIT_S = 45.0
SEVERITIES = ("error", "warning", "info")

#: action -> (the button's label, it changes something, it needs a confirm, the rights it needs)
ACTIONS: dict[str, tuple[str, bool, bool, str]] = {
    "retry": ("Check again", False, False, "dashboard"),
    "start": ("Start", True, False, "setup"),
    "rotate_token": ("Rotate the token", True, False, "fleet_trust"),
    "upgrade": ("Upgrade", True, True, "fleet_trust"),
    "restart_chatd": ("Restart chatd", True, False, "conductor_write"),
    "re_pair": ("How to pair again", False, False, "dashboard"),
    "pick_model": ("Pick a model", False, False, "dashboard"),
    "add_engine": ("Add engine", False, False, "dashboard"),
}

#: code -> (severity, action id or None): what the health page shows of each finding it makes
FINDINGS: dict[str, tuple[str, str | None]] = {
    # an engine host
    "ssh_unreachable": ("error", "retry"),
    "host_key_changed": ("error", "re_pair"),
    "not_paired": ("error", "re_pair"),
    "ctl_failed": ("error", "retry"),
    "engine_down": ("error", "start"),
    "engine_loading": ("info", None),
    "engine_too_old": ("warning", "upgrade"),
    "engine_api_unsupported": ("error", None),
    "no_model": ("warning", "pick_model"),
    "http_blocked": ("error", "retry"),
    "not_allowed": ("error", None),
    "not_an_engine": ("error", "retry"),
    "token_missing": ("error", "rotate_token"),
    "token_refused": ("error", "rotate_token"),
    # a pairing code the engine refused: no target, so no fix (the page links to Add engine)
    "wrong_code": ("warning", None),
    "code_spent": ("warning", None),
    "no_window": ("warning", None),
    "host_key_mismatch": ("error", None),
    "rate_limited": ("warning", None),
    # the controller
    "no_engine": ("info", "add_engine"),
    "chatd_down": ("error", "restart_chatd"),
    "chatd_silent": ("error", "restart_chatd"),
    "chat_unreachable": ("warning", None),
    "sandbox_off": ("warning", None),
    "sandbox_unreachable": ("warning", None),
    "sandbox_no_image": ("warning", None),
    "sandbox_unavailable": ("warning", None),
    "no_memory_cgroup": ("warning", None),
    "disk_low": ("warning", None),
    "backend_down": ("warning", "retry"),
    # the plugin host (SRV-859)
    "plugin_failed": ("warning", None),
    "plugin_no_sandbox": ("warning", None),
    "plugin_tampered": ("error", None),                             # SEC-18
    "plugin_untrusted": ("warning", None),
}

ERR = enginecodes.error
_RUN_UID = re.compile(r"^/run/user/([0-9]+)/")


# ======================================================================================== findings
def finding(code: str, detail: str | None = None, *, severity: str | None = None,
            action: str | None | bool = True, **facts) -> dict:
    """One finding: the table's sentence and fix filled from `facts`, its severity and action
    (`action=None` takes it away, a name replaces it)."""
    e = enginecodes.OnboardError(code, detail, **facts)
    sev, act = FINDINGS.get(code, ("error", None))
    if action is not True:
        act = action
    out = {"code": code, "severity": severity or sev, "sentence": e.message, "fix": e.fix,
           "action": action_view(act), "facts": dict(e.facts)}
    if e.detail:
        out["detail"] = e.detail
    return out


def from_error(exc: enginecodes.OnboardError, **kw) -> dict:
    return finding(exc.code, exc.detail, **kw, **exc.facts)


def action_view(act: str | None) -> dict | None:
    if act is None:
        return None
    label, changes, confirm, _ = ACTIONS[act]
    return {"id": act, "label": label, "changes": changes, "confirm": confirm}


def route_for(method: str, parts: list[str], body) -> str:
    """The rights a /v1/fleet/health request needs (proxy/app.py): a read's, or the fix's."""
    if method == "POST" and len(parts) == 3 and parts[2] == "fix" and isinstance(body, dict):
        act = ACTIONS.get(body.get("action")) if isinstance(body.get("action"), str) else None
        return act[3] if act else "dashboard"
    return "dashboard"


def instance_of(f: dict) -> str | None:
    """Which one of its code a finding is, where a target has several: the backend or plugin it
    names; None for the others."""
    facts = f.get("facts") or {}
    got = facts.get("backend") or facts.get("plugin")
    return str(got) if got is not None else None


def state_of(findings: list[dict]) -> str:
    sev = {f["severity"] for f in findings}
    return next((s for s in ("error", "warning") if s in sev), "ok")


def _check(name: str, ok, code: str | None = None) -> dict:
    return {"check": name, "ok": ok, "code": code}


def _v(text) -> tuple[int, int] | None:
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)", text) if isinstance(text, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


# ======================================================================================== the sandboxes
SANDBOX_BACKENDS = ("docker", "kubernetes")
SANDBOX_STATES = ("ready", "unavailable", "unknown")


def sandbox_backend(cfg: dict, env) -> str:
    """The backend chatd's sandboxes use, as its env file (else this controller's environment)
    sets it: `QSE_SANDBOX_BACKEND`, docker (a container socket, Docker's or Podman's) unless it
    says kubernetes (chatd/sandbox.py `backend_name`)."""
    name = (cfg.get("QSE_SANDBOX_BACKEND") or (env or {}).get("QSE_SANDBOX_BACKEND") or "")
    return "kubernetes" if name.strip().lower() == "kubernetes" else "docker"


def sandbox_report(doc) -> dict | None:
    """chatd's `/health` `sandbox` ({backend, state, why?}), checked; None when it says nothing
    usable (an older chatd, or one that does not answer)."""
    if not isinstance(doc, dict) or doc.get("backend") not in SANDBOX_BACKENDS or \
            doc.get("state") not in SANDBOX_STATES:
        return None
    why = doc.get("why")
    return {"backend": doc["backend"], "state": doc["state"],
            "why": why[:300] if isinstance(why, str) and why else None}


# ======================================================================================== the first run
def setup_state(fleet) -> dict:
    """Whether the page opens the first run's wizard (C5): nothing serves through this controller
    yet. `needs_setup` while no engine host is paired, no other fleet node is known and no backend
    is on; `first_page` is then `add_engine`."""
    local = getattr(fleet, "_local_id", None)
    engines = nodes = 0
    for n in fleet.cfg.nodes():
        if n["transport"] == "ssh" or (n["trust"] or {}).get("kind") == "ssh":
            engines += 1
        elif n["id"] != local and n.get("endpoint") not in ("local", ""):
            nodes += 1
    backends = sum(1 for b in fleet.cfg.backends() if b["enabled"])
    needs = engines == 0 and nodes == 0 and backends == 0
    return {"needs_setup": needs, "engines": engines, "nodes": nodes, "backends": backends,
            "first_page": "add_engine" if needs else None}


# ======================================================================================== small probes
def http_get(address: str, port: int, path: str, token: str | None = None,
             timeout: float = HTTP_S) -> tuple[int, dict | None] | None:
    """(status, JSON body or None) of one GET, or None when nothing answers. The token goes into
    the Authorization header only."""
    hdrs = {"Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        conn.request("GET", path, headers=hdrs)
        r = conn.getresponse()
        raw, st = r.read(1 << 20), r.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        hdrs = token = None
        conn.close()
    try:
        doc = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        doc = None
    return st, doc if isinstance(doc, dict) else None


def unix_get(path: str, url: str, timeout: float = SOCKET_S) -> tuple[int, bytes]:
    """One HTTP/1.0 GET over a unix socket (the container runtime's API): (status, body).
    OSError when nothing answers."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(f"GET {url} HTTP/1.0\r\nHost: runtime\r\nAccept: */*\r\n\r\n".encode())
        buf = b""
        while len(buf) < (1 << 20):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    line = head.split(b"\r\n", 1)[0].split()
    if len(line) < 2 or not line[1].isdigit():
        raise OSError("not an HTTP answer")
    return int(line[1]), body


def memory_cgroup(socket_path: str | None = None, root: str = "/sys/fs/cgroup",
                  proc: str = "/proc/cgroups") -> tuple[bool, str | None]:
    """(ok, why not): the kernel's memory controller is on and, for a socket under
    /run/user/<uid>, delegated to that user's systemd (rootless Podman sets limits there)."""
    try:
        with open(os.path.join(root, "cgroup.controllers")) as f:
            ctrl = f.read().split()
    except OSError:
        ctrl = None
    if ctrl is None:                                 # cgroup v1: /proc/cgroups' enabled column
        try:
            with open(proc) as f:
                rows = [x.split() for x in f if not x.startswith("#")]
        except OSError:
            return False, "neither cgroup v2 nor /proc/cgroups can be read"
        on = any(r[0] == "memory" and r[-1] == "1" for r in rows if len(r) >= 4)
        return (True, None) if on else (False, "the kernel's memory controller is off")
    if "memory" not in ctrl:
        return False, "the kernel's memory controller is off"
    m = _RUN_UID.match(socket_path or "")
    if m:
        uid = m.group(1)
        p = os.path.join(root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service",
                         "cgroup.controllers")
        try:
            with open(p) as f:
                if "memory" not in f.read().split():
                    return False, f"the memory controller is not delegated to user {uid}"
        except OSError:
            pass                                     # not readable from here: not proven either way
    return True, None


# ======================================================================================== the service
class Health:
    def __init__(self, fleet, *, clock=time.time, platform: str = sys.platform, env=None,
                 disk=shutil.disk_usage, cgroup=memory_cgroup, chat_mode: str | None = None):
        self.fleet = fleet
        self.clock = clock
        self.platform = platform
        self.env = os.environ if env is None else env
        self.disk = disk
        self.cgroup = cgroup
        self.chat_mode = chat_mode
        self._cache: dict[str, tuple[float, dict]] = {}
        self._running: dict[str, threading.RLock] = {}
        self._fixes: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    @property
    def cfg(self):
        return self.fleet.cfg

    @property
    def onboarding(self):
        return self.fleet.engines()

    @property
    def chat(self):
        """The chatd this controller owns (proxy/appchat.py), or None."""
        return getattr(self.fleet, "chat", None)

    def chat_off(self) -> bool:
        """Chat is switched off here (OPS-288): `chat_mode` off, else the chat forward's state."""
        if self.chat_mode is not None:
            return self.chat_mode == "off"
        from tandemapp import chat_forward
        return chat_forward.is_off()

    def log(self, msg: str) -> None:
        self.fleet.log(msg)

    # ------------------------------------------------------------------ the routes
    def handle(self, method: str, parts: list[str], q: dict, body, who: str) -> dict:
        sub = parts[1:]
        fresh = str(q.get("fresh") or "") in ("1", "true", "yes")
        if method == "GET" and sub == []:
            return self.all(fresh=fresh)
        if method == "GET" and len(sub) == 1:
            return {"contract_version": CONTRACT, **self.target(sub[0], fresh=fresh)}
        if method == "POST" and len(sub) == 2 and sub[1] == "fix":
            return self.fix(sub[0], body, who)
        raise ERR("not_found", status=404)

    # ------------------------------------------------------------------ the views
    def all(self, *, fresh: bool = False) -> dict:
        rows = self.onboarding._ssh_rows()
        ids = [r["id"] for r in rows]
        with self._lock:
            for k in [k for k in self._cache if k != CONTROLLER and k not in ids]:
                del self._cache[k]
        out: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(PARALLEL, len(ids) + 1)),
                                                   thread_name_prefix="health") as ex:
            futs = {ex.submit(self.target, tid, fresh=fresh): tid for tid in [CONTROLLER] + ids}
            for fut in concurrent.futures.as_completed(futs):
                out[futs[fut]] = fut.result()
        engines = [out[i] for i in ids]
        pairings = self.pairings()
        every = [out[CONTROLLER]] + engines + [{"state": state_of([p["finding"]])} for p in pairings]
        worst = next((s for s in ("error", "warning") if any(t["state"] == s for t in every)), "ok")
        return {"contract_version": CONTRACT, "checked_at": self.clock(), "state": worst,
                "setup": setup_state(self.fleet), "controller": out[CONTROLLER],
                "engines": engines, "pairings": pairings,
                "actions": {k: action_view(k) for k in ACTIONS}}

    def pairings(self) -> list[dict]:
        return [{"address": r["address"], "port": r["port"], "at": r["at"],
                 "finding": from_error(r["error"])} for r in self.onboarding.refusals()]

    def target(self, tid: str, *, fresh: bool = False, force: bool = False) -> dict:
        """One target's view: from the cache while it is young (CACHE_S; MIN_GAP_S with `fresh`),
        else checked, one check of a target at a time. `force` (after a fix) checks again unless a
        check began after this call did."""
        if tid != CONTROLLER:
            self.onboarding._row(tid)                # not_found / not_ssh before anything runs
        asked = self.clock()
        if not force:
            got = self._cached(tid, fresh)
            if got is not None:
                return got
        with self._busy(tid):
            with self._lock:
                c = self._cache.get(tid)
            if c is not None and c[0] > asked:       # checked while this call waited
                return dict(c[1], age_s=round(self.clock() - c[0], 1), cached=True, limited=False)
            if not force:
                got = self._cached(tid, fresh)
                if got is not None:
                    return got
            now = self.clock()
            view = self.check_controller() if tid == CONTROLLER else \
                self.check_engine(self.onboarding._row(tid))
            view.update(checked_at=now, age_s=0.0, cached=False, limited=False,
                        state=state_of(view["findings"]))
            with self._lock:
                self._cache[tid] = (now, view)
            return dict(view)

    def _busy(self, tid: str) -> threading.RLock:
        """The target's lock: its checks and its fixes run one at a time (they share the host's
        SSH key file and control socket)."""
        with self._lock:
            return self._running.setdefault(tid, threading.RLock())

    def _cached(self, tid: str, fresh: bool) -> dict | None:
        with self._lock:
            c = self._cache.get(tid)
        if c is None:
            return None
        age = self.clock() - c[0]
        if age < (MIN_GAP_S if fresh else CACHE_S):
            return dict(c[1], age_s=round(age, 1), cached=True, limited=fresh)
        return None

    # ------------------------------------------------------------------ an engine host
    def ctl(self, row: dict) -> enginectl.EngineHostControl:
        o = self.onboarding
        return enginectl.for_node(row, state_dir=o.root, keys=o.keys, log=self.fleet.log,
                                  timeouts={"status": STATUS_S})

    def check_engine(self, row: dict) -> dict:
        from proxy import engineonboard
        t = row["trust"] or {}
        host, port, user = t.get("host") or row["address"], t.get("port", 22), t.get("user")
        address = row["address"] or t.get("host")
        http_port = t.get("http_port") or enginepair.DEFAULT_PORT
        found: list[dict] = []
        checks: list[dict] = []
        status, ssh_finding = None, None
        try:
            ctl = self.ctl(row)
            try:
                status = ctl.status()
            finally:
                ctl.close()
        except enginectl.CtlError as exc:
            e = engineonboard.ctl_error(exc, host=host, port=port, user=user)
            ssh_finding = from_error(e)
        code = ssh_finding and ssh_finding["code"]
        checks.append(_check("ssh", status is not None, code))
        checks.append(_check("host_key", True if status is not None else
                             False if code == "host_key_changed" else None,
                             "host_key_changed" if code == "host_key_changed" else None))
        eng = (status or {}).get("engine") or {}
        state = eng.get("state")
        if ssh_finding is not None:
            found.append(ssh_finding)
            checks += [_check("engine", None), _check("version", None), _check("model", None)]
        else:
            # the engine's own state
            if state in ("down", "failed"):
                why = "it ended with an error; its log says why" if state == "failed" else \
                    "it was stopped on purpose" if eng.get("stopped_on_purpose") else None
                found.append(finding("engine_down", why, state=state, host=host))
            elif state in ("loading", "draining"):
                found.append(finding("engine_loading", None, state=state, host=host))
            ec = next((f["code"] for f in found if f["code"] in ("engine_down", "engine_loading")),
                      None)
            checks.append(_check("engine", state == "healthy", ec))
            # versions: the engine API (its /health's, as status says) and the control's
            v = self._versions(status, host)
            if v is not None:
                found.append(v)
            checks.append(_check("version", v is None, v and v["code"]))
            # a model
            if state == "healthy" and not eng.get("model"):
                found.append(finding("no_model", None, host=host))
                checks.append(_check("model", False, "no_model"))
            else:
                checks.append(_check("model", True if state == "healthy" else None))
        # HTTP from here, and the token
        if ssh_finding is not None or state in ("healthy", "loading", "draining"):
            # never the token to a host whose SSH key changed or that refuses this key: the
            # address may not be the paired machine any more
            trusted = code not in ("host_key_changed", "not_paired")
            h, tok, answered = self._http(row, address, http_port, serving=state == "healthy",
                                          host=host, token=trusted)
            if ssh_finding is not None:
                if not answered:
                    ssh_finding["detail"] = "; ".join(x for x in (
                        ssh_finding.get("detail"),
                        f"its HTTP port {http_port} does not answer either") if x)
                    h = None
                elif h is None and tok is None and trusted:
                    # it serves with the token; only the control is cut off. Never for a changed
                    # host key or a refused key: no token went there, so serving is not proven
                    ssh_finding["severity"] = "warning"
            found += [x for x in (h, tok) if x is not None]
            http_ok = False if h is not None else True if answered else None
            checks.append(_check("http", http_ok, h and h["code"]))
            checks.append(_check("token", (tok is None) if answered and h is None and trusted
                                 else None, tok and tok["code"]))
        else:
            checks += [_check("http", None), _check("token", None)]
        return {"id": row["id"], "kind": "engine", "alias": row["alias"], "address": address,
                "ssh": {"host": host, "port": port, "user": user},
                "http": {"port": http_port, "token": self.onboarding.keys.http_token_hint(row["id"])},
                "host_key": t.get("host_key"),
                "engine_version": (status or {}).get("engine_version") or eng.get("version"),
                "engine_api": (status or {}).get("engine_api") or eng.get("engine_api"),
                "mode": (status or {}).get("mode"), "engine_state": state,
                "model": eng.get("model"),
                "findings": found, "checks": checks}

    @staticmethod
    def _versions(status: dict, host: str) -> dict | None:
        eng = status.get("engine") or {}
        api = status.get("engine_api") or eng.get("engine_api")
        ctl_v = status.get("version")
        want_api, want_ctl = _v(engineapi.SPEAKS), _v(enginectl.SPEAKS)
        got_api, got_ctl = _v(api), _v(ctl_v)
        if api is not None and (got_api is None or got_api[0] != want_api[0]):
            if got_api is not None and got_api[0] < want_api[0]:
                return finding("engine_too_old", f"engine API {api}; TandemApp speaks "
                                                 f"{engineapi.SPEAKS}", host=host)
            return finding("engine_api_unsupported", None, host=host, engine_api=str(api),
                           speaks=engineapi.SPEAKS)
        if got_ctl is not None and got_ctl[0] != want_ctl[0]:
            return finding("engine_api_unsupported", f"its control speaks {ctl_v}, TandemApp "
                                                     f"{enginectl.SPEAKS}", host=host,
                           engine_api=str(api or "?"), speaks=engineapi.SPEAKS)
        old = []
        if got_api is not None and got_api < want_api:
            old.append(f"engine API {api}, TandemApp is made for {engineapi.SPEAKS}")
        if got_ctl is not None and got_ctl < want_ctl:
            old.append(f"control {ctl_v}, TandemApp is made for {enginectl.SPEAKS}")
        if old:
            return finding("engine_too_old", "; ".join(old), host=host)
        return None

    def _http(self, row: dict, address: str, port: int, *, serving: bool, host: str,
              token: bool = True) -> tuple[dict | None, dict | None, bool]:
        """(the HTTP finding, the token finding, whether the port answered): GET /health (an
        open path of the engine), then GET /v1/models with the sealed token. A port that does not
        answer is http_blocked only while SSH says the engine serves."""
        got = http_get(address, port, "/health")
        if got is None:
            return (finding("http_blocked", None, port=port, address=address, host=host)
                    if serving else None), None, False
        st, doc = got
        if st == 403 and str(((doc or {}).get("error") or {}).get("type", "")).startswith("forbidden"):
            return finding("not_allowed", None, address=address, port=port), None, True
        if st not in (200, 500, 503) or doc is None:
            return finding("not_an_engine", f"/health answered HTTP {st}", address=address,
                           port=port), None, True
        if not token:
            return None, None, True
        tok = self.cfg.engine_token(row["id"])
        if not tok:
            return None, finding("token_missing", None, host=host), True
        got = http_get(address, port, "/v1/models", tok)
        tok = None
        if got is None:
            return (finding("http_blocked", "it stopped answering during the check", port=port,
                            address=address, host=host) if serving else None), None, False
        if got[0] == 401:
            return None, finding("token_refused", None, host=host), True
        return None, None, True

    # ------------------------------------------------------------------ the controller
    def check_controller(self) -> dict:
        found: list[dict] = []
        checks: list[dict] = []
        chat = self.chat
        state_dir = os.path.dirname(self.cfg.path)
        sock = backend = None
        off = self.chat_off()
        if off:
            chat = None
            checks.append(_check("chatd", None))
            checks.append(_check("sandbox", None))
        elif chat is not None:
            st = chat.status()
            log = chat.log_path
            if not st["running"]:
                w = chat.view().get("watch") or {}
                why = ("stopped by hand" if w.get("stopped_by_hand") else
                       "the watcher gave up for this hour" if w.get("gave_up") else None)
                found.append(finding("chatd_down", why, path=log))
            elif not st["answers"]:
                found.append(finding("chatd_silent", None, path=log))
            checks.append(_check("chatd", st["running"] and st["answers"],
                                 None if st["running"] and st["answers"] else found[-1]["code"]))
            told = sandbox_report(st.get("sandbox") if st["answers"] else None)
            backend = (told or {}).get("backend") or sandbox_backend(chat.cfg, self.env)
            if backend == "docker":
                sock = chat.cfg.get("QSE_SANDBOX_SOCKET") or "/var/run/docker.sock"
            sb, sb_ok = self._own_sandbox(chat, backend, sock, told)
            if sb is not None:
                found.append(sb)
            checks.append(_check("sandbox", sb_ok, sb and sb["code"]))
        else:
            url = self.env.get("QSE_CHAT_URL") or "http://127.0.0.1:8016"
            u = urllib.parse.urlsplit(url)
            got = http_get(u.hostname or "127.0.0.1", u.port or 80, "/health", timeout=CHAT_S) \
                if u.scheme == "http" else None
            ok = got is not None and got[0] == 200
            if not ok:
                found.append(finding("chat_unreachable", None, address=f"{u.hostname}:{u.port}"))
            checks.append(_check("chatd", ok, None if ok else "chat_unreachable"))
            # its sandboxes as it says (no socket of its host to look at from here)
            told = sandbox_report((got[1] or {}).get("sandbox") if ok else None)
            backend = (told or {}).get("backend")
            sb = None
            if told is not None and told["state"] == "unavailable":
                sb = finding("sandbox_unavailable", told.get("why") or
                             f"chatd says its {told['backend']} sandboxes are not ready")
                found.append(sb)
            checks.append(_check("sandbox", None if told is None or told["state"] == "unknown"
                                 else sb is None, sb and sb["code"]))
        # the memory cgroup, where an own chatd's sandboxes run
        if chat is not None and self.platform.startswith("linux") and os.path.exists(sock or ""):
            ok, why = self.cgroup(sock)
            if not ok:
                found.append(finding("no_memory_cgroup", why))
            checks.append(_check("memory_cgroup", ok, None if ok else "no_memory_cgroup"))
        else:
            checks.append(_check("memory_cgroup", None))
        # free space
        paths = [state_dir] + ([chat.data] if chat is not None else [])
        low = None
        for p in paths:
            d = self._disk(p)
            if d is not None and (low is None or d[1] < low[1]):
                low = d
        if low is not None and low[1] < DISK_WARN_GB * (1 << 30):
            gb = round(low[1] / (1 << 30), 1)
            found.append(finding("disk_low", None, path=low[0], free_gb=gb, need_gb=DISK_WARN_GB,
                                 severity="error" if gb < DISK_ERROR_GB else "warning"))
        checks.append(_check("disk", low is None or low[1] >= DISK_WARN_GB * (1 << 30),
                             None if low is None or low[1] >= DISK_WARN_GB * (1 << 30)
                             else "disk_low"))
        # the backends the poll found down
        down = [b for b in self.fleet.svc.backends_view() if b["state"] == "down"]
        for b in down:
            found.append(finding("backend_down", (b.get("last_error") or "")[:200] or None,
                                 backend=b["label"] or b["id"]))
        checks.append(_check("backends", not down, "backend_down" if down else None))
        # the plugins (SRV-859): one that gave up or may not start, one with no sandbox
        host = getattr(self.fleet.svc, "plugin_host", None)
        probs = host.problems() if host is not None else []
        for pr in probs:
            found.append(finding(pr["code"], (pr.get("detail") or "")[:300] or None,
                                 plugin=pr["plugin"]))
        checks.append(_check("plugins", not probs, probs[0]["code"] if probs else None))
        # the first run
        setup = setup_state(self.fleet)
        if setup["needs_setup"]:
            found.append(finding("no_engine"))
        checks.append(_check("engines", not setup["needs_setup"],
                             "no_engine" if setup["needs_setup"] else None))
        return {"id": CONTROLLER, "kind": "controller", "alias": "TandemApp",
                "address": None,
                "chat": "off" if off else "own" if chat is not None else "external",
                "sandbox_backend": backend, "sandbox_socket": sock, "findings": found,
                "checks": checks}

    def _disk(self, path: str) -> tuple[str, int] | None:
        p = path
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        try:
            return path, int(self.disk(p or "/").free)
        except OSError:
            return None

    def _own_sandbox(self, chat, backend: str, sock: str | None,
                     told: dict | None) -> tuple[dict | None, bool | None]:
        """(the finding, the check's ok) for the sandboxes of the chatd this controller runs.
        chatd's own word comes first (its `/health` `sandbox`): ready is ready, whatever runtime
        it uses. In a cluster nothing else is looked at: the cluster check runs in chatd, so
        while it runs (or chatd does not say) the check is not run. With a container socket the
        socket is probed when chatd does not say it is ready, for the precise reason."""
        if told is not None and told["state"] == "ready":
            return None, True
        if backend == "kubernetes":
            if told is not None and told["state"] == "unavailable":
                return finding("sandbox_unavailable", told.get("why") or
                               "chatd's cluster check failed"), False
            return None, None
        image = chat.cfg.get("QSE_SANDBOX_IMAGE") or "tandem-sandbox:1"
        sb = self._sandbox(sock, image)
        if sb is None and told is not None and told["state"] == "unavailable":
            sb = finding("sandbox_unavailable", told.get("why") or
                         "chatd says its sandboxes are not ready")
        return sb, sb is None

    @staticmethod
    def _sandbox(sock: str, image: str) -> dict | None:
        if not os.path.exists(sock):
            return finding("sandbox_off", None, path=sock)
        try:
            st, body = unix_get(sock, "/_ping")
        except PermissionError:
            return finding("sandbox_unreachable", "this user may not open the socket", path=sock)
        except OSError as exc:
            return finding("sandbox_unreachable", type(exc).__name__, path=sock)
        if st != 200:
            return finding("sandbox_unreachable", f"/_ping answered HTTP {st}", path=sock)
        try:
            st, _ = unix_get(sock, f"/images/{urllib.parse.quote(image, safe='')}/json")
        except OSError as exc:
            return finding("sandbox_unreachable", type(exc).__name__, path=sock)
        if st == 404:
            return finding("sandbox_no_image", None, image=image)
        return None

    # ------------------------------------------------------------------ the fixes
    def _rate(self, tid: str, alias: str) -> None:
        now = self.clock()
        with self._lock:
            q = self._fixes.setdefault(tid, collections.deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= FIXES_PER_MIN:
                raise ERR("too_many_fixes", node=alias,
                          retry_after=max(1, math.ceil(60 - (now - q[0]))))
            q.append(now)

    def fix(self, tid: str, body, who: str) -> dict:
        from proxy import engineonboard
        b = engineonboard._fields(body, {"action", "ref", "confirm", "when", "instance"},
                                  ("action",))
        action = b["action"]
        if not isinstance(action, str) or action not in ACTIONS:
            raise ERR("bad_request", f"action is one of {', '.join(ACTIONS)}")
        when = b.get("when", "idle")
        if when not in ("now", "idle"):
            raise ERR("bad_request", "when is now or idle")
        inst = b.get("instance")
        if inst is not None and (not isinstance(inst, str) or not 0 < len(inst) <= 200):
            raise ERR("bad_request", "instance is the backend or plugin a finding names")
        row = None if tid == CONTROLLER else self.onboarding._row(tid)
        alias = "TandemApp" if row is None else row["alias"]
        before = self.target(tid)
        offered = {f["action"]["id"] for f in before["findings"] if f["action"]} | {"retry"}
        if action not in offered:
            raise ERR("not_offered", f"it offers {', '.join(sorted(offered))}", node=alias)
        self._rate(tid, alias)
        # what the fix is for: each finding that offers it, one instance each (narrowed to the
        # instance named)
        codes = [(f["code"], instance_of(f)) for f in before["findings"]
                 if f["action"] and f["action"]["id"] == action
                 and (inst is None or instance_of(f) == inst)]
        with self._busy(tid):
            return self._fix(tid, row, alias, action, b, when, before, codes, who)

    def _fix(self, tid: str, row: dict | None, alias: str, action: str, b: dict, when: str,
             before: dict, codes: list, who: str) -> dict:
        out = {"contract_version": CONTRACT, "id": tid, "action": action, "done": True,
               "needs_confirm": False, "job": None, "result": None, "guide": None}
        if action == "retry":
            if row is None:
                self._probe_backends()
        elif action in ("re_pair", "pick_model", "add_engine"):
            out["guide"] = self.guide(action, row, before)
        elif action == "restart_chatd":
            out["job"] = self._restart_chatd(who)
            out["done"] = out["job"]["state"] == "done"
        elif action == "start":
            doc = self._ctl_call(row, lambda c: c.start())
            out["job"] = doc.get("job")
            self._audit(who, "start", row, "engine started from the health page")
        elif action == "rotate_token":
            hid = row["id"]
            doc = self._ctl_call(row, lambda c: self.onboarding.keys.rotate_http_token(
                c, hid, who=who))
            out["result"] = {"rotated": bool(doc.get("rotated", True)), "stored": doc.get("stored"),
                             "hint": doc.get("hint")}
            self._audit(who, "token", row, "HTTP token rotated from the health page")
        elif action == "upgrade":
            out.update(self._upgrade(row, b, when, who))
        after = self.target(tid, force=True)
        still = [f for f in after["findings"] if (f["code"], instance_of(f)) in codes]
        left = {(f["code"], instance_of(f)) for f in still}
        out["target"] = after
        out["finding"] = still[0] if still else None
        out["cleared"] = not still
        out["instances"] = [{"code": c, "instance": i, "cleared": (c, i) not in left}
                            for c, i in codes]
        if ACTIONS[action][1]:
            self.log(f"[health] {alias} ({tid}): {action} by {who} -> "
                     f"{'cleared' if not still else 'still ' + still[0]['code']}")
        return out

    def _ctl_call(self, row: dict, fn):
        from proxy import engineonboard
        t = row["trust"] or {}
        f = {"host": t.get("host"), "port": t.get("port"), "user": t.get("user")}
        try:
            ctl = enginectl.for_node(row, state_dir=self.onboarding.root, keys=self.onboarding.keys,
                                     log=self.fleet.log)
        except enginectl.CtlError as exc:
            raise engineonboard.ctl_error(exc, **f) from None
        try:
            return fn(ctl)
        except enginectl.CtlError as exc:
            raise engineonboard.ctl_error(exc, **f) from None
        finally:
            ctl.close()

    def _audit(self, who: str, action: str, row: dict, text: str, **detail) -> None:
        self.cfg.log_audit(who, action, "node", row["id"], detail={"text": text, **detail})

    def _upgrade(self, row: dict, b: dict, when: str, who: str) -> dict:
        ref = b.get("ref") or self.env.get("TANDEM_ENGINE_REF") or None
        if ref is None or not isinstance(ref, str) or not enginectl.REF_RE.match(ref):
            raise ERR("bad_request", "ref is the engine release to upgrade to (a tag such as "
                                     "v0.16.0), or set TANDEM_ENGINE_REF")
        confirm = b.get("confirm")
        if confirm is None:
            doc = self._ctl_call(row, lambda c: c.upgrade_plan(ref=ref))
            return {"done": False, "needs_confirm": True, "result": {"ref": ref,
                                                                     "plan": doc.get("plan")}}
        if not isinstance(confirm, str) or not enginectl.CONFIRM_RE.match(confirm):
            raise ERR("bad_request", "confirm is the plan's confirm token")
        doc = self._ctl_call(row, lambda c: c.upgrade_apply(confirm=confirm, ref=ref, when=when))
        self._audit(who, "upgrade", row, f"engine upgrade to {ref} ({when})", ref=ref, when=when)
        return {"job": doc.get("job"), "result": {"ref": ref, "plan": doc.get("upgrade")}}

    def _restart_chatd(self, who: str) -> dict:
        chat = self.chat
        if chat is None:
            raise ERR("not_offered", "chatd is not this controller's", node="TandemApp")
        from proxy.nodechat import ChatError
        try:
            job = chat.act("restart", who)
        except ChatError as exc:
            raise ERR("busy", exc.message) from None
        t_end = time.monotonic() + CHATD_WAIT_S
        while time.monotonic() < t_end:
            cur = chat.job
            if cur is None or cur.get("id") != job["id"] or cur.get("state") != "running":
                break
            time.sleep(0.1)
        cur = chat.job if chat.job is not None and chat.job.get("id") == job["id"] else job
        return {k: cur.get(k) for k in ("id", "action", "state", "started_at", "ended_at", "rc",
                                        "message")}

    def _probe_backends(self) -> None:
        svc = self.fleet.svc
        down = {b["id"] for b in svc.backends_view() if b["state"] == "down"}
        for b in svc.cfg.backends():
            if b["id"] in down:
                try:
                    svc.health.check(b)
                except Exception as exc:                             # noqa: BLE001
                    self.log(f"[health] backend {b['id']}: the probe failed: "
                             f"{type(exc).__name__}")

    def guide(self, action: str, row: dict | None, view: dict) -> dict:
        """What the page shows for an action that changes nothing."""
        if action == "re_pair":
            address = (row or {}).get("address") or view.get("address")
            hk = next((f["facts"] for f in view["findings"] if f["code"] == "host_key_changed"), {})
            return {"open": "add_engine", "engine": (row or {}).get("id"), "address": address,
                    "pinned": hk.get("pinned"), "offered": hk.get("offered"),
                    "steps": ["On the engine host, run `tandem pair`: it prints a code, the SSH "
                              "host key's fingerprint and the address.",
                              "Compare that fingerprint with the one shown here; if they differ, "
                              "do not pair.",
                              f"Open Add engine, pick the engine or type {address}, and type the "
                              f"code."]}
        if action == "pick_model":
            return {"open": "model", "engine": (row or {}).get("id"), "address": None,
                    "pinned": None, "offered": None,
                    "steps": ["Open Add engine's model step for this engine and pick a model; "
                              "the page shows its download."]}
        return {"open": "add_engine", "engine": None, "address": None, "pinned": None,
                "offered": None,
                "steps": ["Install the engine on its host with its one command; it prints a "
                          "code, the SSH host key's fingerprint and the address.",
                          "Open Add engine, pick the engine or type its address, and type the "
                          "code."]}
