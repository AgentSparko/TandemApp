"""Where a plugin's process runs (SRV-859): the plugin host's runtimes (proxy/pluginhost.py).

    host         trust `host`, granted by the admin: a process as the app's user, started from the
                 manifest's folder with an environment of its own (never the controller's)
    docker       trust `sandboxed` on a container engine (Docker's API on a socket, which Podman
                 also serves): one container a plugin, made like a chat's sandbox (chatd/sandbox.py)
    kubernetes   trust `sandboxed` in a cluster: one pod a plugin, made like a chat's sandbox pod
                 (chatd/k8s.py), its process run through pods/exec

THE SANDBOX is the chat sandbox's class: the sandbox image, uid 1000, every capability dropped,
no-new-privileges, a read-only root, an in-memory /tmp, no devices, `--network none` (a pod: the
chart's NetworkPolicy), a memory limit (TANDEM_PLUGIN_MEM_MB, 512 by default), one CPU and 128
processes. Mounts: the plugin's installed tree at /plugin, read-only; its own data folder at
/workspace and a home at /home/agent (both under `<state dir>/plugin-sandboxes/`, nothing else of
the controller's); /run/tandem holds the egress proxy's socket. The way out is chatd/egress.py's
proxy with an `Allow`: only the hosts of the plugin's granted `network:` capabilities, so no LAN
address it was not granted, never this box, the controller or the Kubernetes API.

The container's main process is the image's relay (`relay.py serve`, HTTP_PROXY's end inside it);
the plugin's entry runs in it as an exec with stdin attached, from /plugin/<manifest folder>: its
stdout is the MCP stream, its stderr the plugin's log. Stopping a plugin removes its container (or
pod): a restart starts from the image again, with its /workspace kept.

WHICH RUNTIME. The one chatd uses on this install: `QSE_SANDBOX_BACKEND` (kubernetes or docker),
`QSE_SANDBOX_SOCKET` and `QSE_SANDBOX_IMAGE` read from the controller's environment, then from
chatd's env file (`<state dir>/chatd.env`, proxy/appchat.py). `TANDEM_PLUGIN_SANDBOX` overrides it:
auto (the default), docker, kubernetes or off. With none (no socket, `off`) a sandboxed plugin does
not start and says why (`Unavailable`; the health page's plugin_no_sandbox).

The egress proxy's socket is `<state dir>/plugin-sandboxes/egress/<plugin>/proxy.sock`; where that
path is longer than a unix socket's address allows, it is in a folder of this user's own instead
(`$XDG_RUNTIME_DIR/tandem-plugins`, else `<tmp>/tandem-plugins-<uid>`, made 0700 and refused when
another user owns it or it is a link).

A pod's environment is not put on its exec's command line (the API server may log a command): the
host writes it to a 0600 file in the plugin's home, and a small launcher in the pod reads it,
removes it and execs the entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading

LABEL = "tandem.plugin"
INSTANCE_LABEL = "tandem.plugin.instance"
DEFAULT_IMAGE = "tandem-sandbox:1"
DEFAULT_SOCKET = "/var/run/docker.sock"
MEM_MB = 512
CPUS = 1
PIDS = 128
EGRESS_PORT = 3129
STOP_GRACE_S = 3.0
ENV_FILE = ".tandem-plugin-env.json"
SOCK_MAX = 100                    # a unix socket's path, with room under sun_path's 108 bytes
#: the pod's launcher: argv[1] the env file, the rest the entry; the file is read and removed
LAUNCH = ("import json, os, sys\n"
          "p = sys.argv[1]\n"
          "with open(p) as f:\n"
          "    e = json.load(f)\n"
          "os.remove(p)\n"
          "env = dict(os.environ)\n"
          "env.update(e)\n"
          "os.execvpe(sys.argv[2], sys.argv[2:], env)\n")


class Unavailable(Exception):
    """No runtime for this plugin here, and why (the health page says it): not the plugin's
    fault, so not counted as a crash; the host tries again later."""


class StartFailed(Exception):
    """The plugin's own entry did not start (no such program, an exec refused): a crash."""


