"""The plugin host (SRV-859): one supervised process per enabled plugin, its contributions wired in
while it runs, its capabilities enforced at each use.

    host = PluginHost(svc)          one per controller (proxy/service.py); idle with no plugin
    host.start()                    the watcher: the registry read every 2 s and on each change
    host.sync()                     enabled plugins started, disabled or removed ones stopped, a
                                    changed one (settings, grants, secrets, version) restarted
    host.start_plugin(id), stop_plugin(id), restart_plugin(id)
                                    the admin's (a hand stop holds until a hand start)
    host.status(id), logs(id, n), problems()

THE PROCESS. `entry.command` (argv, never a shell) from the manifest's folder, at its trust level
(proxy/pluginrt.py): `sandboxed` (the default) in a container or pod of the chat sandbox's class
with the egress filter; `host` as a process of the app's user, only once the admin granted it
(the plugins scope's `trust` key); `engine` refused: "not available yet" until the in-process
chain exists. Its environment holds only what its grants allow, never the controller's:

    TANDEM_PLUGIN, TANDEM_PLUGIN_VERSION         its id and version
    TANDEM_PLUGIN_SETTINGS                       its own settings (JSON, the schema's defaults in)
    TANDEM_PLUGIN_DATA                           its own data folder (/workspace in a sandbox)
    TANDEM_PLUGIN_CAPABILITIES                   the capabilities granted (JSON list)
    <NAME>                                       each secret granted by `secrets:<NAME>`
    the manifest's `entry.env`, LANG, PYTHONUNBUFFERED; PATH and HOME (a host plugin: PATH of
    this process, HOME its data folder)

THE PROTOCOL is MCP over stdio (one JSON-RPC message a line; chatd/mcp.py's client) plus:

    hooks/call        {hook, stage, payload, mode, request, event, hooks, id, ts}: a hooks-v1
                      envelope for the contributed hook `hook`, the event at the hook's payload
                      level -> a hooks-v1 result
    decision/ask      {provider, question, instructions, options, model, deadline_ms, input}:
                      `input` at the plugin's prompts level (meta: {chars}; redacted and full:
                      {text}) -> {label, confidence?, probabilities?}
    backend/request   {backend, method, path, body}: a request to a contributed backend ->
                      {status, body} or {status, events: [...]} (a stream)

and the plugin may ask the host (requests from its side):

    tandem/settings   its own settings: needs settings:read
    tandem/secret     {name}: a secret granted by secrets:<name>; another plugin's never
    tandem/engine     engine control: refused until it exists (engine:control, F3)

A request whose capability was not granted is refused (JSON-RPC error -32001) and journaled: the
plugin's `refusals` and the config database's audit (`refused`, the capability's name, never a
value).

SUPERVISION. A plugin that exits, or does not answer `initialize` within INIT_S, is started again
after a back-off (1, 2, 4, 8, 16, 32, 60 s). At the MAX_CRASHES-th failure within an hour the host
gives up: the plugin is disabled in the registry (by `plugin-host`, audited) and a finding names
it (plugin_failed) until it is enabled again. A hand stop holds it until a hand start. No sandbox
runtime here: the plugin waits (state `unavailable`, finding plugin_no_sandbox) and is looked at
again every UNAVAILABLE_S; that is not counted as a failure.

CONTRIBUTIONS, registered when it runs and gone when it stops:

    tools               offered to chatd as `mcp__plugin_<id>__<tool>` (/chat-engine/v1/plugins/
                        tools, proxy/chatengine.py): a tool the manifest declares AND the server
                        lists, with the manifest's checked schema and annotations
    hooks               hook rows of kind plugin in the runner (proxy/hooks.py `contrib`): id
                        `<plugin>/<hook>`, priority 100, mode on, the manifest's stages, role and
                        payload. A registry hook of kind plugin with target `<plugin>/<hook>`
                        takes its place (its priority, scope, mode and policy). The payload must
                        be within the plugin's granted prompts: level, checked when it is
                        registered and again at each call
    decision_providers  providers of kind plugin (proxy/providers.py `contrib`): id
                        `<plugin>/<provider>`, mode shadow (asked off the request's path,
                        journaled, never used) until a registry provider of kind plugin with that
                        target says on or ab
    backends            rows of the backends store (kind generic, models listed, label
                        "<title>: <name> (plugin <id>)", id `plugin-<id>-<name>`) whose URL is
                        this controller's /plugin-backends/<id>/<name> (loopback only), answered
                        through backend/request
    skills              the plugin's skill folders, one more `skill_dirs` entry each for the
                        controller's conductor (read from the installed tree, never written)
    panels              its HTML files served under /plugin-panels/<id>/<panel>/ with a CSP
                        sandbox (an opaque origin), and the bridge (`bridge`): the methods its
                        capabilities allow
    engine_hooks        refused: they need trust engine

LOGS. The plugin's stderr and the host's lines about it go to `<state dir>/logs/plugins/<id>.log`
(LOG_MAX bytes, then one older file) and to an in-memory tail; every secret value the plugin was
given is replaced by `[secret NAME]` before either.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import threading
import time

from proxy import pluginrt
from proxy.pluginstore import PluginError

INIT_S = 20.0
CALL_S = 60.0
MAX_CRASHES = 5
CRASH_WINDOW_S = 3600.0
BACKOFF_S = (1, 2, 4, 8, 16, 32, 60)
UNAVAILABLE_S = 30.0
WATCH_S = 2.0
LOG_MAX = 1 << 20
TAIL_LINES = 1000
LINE_MAX = 4000
REFUSALS_MAX = 200
LEVELS = ("meta", "redacted", "full")
#: env names a secret may not take (they would change how the process runs)
RESERVED_ENV = re.compile(r"^(PATH|HOME|LANG|LC_[A-Z_]*|TZ|USER|SHELL|PWD|TMPDIR|LD_[A-Z_]*|"
                          r"PYTHON[A-Z_]*|HTTPS?_PROXY|NO_PROXY|TANDEM_[A-Z0-9_]*)$")
#: the bridge's methods and the capability each needs (None: the plugin's own, always)
BRIDGE = {"plugin.info": None, "plugin.status": None, "settings.read": "settings:read",
          "tools.call": None, "engine.control": "engine:control"}
PANEL_CSP = ("sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' "
             "'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
             "font-src 'self'; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; "
             "form-action 'none'")


class Refused(Exception):
    """The plugin may not start as it is (disabled, a changed tree, incompatible, a trust not
    granted, engine trust): no restart until the registry changes."""


class CapabilityRefused(Exception):
    def __init__(self, capability: str, message: str):
        super().__init__(message)
        self.capability, self.message = capability, message


def level_of(granted) -> str | None:
    """The highest prompts: level granted, or None."""
    top = -1
    for c in granted:
        if c.startswith("prompts:") and c[8:] in LEVELS:
            top = max(top, LEVELS.index(c[8:]))
    return LEVELS[top] if top >= 0 else None


def level_ok(want: str, granted) -> bool:
    have = level_of(granted)
    return have is not None and LEVELS.index(want) <= LEVELS.index(have)


def backend_id(pid: str, name: str) -> str:
    bid = f"plugin-{pid}-{name}".replace("_", "-")
    if len(bid) <= 32 and re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", bid):
        return bid
    return "plugin-" + hashlib.sha256(f"{pid}/{name}".encode()).hexdigest()[:16]


def network_hosts(granted) -> list[str]:
    """The `host` or `host:port` of each granted network: capability."""
    return sorted(c.split(":", 1)[1].lower() for c in granted if c.startswith("network:"))


# ===================================================================================== the log
class Log:
    """A plugin's log: an in-memory tail and a size-capped file, secrets replaced."""

    def __init__(self, path: str | None):
        self.path = path
        self.tail: collections.deque = collections.deque(maxlen=TAIL_LINES)
        self.lock = threading.Lock()
        self.secrets: dict[str, str] = {}
        self._part = b""

    def redact(self, text: str) -> str:
        for name, v in sorted(self.secrets.items(), key=lambda kv: -len(kv[1])):
            if v and len(v) >= 4:
                text = text.replace(v, f"[secret {name}]")
        return text

    def line(self, text: str) -> None:
        text = self.redact(text.rstrip("\n")[:LINE_MAX])
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        row = f"{stamp} {text}"
        with self.lock:
            self.tail.append(row)
            if self.path:
                try:
                    os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
                    if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_MAX:
                        os.replace(self.path, self.path + ".1")
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8", errors="replace") as f:
                        f.write(row + "\n")
                except OSError:
                    pass

    def stderr(self, chunk: bytes) -> None:
        data = self._part + chunk
        lines = data.split(b"\n")
        self._part = lines.pop()
        if len(self._part) > LINE_MAX:
            lines.append(self._part)
            self._part = b""
        for ln in lines:
            self.line("stderr: " + ln.decode("utf-8", "replace"))

    def last(self, n: int) -> list[str]:
        with self.lock:
            return list(self.tail)[-max(0, n):]