class Proc:
    """One running plugin process: `send(bytes)` writes its stdin, `close()` ends it (and its
    container), `alive`, `code` (its exit code once known), `where` (pid or container)."""

    def __init__(self, send, close, where: str, code=None):
        self._send, self._close, self.where = send, close, where
        self._code = code or (lambda: None)
        self.ended = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self.ended.is_set()

    def code(self):
        return self._code()

    def send(self, data: bytes) -> None:
        self._send(data)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close()


def short_dir(root: str) -> str:
    """A folder of this user's own for the egress sockets of the sandboxes under `root`, short
    enough for a socket's address (THE MODULE'S DOC)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    base = os.path.join(base, "tandem-plugins") if base and os.path.isdir(base) else \
        os.path.join(tempfile.gettempdir(), f"tandem-plugins-{os.getuid()}")
    for d in (base, os.path.join(base, hashlib.sha256(os.path.realpath(root).encode())
                                 .hexdigest()[:12])):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f"{d} is not this user's own folder")
    return d


def _lines(on_line):
    """A feeder: bytes in, one call per complete line out (empty lines dropped)."""
    buf = {"b": b""}

    def feed(chunk: bytes) -> None:
        buf["b"] += chunk
        while b"\n" in buf["b"]:
            ln, buf["b"] = buf["b"].split(b"\n", 1)
            if ln.strip():
                on_line(ln)
    return feed


# ===================================================================================== host
class HostRuntime:
    """Trust `host`: a plain subprocess as the app's user, in a session of its own (its group is
    ended on stop), from the manifest's folder, with `spec["env"]` and nothing else."""
    kind = "host"

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def spawn(self, spec: dict, on_line, on_err) -> Proc:
        cwd = spec["cwd"]
        argv = list(spec["argv"])
        os.makedirs(spec["data"], mode=0o700, exist_ok=True)
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=dict(spec["env"]), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, close_fds=True)
        except OSError as exc:
            raise StartFailed(f"the entry did not start: {type(exc).__name__}: "
                              f"{exc.strerror or exc}") from None
        proc = None

        def out():
            feed = _lines(on_line)
            try:
                while True:
                    d = p.stdout.read1(65536)
                    if not d:
                        break
                    feed(d)
            except (OSError, ValueError):
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            proc.ended.set()
            on_line(None)

        def err():
            try:
                while True:
                    d = p.stderr.read1(65536)
                    if not d:
                        break
                    on_err(d)
            except (OSError, ValueError):
                pass

        def send(data: bytes) -> None:
            p.stdin.write(data)
            p.stdin.flush()

        def close() -> None:
            if p.poll() is None:
                for sig, wait in ((signal.SIGTERM, STOP_GRACE_S), (signal.SIGKILL, 5.0)):
                    try:
                        os.killpg(p.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        p.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            for f in (p.stdin, p.stdout, p.stderr):
                try:
                    f.close()
                except OSError:
                    pass

        proc = Proc(send, close, f"pid {p.pid}", code=p.poll)
        threading.Thread(target=out, name=f"plugin-{spec['id']}-out", daemon=True).start()
        threading.Thread(target=err, name=f"plugin-{spec['id']}-err", daemon=True).start()
        return proc


# ===================================================================================== sandboxes
def _boxes_mixin():
    """The plugin's box over chatd's sandbox class: its name, folders, the tree mount, no memory
    admission of chat's (a plugin's container has its own limit), and the egress `Allow`."""
    from chatd.sandbox import Unavailable as SbxUnavailable

    class Mixin:
        def _plugin_init(self, root: str, mem_bytes: int):
            self.trees: dict[str, str] = {}          # plugin id -> its installed tree
            self.policies: dict = {}                 # plugin id -> egress.Allow
            self.mem_bytes = mem_bytes
            self.network_mode = lambda cid: self.policies.get(cid) or "off"

        def paths(self, cid: str) -> tuple[str, str, str]:
            eg = os.path.join(self.data, "egress", cid)
            if len(os.path.join(eg, "proxy.sock")) > SOCK_MAX:
                eg = os.path.join(short_dir(self.data), cid)
            return (os.path.join(self.data, "data", cid), os.path.join(self.data, "home", cid),
                    eg)

        def admit(self):
            return None

        def why(self) -> str:
            return "the sandbox runtime is not available"

        def ensure(self, cid: str, cancel=None, on_wait=None) -> str:
            if not self.available():
                raise SbxUnavailable(self.why())
            b = self.box(cid)
            with b.lock:
                b.last_used = self.clock()
                if b.state == "running" and b.container and self._alive(b):
                    return b.container
                b.state, b.error = "creating", None
                try:
                    b.container = self._start(cid)
                    b.state, b.stopped_reason = "running", None
                    b.started = b.last_used = self.clock()
                    self._proxy(b)
                except (SbxUnavailable, OSError) as exc:
                    b.state, b.error = "error", str(exc)
                    try:
                        self.remove(cid)
                    except Exception:                              # noqa: BLE001
                        pass
                    raise SbxUnavailable(str(exc)) from None
                return b.container
    return Mixin, SbxUnavailable


def _docker_boxes():
    from chatd import sandbox as sbx
    Mixin, SbxUnavailable = _boxes_mixin()

    class PluginBoxes(Mixin, sbx.Sandboxes):
        def __init__(self, root: str, *, engine, image: str, mem_bytes: int, log=None):
            super().__init__(root, engine=engine, image=image, log=log, host="app")
            self._plugin_init(root, mem_bytes)

        def name(self, cid: str) -> str:
            return "tandem-plugin-" + cid.lower()

        def owns(self, labels, cid: str) -> bool:
            return (labels or {}).get(INSTANCE_LABEL) == self.instance

        def why(self) -> str:
            return (f"the sandbox image {self.image} is not in the container runtime at "
                    f"{self.engine.path}, or it does not answer")

        def _start(self, cid: str) -> str:
            data, home, eg = self.paths(cid)
            for d in (data, home, eg):
                os.makedirs(d, exist_ok=True)
            tree = self.trees[cid]
            name = self.name(cid)
            st, info = self.engine.call("GET", f"/containers/{name}/json")
            if st == 200 and isinstance(info, dict):
                labels = (info.get("Config") or {}).get("Labels") or {}
                if labels.get(INSTANCE_LABEL) != self.instance:
                    raise SbxUnavailable(f"a container of another TandemApp has this plugin's "
                                         f"name ({name})")
                self.engine.call("DELETE", f"/containers/{info['Id']}?force=1", timeout=30)
            elif st != 404:
                raise SbxUnavailable(f"the container engine answered {st}")
            proxy = "http://127.0.0.1:3128"
            body = {
                "Image": self.image, "User": "1000:1000", "WorkingDir": "/workspace",
                "Cmd": ["python3", "/opt/tandem/relay.py", "serve"],
                "Env": ["HOME=/home/agent", "LANG=C.UTF-8", "PYTHONUNBUFFERED=1",
                        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        f"HTTP_PROXY={proxy}", f"HTTPS_PROXY={proxy}", f"http_proxy={proxy}",
                        f"https_proxy={proxy}", "NO_PROXY=localhost,127.0.0.1",
                        "no_proxy=localhost,127.0.0.1"],
                "Labels": {LABEL: cid, INSTANCE_LABEL: self.instance},
                "HostConfig": {
                    "Binds": [f"{tree}:/plugin:ro", f"{data}:/workspace:rw",
                              f"{home}:/home/agent:rw", f"{eg}:/run/tandem:ro"],
                    "NetworkMode": "none", "Memory": self.mem_bytes,
                    "MemorySwap": self.mem_bytes, "NanoCpus": CPUS * 10 ** 9,
                    "PidsLimit": PIDS, "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"], "ReadonlyRootfs": True,
                    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=64m"}, "Init": True,
                    "Devices": [], "Privileged": False},
            }
            st, info = self.engine.call("POST", f"/containers/create?name={name}", body)
            if st not in (200, 201):
                msg = info.get("message") if isinstance(info, dict) else info
                raise SbxUnavailable(f"the plugin's container could not be created ({st}: "
                                     f"{str(msg)[:200]})")
            container = info["Id"]
            st, _ = self.engine.call("POST", f"/containers/{container}/start")
            if st not in (204, 304):
                self.engine.call("DELETE", f"/containers/{container}?force=1", timeout=30)
                raise SbxUnavailable(f"the plugin's container did not start ({st})")
            return container
    return PluginBoxes


def _kube_pods():
    from chatd import k8s
    Mixin, SbxUnavailable = _boxes_mixin()

    class PluginPods(Mixin, k8s.KubeSandboxes):
        def __init__(self, root: str, *, api=None, env=None, image: str, mem_bytes: int,
                     egress_port: int, log=None):
            env = dict(os.environ if env is None else env)
            env["QSE_K8S_EGRESS_PORT"] = str(egress_port)
            super().__init__(root, api=api, env=env, image=image, log=log, host="app")
            self._plugin_init(root, mem_bytes)
            self.limit_bytes = mem_bytes
            self.cpus = float(CPUS)

        def name(self, cid: str) -> str:
            n = re.sub(r"[^a-z0-9-]", "-", cid.lower()).strip("-")
            return ("tandem-plugin-" + n)[:63].rstrip("-")

        def why(self) -> str:
            return self.unavailable_words()

        def pod_spec(self, name: str, cid: str | None = None, probe: bool = False) -> dict:
            spec = super().pod_spec(name, cid, probe)
            if cid is not None:
                spec["metadata"]["labels"][LABEL] = k8s._label_value(cid)
                c = spec["spec"]["containers"][0]
                if self.claim and cid in self.trees:
                    c["volumeMounts"].append({"name": "data", "mountPath": "/plugin",
                                              "subPath": self._rel(self.trees[cid]),
                                              "readOnly": True})
            return spec
    return PluginPods