# ===================================================================================== the wire
def _session_class():
    from chatd import mcp

    class PluginSession(mcp.Session):
        """chatd's MCP session, with the plugin's own requests (tandem/*) answered here."""

        def __init__(self, transport, on_request, log=None):
            self.on_request = on_request
            super().__init__(transport, log=log)

        def _incoming(self, msg: dict) -> None:
            method = msg.get("method")
            if isinstance(method, str) and method.startswith("tandem/") and "id" in msg:
                try:
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "result": self.on_request(method, msg.get("params") or {})}
                except CapabilityRefused as exc:
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "error": {"code": -32001, "message": exc.message,
                                     "data": {"capability": exc.capability}}}
                except LookupError:
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "error": {"code": -32601, "message": f"no method {method}"}}
                try:
                    self.t.send(out)
                except OSError:
                    pass
                return
            super()._incoming(msg)
    return PluginSession, mcp.McpError


class Pipe:
    """The session's transport over a `pluginrt.Proc` (set once spawned)."""

    def __init__(self):
        self.on_message = None
        self.on_close = None
        self.proc = None
        self.wlock = threading.Lock()

    def line(self, ln: bytes | None) -> None:
        if ln is None:
            if self.on_close:
                self.on_close()
            return
        try:
            msg = json.loads(ln)
        except ValueError:
            return
        if isinstance(msg, dict) and self.on_message:
            self.on_message(msg)

    def send(self, msg: dict) -> None:
        if self.proc is None:
            raise OSError("the plugin is not running")
        with self.wlock:
            self.proc.send(json.dumps(msg, separators=(",", ":")).encode() + b"\n")

    def close(self) -> None:
        if self.proc is not None:
            self.proc.close()


# ===================================================================================== one plugin
class Plugin:
    def __init__(self, pid: str, log_path: str | None):
        self.id = pid
        self.state = "stopped"          # starting, running, backoff, stopped, unavailable,
                                        # refused, failed
        self.held = False
        self.version = None
        self.row_version = None
        self.crashes: collections.deque = collections.deque()
        self.restarts = 0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.next_at: float | None = None
        self.where: str | None = None
        self.runtime: str | None = None
        self.session = None
        self.pipe: Pipe | None = None
        self.info: dict = {}
        self.tools: list[dict] = []
        self.granted: set = set()
        self.notes: list[str] = []      # contributions refused at registration
        self.backends: list[str] = []
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.log = Log(log_path)
        self.refusals: collections.deque = collections.deque(maxlen=REFUSALS_MAX)

    @property
    def manifest(self) -> dict:
        return self.info.get("manifest") or {}

    def contributes(self, part: str) -> list:
        return list((self.manifest.get("contributes") or {}).get(part) or [])