# ===================================================================================== a box run
class BoxRuntime:
    """Trust `sandboxed`: the plugin in its own container or pod (`boxes`: PluginBoxes or
    PluginPods), its entry an exec with stdin, the egress `Allow` its granted hosts."""

    def __init__(self, kind: str, boxes, log=None):
        self.kind = kind
        self.boxes = boxes
        self.log = log or (lambda m: None)

    def available(self) -> tuple[bool, str | None]:
        try:
            ok = self.boxes.available()
        except Exception as exc:                                   # noqa: BLE001
            return False, f"{type(exc).__name__}"
        return (True, None) if ok else (False, self.boxes.why())

    def spawn(self, spec: dict, on_line, on_err) -> Proc:
        from chatd.egress import Allow
        from chatd.sandbox import Unavailable as SbxUnavailable
        from chatd.sandbox import demux
        pid = spec["id"]
        self.boxes.trees[pid] = spec["tree"]
        self.boxes.policies[pid] = Allow(spec.get("hosts") or ())
        try:
            self.boxes.ensure(pid)
        except SbxUnavailable as exc:
            if not self.boxes.available():
                raise Unavailable(str(exc)) from None
            raise StartFailed(f"its sandbox did not start: {exc}") from None
        except OSError as exc:
            raise StartFailed(f"its sandbox did not start: {exc}") from None
        workdir = "/plugin" + ("/" + spec["workdir"] if spec.get("workdir") else "")
        env = dict(spec["env"])
        env.update(HOME="/home/agent", TANDEM_PLUGIN_DATA="/workspace")
        argv = list(spec["argv"])
        try:
            if self.kind == "kubernetes":
                _, home, _ = self.boxes.paths(pid)
                path = os.path.join(home, ENV_FILE)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(env, f)
                cmd = ["python3", "-c", LAUNCH, "/home/agent/" + ENV_FILE, *argv]
                exec_id, sock, first = self.boxes.open_exec(pid, cmd, stdin=True,
                                                            workdir=workdir)
            else:
                exec_id, sock, first = self.boxes.open_exec(
                    pid, argv, stdin=True, workdir=workdir,
                    env=[f"{k}={v}" for k, v in env.items()])
        except (SbxUnavailable, OSError) as exc:
            self.boxes.remove(pid)
            raise StartFailed(f"the entry did not start in the sandbox: {exc}") from None
        proc = None

        def reader():
            feed = _lines(on_line)

            def data(stream, chunk):
                if stream == 1:
                    feed(chunk)
                elif stream == 2:
                    on_err(chunk)
            try:
                demux(sock, first, data)
            except OSError:
                pass
            finally:
                proc.ended.set()
                on_line(None)

        def send(b: bytes) -> None:
            sock.sendall(b)

        def close() -> None:
            try:
                self.boxes.remove(pid)
            except Exception as exc:                               # noqa: BLE001
                self.log(f"[plugins] {pid}: its sandbox was not removed: {type(exc).__name__}")
            try:
                sock.close()
            except OSError:
                pass

        def code():
            try:
                running, c, _ = self.boxes.exit_code(exec_id)
            except Exception:                                      # noqa: BLE001
                return None
            return None if running else c

        proc = Proc(send, close, self.boxes.name(pid), code=code)
        threading.Thread(target=reader, name=f"plugin-{pid}-io", daemon=True).start()
        return proc

    def close(self) -> None:
        try:
            self.boxes.stopping.set()
            eg = getattr(self.boxes, "egress", None)
            if eg is not None:
                eg.close()
        except Exception:                                          # noqa: BLE001
            pass