# ===================================================================================== the host
class PluginHost:
    def __init__(self, store, *, runner=None, providers=None, cfg=None,
                 state_dir: str | None = None, log=None, env=None, library=None,
                 health_check=None, clock=time.monotonic):
        self.store = store
        self.runner = runner
        self.providers = providers
        self.cfg = cfg if cfg is not None else store.cfg
        self.state_dir = state_dir or os.path.dirname(os.path.abspath(self.cfg.path))
        self.log = log or (lambda m: None)
        self.env = dict(os.environ if env is None else env)
        self.library = library or (lambda: None)       # the controller's conductor's Library
        self.health_check = health_check                # fn(backend row): its models now
        self.clock = clock
        self.own_url: str | None = None                 # this controller, once it listens
        self.plugins: dict[str, Plugin] = {}
        self.gave_up: dict[str, dict] = {}
        self.lock = threading.RLock()
        self._glock = threading.Lock()          # the generation's own: a run thread bumps it
        self.gen = 0
        self.max_crashes = MAX_CRASHES
        self.backoff_s = BACKOFF_S
        self.unavailable_s = UNAVAILABLE_S
        self.init_s = INIT_S
        self._runtimes: dict = {"host": pluginrt.HostRuntime()}
        self._sandbox = None
        self._sandbox_cfg = None
        self._kick = threading.Event()
        self._stopping = threading.Event()
        self._watcher: threading.Thread | None = None
        store.listeners.append(lambda pid: self._kick.set())
        store.before_remove = self.forget
        if runner is not None:
            runner.contrib = self.hook_rows
            runner.plugin_call = self.hook_call
            runner.store.plugin_target = self.hook_target
        if providers is not None:
            providers.contrib = self.provider_rows
            providers.plugin_ask = self.decision_ask
            providers.plugin_up = self.running
            providers.store.plugin_target = self.provider_target

    # ------------------------------------------------------------------ runtimes
    def set_runtime(self, trust: str, runtime) -> None:
        """A test's (or a later package's) runtime for a trust level."""
        if trust == "sandboxed":
            self._sandbox, self._sandbox_cfg = runtime, {"backend": runtime.kind, "why": None}
        else:
            self._runtimes[trust] = runtime

    def sandbox(self):
        """(the sandboxed runtime or None, why not)."""
        if self._sandbox is None and self._sandbox_cfg is None:
            from proxy import appchat
            chat_env = {}
            path = os.path.join(self.state_dir, "chatd.env")
            if os.path.isfile(path):
                try:
                    chat_env = appchat.read_env_file(path)
                except (OSError, ValueError):
                    chat_env = {}
            cfg = pluginrt.sandbox_config(self.env, chat_env)
            self._sandbox_cfg = cfg
            if cfg["backend"] != "off":
                try:
                    self._sandbox = pluginrt.make_sandbox(
                        cfg, os.path.join(self.state_dir, "plugin-sandboxes"), env=self.env,
                        chat_env=chat_env, log=self.log)
                except Exception as exc:                           # noqa: BLE001
                    cfg["why"] = f"the {cfg['backend']} runtime: {type(exc).__name__}: {exc}"
                    self._sandbox = None
        return self._sandbox, (self._sandbox_cfg or {}).get("why")

    # ------------------------------------------------------------------ the watcher
    def start(self, watch: bool = True) -> "PluginHost":
        self.sync()
        if watch and self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, name="plugin-host", daemon=True)
            self._watcher.start()
        return self

    def _watch(self) -> None:
        while not self._stopping.is_set():
            self._kick.wait(WATCH_S)
            self._kick.clear()
            if self._stopping.is_set():
                return
            try:
                self.sync()
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[plugins] the host's look at the registry failed: "
                         f"{type(exc).__name__}")

    def close(self) -> None:
        self._stopping.set()
        self._kick.set()
        for pid in list(self.plugins):
            self._stop(pid)
        if self._sandbox is not None and hasattr(self._sandbox, "close"):
            self._sandbox.close()

    def sync(self) -> None:
        """Bring the processes in line with the registry (see the module's doc)."""
        if not self.store.ready():
            return
        rows = {p["id"]: p for p in self.store.list() if p["kind"] == "plugin"}
        with self.lock:
            for pid in list(self.plugins):
                row = rows.get(pid)
                if row is None or not row["enabled"]:
                    self._stop(pid, forget=True)
            for pid in list(self.gave_up):
                row = rows.get(pid)
                if row is None:
                    self.gave_up.pop(pid, None)
                elif row["enabled"] and row["row_version"] > self.gave_up[pid]["row_version"]:
                    self.gave_up.pop(pid, None)              # enabled again by someone
            for pid, row in rows.items():
                if not row["enabled"] or pid in self.gave_up:
                    continue
                p = self.plugins.get(pid)
                if p is None:
                    self._start(pid)
                elif p.held:
                    continue
                elif p.row_version is not None and p.row_version != row["row_version"]:
                    self.log(f"[plugins] {pid}: its registry row changed; restarting it")
                    self._stop(pid)
                    self._start(pid, keep=p)
                elif p.thread is None or not p.thread.is_alive():
                    if p.state in ("stopped",):
                        self._start(pid, keep=p)
        lib = self.library()
        if lib is not None:
            lib.extra_dirs = self.skill_dirs

    # ------------------------------------------------------------------ admin
    def start_plugin(self, pid: str) -> dict:
        row = self.store.need(pid)
        if not row["enabled"]:
            raise PluginError(409, "disabled", f"{pid} is disabled: enable it first")
        with self.lock:
            p = self.plugins.get(pid)
            self.gave_up.pop(pid, None)
            if p is not None:
                p.held = False
                if p.thread is not None and p.thread.is_alive():
                    return self.status(pid)
            self._start(pid, keep=p)
        return self.status(pid)

    def stop_plugin(self, pid: str) -> dict:
        self.store.need(pid)
        with self.lock:
            p = self.plugins.get(pid)
            if p is not None:
                p.held = True
                self._stop(pid)
        return self.status(pid)

    def restart_plugin(self, pid: str) -> dict:
        self.store.need(pid)
        with self.lock:
            p = self.plugins.get(pid)
            if p is not None:
                self._stop(pid)
                p.held = False
        return self.start_plugin(pid)

    def forget(self, pid: str) -> None:
        """Before a removal: the process ends and its contributions go."""
        with self.lock:
            self._stop(pid, forget=True)
            self.gave_up.pop(pid, None)

    # ------------------------------------------------------------------ start, stop
    def _start(self, pid: str, keep: Plugin | None = None) -> None:
        p = keep or Plugin(pid, os.path.join(self.state_dir, "logs", "plugins", f"{pid}.log"))
        p.stop_ev = threading.Event()
        p.state = "starting"
        self.plugins[pid] = p
        p.thread = threading.Thread(target=self._run, args=(p,), name=f"plugin-{pid}",
                                    daemon=True)
        p.thread.start()

    def _stop(self, pid: str, forget: bool = False) -> None:
        p = self.plugins.get(pid)
        if p is None:
            return
        p.stop_ev.set()
        self._end(p)
        t = p.thread
        if t is not None and t is not threading.current_thread():
            t.join(15)
        if p.state not in ("failed",):
            p.state = "stopped"
        if forget:
            self.plugins.pop(pid, None)

    def _end(self, p: Plugin) -> None:
        """Contributions off, the session and the process ended."""
        self._unregister(p)
        s, pipe = p.session, p.pipe
        p.session = None
        if s is not None:
            try:
                s.close()
            except Exception:                                      # noqa: BLE001
                pass
        elif pipe is not None:
            pipe.close()

    def _run(self, p: Plugin) -> None:
        while not p.stop_ev.is_set():
            try:
                spec, runtime = self._spec(p)
            except Refused as exc:
                p.state, p.last_error = "refused", str(exc)
                p.log.line(f"[host] not started: {exc}")
                self.log(f"[plugins] {p.id} not started: {exc}")
                return
            except pluginrt.Unavailable as exc:
                p.state, p.last_error = "unavailable", str(exc)
                p.log.line(f"[host] no sandbox: {exc}")
                p.next_at = time.time() + self.unavailable_s
                if p.stop_ev.wait(self.unavailable_s):
                    break
                continue
            except Exception as exc:                               # noqa: BLE001
                p.state, p.last_error = "refused", f"{type(exc).__name__}: {exc}"[:300]
                p.log.line(f"[host] not started: {p.last_error}")
                return
            ok = self._launch(p, spec, runtime)
            if ok:
                p.state, p.last_error = "running", None
                p.started_at = time.time()
                self._bump()
                while not p.stop_ev.is_set() and p.pipe is not None and \
                        p.pipe.proc is not None and p.pipe.proc.alive:
                    p.pipe.proc.ended.wait(0.5)
                code = p.pipe.proc.code() if p.pipe is not None and p.pipe.proc else None
                if not p.stop_ev.is_set():
                    p.last_error = f"it exited (code {code})"
                    p.log.line(f"[host] exited (code {code})")
            self._end(p)
            if p.stop_ev.is_set():
                break
            if self._crashed(p):
                return
        if p.state != "failed":
            p.state = "stopped"
        p.log.line("[host] stopped")

    def _crashed(self, p: Plugin) -> bool:
        """One failure counted; True when the host gave up on the plugin."""
        now = self.clock()
        p.crashes.append(now)
        while p.crashes and now - p.crashes[0] > CRASH_WINDOW_S:
            p.crashes.popleft()
        p.restarts += 1
        n = len(p.crashes)
        if n >= self.max_crashes:
            p.state = "failed"
            why = (f"failed {n} times within an hour ({p.last_error}); disabled until it is "
                   f"enabled again")
            p.log.line(f"[host] gave up: {why}")
            self.log(f"[plugins] {p.id}: {why}")
            try:
                row = self.store.set_enabled(p.id, False, who="plugin-host")
                rv = row["row_version"]
            except PluginError:
                rv = p.row_version or 0
            self.gave_up[p.id] = {"at": time.time(), "error": p.last_error, "failures": n,
                                  "row_version": rv, "version": p.version}
            return True
        delay = self.backoff_s[min(n - 1, len(self.backoff_s) - 1)]
        p.state, p.next_at = "backoff", time.time() + delay
        p.log.line(f"[host] starting again in {delay} s (failure {n} of {self.max_crashes} "
                   f"within an hour)")
        p.stop_ev.wait(delay)
        return False

    # ------------------------------------------------------------------ the spec
    def _spec(self, p: Plugin) -> tuple[dict, object]:
        try:
            info = self.store.load(p.id)
        except PluginError as exc:
            raise Refused(exc.message) from None
        m = info["manifest"]
        p.info, p.version, p.row_version = info, info["version"], info["row_version"]
        p.granted = set(info["granted"])
        trust = m.get("trust", "sandboxed")
        if trust == "engine":
            raise Refused("trust engine (a wheel inside the engine) is not available yet: it "
                          "comes with the in-process chain and signing")
        if m.get("contributes", {}).get("engine_hooks"):
            raise Refused("engine_hooks run inside the engine, which is not available yet")
        if trust == "host":
            if info.get("trust_granted") != "host":
                raise Refused("it asks to run as a process of the app's user (trust host), "
                              "which the admin has not granted (its settings: trust host)")
            runtime = self._runtimes["host"]
        else:
            runtime, why = self.sandbox()
            if runtime is None:
                raise pluginrt.Unavailable(why or "no sandbox runtime here")
            ok, why = runtime.available()
            if not ok:
                raise pluginrt.Unavailable(why or "the sandbox runtime is not available")
        tree = info["path"]
        mdir = info["manifest_dir"] or ""
        cwd = os.path.join(tree, *[x for x in mdir.split("/") if x])
        data = os.path.join(self.state_dir, "plugin-data", p.id)
        env = {k: str(v) for k, v in ((m.get("entry") or {}).get("env") or {}).items()}
        env.update({"LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "TANDEM_PLUGIN": p.id, "TANDEM_PLUGIN_VERSION": info["version"],
                    "TANDEM_PLUGIN_SETTINGS": json.dumps(info["settings"], sort_keys=True),
                    "TANDEM_PLUGIN_DATA": data,
                    "TANDEM_PLUGIN_CAPABILITIES": json.dumps(sorted(p.granted))})
        if runtime.kind == "host":
            env["PATH"] = self.env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
            env["HOME"] = data
        p.log.secrets = {}
        declared = set(info["secrets"])
        for c in sorted(p.granted):
            if not c.startswith("secrets:"):
                continue
            name = c.split(":", 1)[1]
            if name not in declared:
                continue                                           # granted, never set
            if RESERVED_ENV.match(name):
                p.log.line(f"[host] the secret {name} is not passed: its name is one of the "
                           f"process's own variables")
                continue
            try:
                val = self.store.secret_for(p.id, name)
            except Exception as exc:                               # noqa: BLE001
                p.log.line(f"[host] the secret {name} could not be read "
                           f"({type(exc).__name__})")
                continue
            if val is not None:
                env[name] = val
                p.log.secrets[name] = val
        spec = {"id": p.id, "argv": list((m.get("entry") or {}).get("command") or []),
                "cwd": cwd, "tree": tree, "workdir": "/".join(x for x in mdir.split("/") if x),
                "data": data, "env": env, "hosts": network_hosts(p.granted)}
        return spec, runtime

    def _launch(self, p: Plugin, spec: dict, runtime) -> bool:
        PluginSession, McpError = _session_class()
        pipe = Pipe()
        s = PluginSession(pipe, on_request=lambda method, params: self._request(p, method,
                                                                                params),
                          log=lambda m: p.log.line(f"[mcp] {m}"))
        p.pipe, p.session, p.runtime = pipe, s, runtime.kind
        try:
            pipe.proc = runtime.spawn(spec, pipe.line, p.log.stderr)
        except (pluginrt.StartFailed, pluginrt.Unavailable) as exc:
            p.last_error = str(exc)
            p.log.line(f"[host] {exc}")
            p.session = None
            return False
        p.where = pipe.proc.where
        p.log.line(f"[host] started {p.version} ({runtime.kind}, {p.where})")
        try:
            s.initialize(timeout=self.init_s)
            listed = s.list_tools(timeout=self.init_s) if p.contributes("tools") else []
        except (McpError, OSError) as exc:
            pipe.proc.ended.wait(1.0)
            code = pipe.proc.code()
            p.last_error = f"no MCP handshake ({exc})" + (
                f"; it exited (code {code})" if code is not None else "")
            p.log.line(f"[host] {p.last_error}")
            return False
        names = {t.get("name") for t in listed if isinstance(t, dict)}
        p.tools = [t for t in p.contributes("tools") if t["name"] in names]
        for t in p.contributes("tools"):
            if t["name"] not in names:
                p.notes.append(f"tool {t['name']}: declared but the server does not list it")
        self._register(p)
        return True

    # ------------------------------------------------------------------ the plugin's requests
    def _refuse(self, p: Plugin, capability: str, what: str) -> CapabilityRefused:
        p.refusals.append({"ts": int(time.time() * 1000), "capability": capability,
                           "what": what})
        p.log.line(f"[host] refused: {what} (needs {capability})")
        try:
            with self.cfg._lock:
                self.cfg._audit(f"plugin:{p.id}", "refused", "plugin", p.id, [capability],
                                detail={"what": what[:200]})
                self.cfg._conn.commit()
        except Exception:                                          # noqa: BLE001
            pass
        return CapabilityRefused(capability, f"refused: {what} needs the capability "
                                             f"{capability}, which was not granted")

    def _request(self, p: Plugin, method: str, params: dict):
        if method == "tandem/settings":
            if "settings:read" not in p.granted:
                raise self._refuse(p, "settings:read", "reading its settings")
            row = self.store.get(p.id)
            return {"settings": self.store.effective_settings(row) if row else {}}
        if method == "tandem/secret":
            name = params.get("name") if isinstance(params, dict) else None
            other = params.get("plugin") if isinstance(params, dict) else None
            if other not in (None, p.id):
                raise self._refuse(p, f"secrets:{other}/{name}", f"another plugin's secret "
                                                                 f"({other})")
            if not isinstance(name, str) or f"secrets:{name}" not in p.granted:
                raise self._refuse(p, f"secrets:{name}", f"the secret {name!r}")
            if name not in self.store.secret_names(self.store.need(p.id)):
                raise self._refuse(p, f"secrets:{name}", f"the secret {name!r}")
            return {"name": name, "value": self.store.secret_for(p.id, name)}
        if method == "tandem/engine":
            raise self._refuse(p, "engine:control", "engine control (not available yet)")
        raise LookupError(method)

    # ------------------------------------------------------------------ contributions
    def _bump(self) -> None:
        with self._glock:
            self.gen += 1

    def running(self, pid: str) -> bool:
        p = self.plugins.get(pid)
        return p is not None and p.state == "running" and p.session is not None

    def _live(self) -> list[Plugin]:
        return [p for p in list(self.plugins.values()) if p.state == "running" and
                p.session is not None]

    def _register(self, p: Plugin) -> None:
        p.notes = [n for n in p.notes if n.startswith("tool ")]
        lvl = level_of(p.granted)
        for h in p.contributes("hooks"):
            want = h.get("payload", "meta")
            if not level_ok(want, p.granted):
                p.notes.append(f"hook {h['name']}: payload {want} needs prompts:{want} "
                               f"granted (has {lvl or 'none'}); not registered")
                self._refuse(p, f"prompts:{want}", f"the hook {h['name']} at payload {want}")
        if p.contributes("decision_providers") and lvl is None:
            p.notes.append("decision providers: need a prompts: capability granted; not "
                           "registered")
            self._refuse(p, "prompts:meta", "its decision providers")
        if "engine:control" in p.granted:
            p.notes.append("engine:control is granted but not available yet (F3): every use "
                           "is refused")
        self._backends_on(p)
        self._bump()

    def _unregister(self, p: Plugin) -> None:
        was = p.state == "running"
        if p.state == "running":
            p.state = "stopping"
        self._backends_off(p)
        if was:
            self._bump()

    def hook_rows(self) -> tuple[int, list[dict]]:
        """The runner's contributed hooks (proxy/hooks.py `contrib`): (generation, rows)."""
        out = []
        for p in self._live():
            for h in p.contributes("hooks"):
                if not level_ok(h.get("payload", "meta"), p.granted):
                    continue
                hid = f"{p.id}/{h['name']}"
                out.append({"id": hid, "name": hid, "kind": "plugin", "target": hid,
                            "stages": list(h["stages"]), "role": h["role"], "priority": 100,
                            "mode": "on", "scope": {}, "policy": None, "timeout_ms": None,
                            "payload": h.get("payload", "meta"), "endpoint": None, "options": {},
                            "secret_ref": None, "grant": None, "enabled": True,
                            "created_at": 0, "updated_at": 0, "updated_by": f"plugin:{p.id}",
                            "version": p.row_version or 1, "plugin": p.id})
        return self.gen, out

    def hook_target(self, target) -> dict | None:
        """A hook the registry's row of kind plugin may name: the manifest's {stages, role,
        payload}, or None (no such plugin or hook)."""
        if not isinstance(target, str) or target.count("/") != 1:
            return None
        pid, name = target.split("/")
        row = self.store.get(pid) if self.store.ready() else None
        if row is None:
            return None
        for h in (row["manifest"].get("contributes") or {}).get("hooks") or []:
            if h.get("name") == name:
                return {"stages": list(h["stages"]), "role": h["role"],
                        "payload": h.get("payload", "meta")}
        return None

    def hook_call(self, h: dict, env: dict, timeout_s: float):
        """A hook of kind plugin, called from the runner's pool: the raw hooks-v1 result."""
        from proxy import hookcall
        pid, _, name = str(h.get("target") or "").partition("/")
        p = self.plugins.get(pid)
        if p is None or p.state != "running" or p.session is None:
            raise hookcall.CallError("error", f"the plugin {pid} is not running")
        declared = next((x for x in p.contributes("hooks") if x["name"] == name), None)
        if declared is None:
            raise hookcall.CallError("error", f"the plugin {pid} has no hook {name}")
        if not level_ok(h["payload"], p.granted):
            self._refuse(p, f"prompts:{h['payload']}", f"the hook {name} at payload "
                                                       f"{h['payload']}")
            raise hookcall.CallError("capability", f"payload {h['payload']} needs "
                                                   f"prompts:{h['payload']} granted")
        _, McpError = _session_class()
        params = dict(env)
        params["hook"] = name
        params["hook_id"] = h["id"]
        try:
            return p.session.request("hooks/call", params, timeout=max(0.05, timeout_s))
        except McpError as exc:
            raise hookcall.CallError("error", f"the plugin answered: {exc}") from None
        except OSError:
            raise hookcall.CallError("error", "the plugin's process is gone") from None

    def provider_rows(self) -> tuple[int, list[dict]]:
        out = []
        for p in self._live():
            if level_of(p.granted) is None:
                continue
            for d in p.contributes("decision_providers"):
                pid = f"{p.id}/{d['name']}"
                out.append({"id": pid, "name": pid, "kind": "plugin", "endpoint": None,
                            "hook_id": None, "target": pid, "model": "auto", "timeout_ms": 400,
                            "questions": {q: {} for q in d["questions"]}, "mode": "shadow",
                            "ab_percent": 0, "sticky": "session", "enabled": True,
                            "created_at": 0, "updated_at": 0, "updated_by": f"plugin:{p.id}",
                            "version": p.row_version or 1, "plugin": p.id})
        return self.gen, out

    def provider_target(self, target) -> dict | None:
        """{questions} of the provider a registry provider of kind plugin names, or None."""
        if not isinstance(target, str) or target.count("/") != 1:
            return None
        pid, name = target.split("/")
        row = self.store.get(pid) if self.store.ready() else None
        if row is None:
            return None
        for d in (row["manifest"].get("contributes") or {}).get("decision_providers") or []:
            if d.get("name") == name:
                return {"questions": list(d["questions"])}
        return None

    def decision_ask(self, target: str, qid: str, state: str, question: str, options: dict,
                     model: str, timeout_ms: float) -> dict:
        """decision/ask to a plugin's provider: {label, confidence, probs} or {error}."""
        pid, _, name = str(target or "").partition("/")
        p = self.plugins.get(pid)
        if p is None or p.state != "running" or p.session is None:
            return {"error": "plugin_down"}
        d = next((x for x in p.contributes("decision_providers") if x["name"] == name), None)
        if d is None or qid not in d["questions"]:
            return {"error": "no_provider"}
        lvl = level_of(p.granted)
        if lvl is None:
            self._refuse(p, "prompts:meta", f"the provider {name}")
            return {"error": "capability"}
        if lvl == "meta":
            inp = {"chars": len(state or "")}
        elif lvl == "redacted":
            from proxy import hookcall
            try:
                inp = {"text": hookcall.redact_value(state or "")}
            except Exception:                                      # noqa: BLE001
                return {"error": "redaction_failed"}
        else:
            inp = {"text": state or ""}
        params = {"provider": name, "question": qid, "instructions": question,
                  "options": dict(options), "model": model, "deadline_ms": int(timeout_ms),
                  "payload": lvl, "input": inp}
        _, McpError = _session_class()
        try:
            r = p.session.request("decision/ask", params, timeout=max(0.05, timeout_ms / 1000))
        except McpError as exc:
            return {"error": "timeout" if "no answer within" in str(exc) else "error",
                    "detail": str(exc)[:200]}
        except OSError:
            return {"error": "plugin_down"}
        if not isinstance(r, dict):
            return {"error": "bad_answer"}
        label = r.get("label", r.get("choice"))
        if not isinstance(label, str):
            return {"error": "bad_answer"}
        probs = r.get("probabilities") if isinstance(r.get("probabilities"), dict) else {}
        try:
            conf = float(r.get("confidence", probs.get(label, 1.0)))
            probs = {str(k): float(v) for k, v in probs.items()}
        except (TypeError, ValueError):
            return {"error": "bad_answer"}
        return {"label": label, "confidence": conf, "probs": probs or {label: conf}}

    # ------------------------------------------------------------------ tools (chatd)
    def tools_view(self) -> list[dict]:
        out = []
        for p in sorted(self._live(), key=lambda x: x.id):
            if not p.tools:
                continue
            out.append({"id": p.id, "version": p.version,
                        "title": p.manifest.get("title") or p.id,
                        "tools": [{k: t[k] for k in ("name", "title", "description",
                                                     "inputSchema", "annotations")
                                   if k in t} for t in p.tools]})
        return out

    def tool_call(self, pid: str, tool: str, args, timeout_s: float = CALL_S) -> dict:
        p = self.plugins.get(pid)
        if p is None or p.state != "running" or p.session is None:
            raise PluginError(503, "plugin_down", f"the plugin {pid} is not running")
        t = next((x for x in p.tools if x["name"] == tool), None)
        if t is None:
            raise PluginError(404, "not_found", f"the plugin {pid} offers no tool {tool!r}")
        why = _arguments_refused(args, t["inputSchema"])
        if why:
            raise PluginError(400, "bad_arguments", f"{tool}: {why}")
        _, McpError = _session_class()
        try:
            return p.session.call(tool, args, timeout=timeout_s)
        except McpError as exc:
            raise PluginError(502, "plugin_error", f"the plugin answered: {exc}") from None
        except OSError:
            raise PluginError(503, "plugin_down", f"the plugin {pid} is gone") from None

    # ------------------------------------------------------------------ backends
    def _backends_on(self, p: Plugin) -> None:
        p.backends = []
        if not p.contributes("backends"):
            return
        if not self.own_url:
            p.notes.append("backends: this controller's own address is not known yet; not "
                           "registered")
            return
        title = p.manifest.get("title") or p.id
        for b in p.contributes("backends"):
            bid = backend_id(p.id, b["name"])
            doc = {"id": bid, "kind": "generic",
                   "label": f"{title}: {b['name']} (plugin {p.id})"[:80],
                   "url": f"{self.own_url}/plugin-backends/{p.id}/{b['name']}",
                   "models_mode": "list", "models": list(b.get("models") or []),
                   "enabled": True}
            try:
                if self.cfg.backend(bid) is not None:
                    self.cfg.delete_backend(bid, who=f"plugin:{p.id}")
                row = self.cfg.create_backend(doc, who=f"plugin:{p.id}")
                p.backends.append(bid)
            except Exception as exc:                               # noqa: BLE001
                p.notes.append(f"backend {b['name']}: not registered ({exc})")
                continue
            if self.health_check is not None:
                threading.Thread(target=self._check_backend, args=(row,), daemon=True).start()

    def _check_backend(self, row: dict) -> None:
        try:
            self.health_check(row)
        except Exception:                                          # noqa: BLE001
            pass

    def _backends_off(self, p: Plugin) -> None:
        for bid in list(p.backends):
            try:
                if self.cfg.backend(bid) is not None:
                    self.cfg.delete_backend(bid, who=f"plugin:{p.id}")
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[plugins] {p.id}: its backend {bid} was not removed: "
                         f"{type(exc).__name__}")
        p.backends = []

    def backend_of(self, pid: str, name: str) -> dict | None:
        p = self.plugins.get(pid)
        if p is None:
            return None
        return next((b for b in p.contributes("backends") if b["name"] == name), None)

    def backend_request(self, pid: str, name: str, method: str, path: str, body,
                        timeout_s: float = 600.0) -> dict:
        p = self.plugins.get(pid)
        if p is None or p.state != "running" or p.session is None:
            raise PluginError(503, "plugin_down", f"the plugin {pid} is not running")
        if self.backend_of(pid, name) is None:
            raise PluginError(404, "not_found", f"the plugin {pid} has no backend {name!r}")
        _, McpError = _session_class()
        try:
            r = p.session.request("backend/request", {"backend": name, "method": method,
                                                      "path": path, "body": body},
                                  timeout=timeout_s)
        except McpError as exc:
            raise PluginError(502, "plugin_error", f"the plugin answered: {exc}") from None
        except OSError:
            raise PluginError(503, "plugin_down", f"the plugin {pid} is gone") from None
        if not isinstance(r, dict) or not isinstance(r.get("status"), int):
            raise PluginError(502, "plugin_error", "backend/request answers {status, body} or "
                                                   "{status, events}")
        return r

    # ------------------------------------------------------------------ skills
    def skill_dirs(self) -> list[str]:
        out = []
        for p in sorted(self._live(), key=lambda x: x.id):
            tree = p.info.get("path")
            mdir = p.info.get("manifest_dir") or ""
            for d in p.contributes("skills"):
                parts = [x for x in (mdir + "/" + d).split("/") if x not in ("", ".")]
                full = os.path.realpath(os.path.join(tree, *parts))
                if full.startswith(os.path.realpath(tree) + os.sep) and os.path.isdir(full):
                    out.append(full)
        return out

    # ------------------------------------------------------------------ panels
    def panel_file(self, pid: str, name: str, rel: str) -> str | None:
        """The file of a running plugin's panel: its HTML at `rel` empty, else a file beside it
        (in its folder or below); None when there is none."""
        p = self.plugins.get(pid)
        if p is None or p.state != "running":
            return None
        panel = next((x for x in p.contributes("panels") if x["name"] == name), None)
        if panel is None:
            return None
        tree = os.path.realpath(p.info["path"])
        mdir = [x for x in (p.info.get("manifest_dir") or "").split("/") if x]
        page = os.path.realpath(os.path.join(tree, *mdir, *[x for x in panel["path"].split("/")
                                                             if x not in ("", ".")]))
        if not rel:
            return page if page.startswith(tree + os.sep) and os.path.isfile(page) else None
        base = os.path.dirname(page)
        parts = [x for x in rel.split("/") if x not in ("", ".")]
        if any(x == ".." or x.startswith(".") for x in parts):
            return None
        full = os.path.realpath(os.path.join(base, *parts))
        if not full.startswith(base + os.sep) or not full.startswith(tree + os.sep):
            return None
        return full if os.path.isfile(full) else None

    def panels(self, pid: str) -> list[dict]:
        p = self.plugins.get(pid)
        if p is None or p.state != "running":
            return []
        return [{"name": x["name"], "title": x["title"],
                 "url": f"/plugin-panels/{pid}/{x['name']}/", "sandbox": "allow-scripts "
                 "allow-forms", "csp": PANEL_CSP,
                 "bridge": {"message": "postMessage({type: 'tandem-bridge', id, method, "
                                       "params}) to the parent; the answer is {type: "
                                       "'tandem-bridge-result', id, result | error}",
                            "methods": self.bridge_methods(pid)}}
                for x in p.contributes("panels")]

    def bridge_methods(self, pid: str) -> list[str]:
        p = self.plugins.get(pid)
        if p is None:
            return []
        out = []
        for m, cap in BRIDGE.items():
            if m == "engine.control":
                continue                        # not available yet, whatever is granted
            if cap is None or cap in p.granted:
                out.append(m)
        return out

    def bridge(self, pid: str, method, params) -> dict:
        """One call of a panel's bridge, as the page relays it: only what the plugin's
        capabilities allow; anything else refused and journaled."""
        p = self.plugins.get(pid)
        if p is None or p.state != "running":
            raise PluginError(503, "plugin_down", f"the plugin {pid} is not running")
        if method not in BRIDGE:
            raise PluginError(403, "refused", f"the bridge has no method {method!r}")
        cap = BRIDGE[method]
        if method == "engine.control":
            self._refuse(p, "engine:control", "engine control from its panel (not available "
                                              "yet)")
            raise PluginError(403, "refused", "engine control is not available to plugins yet")
        if cap is not None and cap not in p.granted:
            self._refuse(p, cap, f"the bridge's {method}")
            raise PluginError(403, "refused", f"{method} needs {cap}, which was not granted")
        params = params if isinstance(params, dict) else {}
        if method == "plugin.info":
            return {"id": pid, "version": p.version, "title": p.manifest.get("title"),
                    "capabilities": sorted(p.granted)}
        if method == "plugin.status":
            st = self.status(pid)
            return {k: st[k] for k in ("state", "restarts", "tools", "hooks", "providers")}
        if method == "settings.read":
            row = self.store.need(pid)
            return {"settings": self.store.effective_settings(row)}
        # tools.call: its own tools that change nothing
        tool = params.get("tool")
        t = next((x for x in p.tools if x["name"] == tool), None)
        if t is None:
            raise PluginError(404, "not_found", f"no tool {tool!r} of {pid}")
        if (t.get("annotations") or {}).get("readOnlyHint") is not True:
            self._refuse(p, "tools:write", f"the tool {tool} (not read-only) from its panel")
            raise PluginError(403, "refused", f"a panel may call only read-only tools; {tool} "
                                              f"is not")
        return self.tool_call(pid, tool, params.get("arguments") or {}, timeout_s=30)

    # ------------------------------------------------------------------ views
    def status(self, pid: str) -> dict:
        p = self.plugins.get(pid)
        row = self.store.get(pid)
        g = self.gave_up.get(pid)
        if p is None:
            st = "failed" if g else ("stopped" if row and row["enabled"] else "disabled")
            out = {"id": pid, "state": st, "held": False, "runtime": None, "where": None,
                   "version": row["version"] if row else None, "started_at": None,
                   "restarts": 0, "failures_last_hour": 0, "next_start_at": None,
                   "last_error": g["error"] if g else None, "tools": [], "hooks": [],
                   "providers": [], "backends": [], "skills": [], "panels": [], "notes": [],
                   "refusals": []}
        else:
            run = p.state == "running"
            out = {"id": pid, "state": p.state, "held": p.held, "runtime": p.runtime,
                   "where": p.where, "version": p.version,
                   "started_at": int(p.started_at * 1000) if p.started_at and run else None,
                   "restarts": p.restarts, "failures_last_hour": len(p.crashes),
                   "next_start_at": int(p.next_at * 1000) if p.next_at and
                   p.state in ("backoff", "unavailable") else None,
                   "last_error": p.last_error,
                   "tools": [t["name"] for t in p.tools] if run else [],
                   "hooks": [h["id"] for h in self.hook_rows()[1] if h["plugin"] == pid],
                   "providers": [x["id"] for x in self.provider_rows()[1] if x["plugin"] == pid],
                   "backends": list(p.backends) if run else [],
                   "skills": [os.path.relpath(d, os.path.realpath(p.info["path"]))
                              for d in self.skill_dirs()
                              if d.startswith(os.path.realpath(p.info.get("path") or "") +
                                              os.sep)],
                   "panels": [x["name"] for x in p.contributes("panels")] if run else [],
                   "notes": list(p.notes), "refusals": list(p.refusals)[-20:]}
        tg = (row or {}).get("trust_granted") or "sandboxed"
        out["trust"] = {"requested": (row or {}).get("trust") or "sandboxed", "granted": tg}
        out["gave_up"] = dict(g) if g else None
        return out

    def statuses(self) -> list[dict]:
        rows = self.store.list() if self.store.ready() else []
        return [self.status(r["id"]) for r in rows if r["kind"] == "plugin"]

    def logs(self, pid: str, n: int = 200) -> list[str]:
        p = self.plugins.get(pid)
        if p is not None:
            return p.log.last(n)
        path = os.path.join(self.state_dir, "logs", "plugins", f"{pid}.log")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return [ln.rstrip("\n") for ln in f.readlines()[-max(0, n):]]
        except OSError:
            return []

    def problems(self) -> list[dict]:
        """The health page's findings (proxy/fleethealth.py): {code, plugin, detail}."""
        out = []
        for pid, g in sorted(self.gave_up.items()):
            out.append({"code": "plugin_failed", "plugin": pid, "detail": g.get("error")})
        for p in sorted(self.plugins.values(), key=lambda x: x.id):
            if p.state == "refused":
                out.append({"code": "plugin_failed", "plugin": p.id, "detail": p.last_error})
            elif p.state == "unavailable":
                out.append({"code": "plugin_no_sandbox", "plugin": p.id,
                            "detail": p.last_error})
        return out


def _arguments_refused(args, schema: dict) -> str | None:
    """Why a tool's arguments do not fit its (strict) input schema's top level, or None."""
    if not isinstance(args, dict):
        return "the arguments are a JSON object"
    props = schema.get("properties") or {}
    extra = sorted(set(args) - set(props))
    if extra:
        return f"no argument {extra[0]!r}"
    missing = [k for k in schema.get("required") or [] if k not in args]
    if missing:
        return f"{missing[0]!r} is required"
    return None


__all__ = ["PluginHost", "Refused", "CapabilityRefused", "level_of", "level_ok", "backend_id",
           "network_hosts"]