# ===================================================================================== the choice
def sandbox_config(env: dict, chat_env: dict | None = None) -> dict:
    """{backend: docker|kubernetes|off, socket, image, why}: the sandbox runtime chatd uses on
    this install, `TANDEM_PLUGIN_SANDBOX` over it (THE MODULE'S DOC)."""
    chat_env = chat_env or {}

    def get(k):
        return (env.get(k) or chat_env.get(k) or "").strip()
    want = (env.get("TANDEM_PLUGIN_SANDBOX") or "auto").strip().lower()
    image = get("QSE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    if want == "off":
        return {"backend": "off", "socket": None, "image": image,
                "why": "TANDEM_PLUGIN_SANDBOX is off"}
    backend = want if want in ("docker", "kubernetes") else \
        ("kubernetes" if get("QSE_SANDBOX_BACKEND").lower() == "kubernetes" else "docker")
    sock = get("QSE_SANDBOX_SOCKET") or DEFAULT_SOCKET
    if backend == "docker" and not os.path.exists(sock):
        return {"backend": "off", "socket": sock, "image": image,
                "why": f"no container runtime socket at {sock}"}
    return {"backend": backend, "socket": sock if backend == "docker" else None,
            "image": image, "why": None}


def make_sandbox(cfg: dict, root: str, *, env: dict | None = None, chat_env: dict | None = None,
                 log=None, api=None, engine=None):
    """The `BoxRuntime` for `sandbox_config`'s answer, or None (off)."""
    env = dict(os.environ if env is None else env)
    merged = dict(chat_env or {})
    merged.update({k: v for k, v in env.items() if v})
    mem = int(float(env.get("TANDEM_PLUGIN_MEM_MB") or MEM_MB) * 1024 * 1024)
    os.makedirs(root, mode=0o700, exist_ok=True)
    if cfg["backend"] == "docker":
        from chatd import sandbox as sbx
        boxes = _docker_boxes()(root, engine=engine or sbx.Engine(cfg["socket"]),
                                image=cfg["image"], mem_bytes=mem, log=log)
        return BoxRuntime("docker", boxes, log)
    if cfg["backend"] == "kubernetes":
        port = int(env.get("TANDEM_PLUGIN_EGRESS_PORT") or merged.get(
            "TANDEM_PLUGIN_EGRESS_PORT") or EGRESS_PORT)
        pods = _kube_pods()(root, api=api, env=merged, image=cfg["image"], mem_bytes=mem,
                            egress_port=port, log=log)
        return BoxRuntime("kubernetes", pods, log)
    return None

