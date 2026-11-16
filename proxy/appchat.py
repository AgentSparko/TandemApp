"""chatd owned by the controller (SRV-841): the Chat tab's service on the controller's own host.

    chat = AppChat.load(state_dir, env=os.environ)      `--chat own` (TANDEM_CHAT=own)
    chat.gateway = "http://127.0.0.1:8080"              the controller's own address, once bound
    chat.view()                  what runs (GET /v1/fleet/chat)
    chat.act(action, who)        start | stop | restart: a job (POST /v1/fleet/chat/{action})
    chat.tick()                  the watcher, once a minute (start_watcher, close)

WHO RUNS CHATD. `tandem-app serve --chat off` (OPS-288, what every new install writes): nobody,
the Chat tab says Chat is off until an admin switches it on in Settings (proxy/appparts.py, which
then makes it this controller's). `--chat external` (TANDEM_CHAT's default when unset) leaves
it to someone else and only forwards /v1/chat-app/* to QSE_CHAT_URL, as before: TandemNode on an
engine box (`tandem-node serve --chat on`, proxy/nodechat.py), an engine's own launchers before
engine API 1.7, or a unit. `--chat own` makes it this controller's: started, stopped and watched here, on a host
that may run no engine at all, and the forward goes to this chatd. One owner at a time: a box whose
TandemNode or engine runs chatd keeps `external`.

ITS OWN ENVIRONMENT. chatd runs with the variables of its env file, `<state dir>/chatd.env`
(TANDEM_CHAT_ENV; KEY=VALUE lines, `export` and quotes allowed, never run as a script), and only
these of the controller's process: PATH, HOME, USER, LOGNAME, LANG, LC_*, TZ, TMPDIR,
XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS. Never the controller's admin token, its backends' keys
or master key, never an engine's secrets.env or serve.env (TandemNode's launcher reads those,
proxy/nodechat.py). Defaults for what the file leaves out:

    QSE_CHAT_URL     http://127.0.0.1:8016       where chatd listens (its port)
    QSE_CHAT_DATA    <state dir>/chat            its chats, forward.key and chatd.pid
    QSE_CHAT_ENGINE  <gateway>/chat-engine       the engine chatd calls: this controller's
                                                 gateway as a whole engine API (SRV-843,
                                                 proxy/chatengine.py), so chat traffic passes
                                                 its routes, keys and ledger
    QSE_CHAT_HOST    app or engine               SRV-843: whether the chat engine runs on this
                                                 host (`engine_here`): engine when its URL is
                                                 loopback, else app. chatd sizes its sandboxes by
                                                 it (chatd/sandbox.py: 2 GiB and 2 running on
                                                 app, 4 and 4 beside an engine) and asks an
                                                 engine to release memory only beside one
    QSE_CHAT_ENGINE_KEY  this controller's key   SRV-843: once the controller has users, a key
                         for chatd               of its own user `chatd`, made here and kept in
                                                 <data>/engine.key (0600), so chat traffic
                                                 passes the completions' rule and its ledger
                                                 rows name chatd; made again when that user is
                                                 gone or disabled
    CHATD_PY         this controller's Python    the interpreter

QSE_CHAT_MAX_RUNS and the sandbox's QSE_SANDBOX_* go in the same file
(packaging/linux/chatd.env.example); a value there wins over every default above.

THE PROCESS is `<python> -u -m chatd --host H --port P --data D` from TandemApp's tree
(PYTHONPATH), in a session of its own, its output appended to `<state dir>/logs/chatd.log`; its pid
file is `<data>/chatd.pid`, the one ops/chatd.sh keeps, so `ops/chatd.sh status` with the same data
sees it. Under systemd (INVOCATION_ID and XDG_RUNTIME_DIR set, systemd-run found) a start runs in a
scope of its own (`systemd-run --user --scope`), so a restart of the controller's unit never ends
a chat run; a chatd found running at the controller's start (its pid file, `-m chatd` in its
command line, /health saying chatd) is adopted, not started twice. A start waits 10 s for /health;
a stop sends SIGTERM (open runs end `interrupted`, Continue resumes them), then SIGKILL after 30 s.

THE WATCHER is ops/watchdog.sh's chatd rule as TandemNode runs it, once a minute: a running chatd
clears the count; a dead one is started, at most 3 starts in a clock hour (`<state dir>/chat.watch`)
then one line "giving up until the next hour"; a hand stop holds it until a hand start or restart.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from proxy.nodechat import ACTIONS, EVERY_S, MAX_STARTS, ChatError

#: SRV-843: the path under the controller's own URL where chatd finds its engine
#: (proxy/chatengine.py PREFIX, kept here so this module imports nothing of the proxy's routes)
ENGINE_PREFIX = "/chat-engine"
HOST_KINDS = ("app", "engine")
ENGINE_USER = "chatd"
ENGINE_KEY_FILE = "engine.key"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODES = ("off", "external", "own")
DEFAULT_URL = "http://127.0.0.1:8016"
START_WAIT_S = 10.0
STOP_WAIT_S = 30.0
#: the controller's variables chatd gets; everything else comes from its env file
PASS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS")


def read_env_file(path: str) -> dict:
    """KEY=VALUE lines of `path` ({} when there is none). A line that is not one is refused with
    its number; the values are never printed."""
    out: dict = {}
    try:
        f = open(path)
    except FileNotFoundError:
        return out
    with f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            k = k.strip()
            if not sep or not k or not (k[0].isalpha() or k[0] == "_") or \
                    not k.replace("_", "").isalnum():
                raise ValueError(f"{path}:{n}: not a KEY=VALUE line")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5, stdin=subprocess.DEVNULL)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_chatd(pid: int | None) -> bool:
    """`pid` lives and runs `-m chatd` (a reused pid is not a running chatd)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return " -m chatd " in f" {_cmdline(pid)} "


class AppChat:
    def __init__(self, state_dir: str, cfg: dict, *, env=None, log=lambda m: print(m, flush=True),
                 clock=time.time, which=shutil.which, every_s: float = EVERY_S,
                 env_path: str | None = None):
        self.state_dir = os.path.expanduser(state_dir)
        self.cfg = dict(cfg)                         # the env file's variables
        self.env = dict(os.environ if env is None else env)
        self.env_path = env_path
        self.log, self.clock, self.which, self.every_s = log, clock, which, every_s
        self.url = self.cfg.get("QSE_CHAT_URL") or DEFAULT_URL
        u = urllib.parse.urlsplit(self.url)
        if u.scheme != "http" or not u.hostname or not u.port:
            raise ValueError(f"QSE_CHAT_URL {self.url!r}: http://HOST:PORT")
        self.host, self.port = u.hostname, u.port
        self.data = os.path.expanduser(self.cfg.get("QSE_CHAT_DATA") or
                                       os.path.join(self.state_dir, "chat"))
        self.python = os.path.expanduser(self.cfg.get("CHATD_PY") or sys.executable)
        self.log_path = os.path.join(self.state_dir, "logs", "chatd.log")
        self.watch_path = os.path.join(self.state_dir, "chat.watch")
        self.pid_path = os.path.join(self.data, "chatd.pid")
        self.gateway: str | None = None              # the controller's own URL, set once bound
        # SRV-843, set by the controller: whether its chat engine runs on this host, and its
        # users (proxy/config.py ConfigDB) for chatd's own key
        self.engine_here = None
        self.users = None
        self.proc: subprocess.Popen | None = None
        self.job: dict | None = None
        self.lock = threading.Lock()
        self._run = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @classmethod
    def load(cls, state_dir: str, env=None, **kw) -> "AppChat":
        env = os.environ if env is None else env
        path = os.path.expanduser(env.get("TANDEM_CHAT_ENV") or
                                  os.path.join(os.path.expanduser(state_dir), "chatd.env"))
        return cls(state_dir, read_env_file(path), env=env, env_path=path, **kw)

    # ------------------------------------------------------------------ the forward's side
    def forward_key(self) -> str | None:
        k = (self.cfg.get("QSE_CHAT_FORWARD_KEY") or "").strip()
        if k:
            return k
        try:
            with open(os.path.join(self.data, "forward.key")) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def engine_url(self) -> str:
        return self.cfg.get("QSE_CHAT_ENGINE") or \
            (self.gateway or "http://127.0.0.1:8080") + ENGINE_PREFIX

    def host_kind(self) -> str:
        """`engine` when chatd shares its host with the chat engine, `app` otherwise (the env
        file's QSE_CHAT_HOST wins)."""
        got = (self.cfg.get("QSE_CHAT_HOST") or "").strip().lower()
        if got in HOST_KINDS:
            return got
        try:
            here = self.engine_here() if self.engine_here is not None else None
        except Exception:                                            # noqa: BLE001
            here = None
        return "engine" if here else "app"

    #: SRV-869 (accounts on): () -> a fresh secret of chatd's service identity svc:chatd, minted
    #: at each start of chatd and handed over in its environment only (proxy/app.py own_chat)
    service_key = None

    def engine_key(self) -> str | None:
        """chatd's key for this controller's completions once it has users (QSE_CHAT_ENGINE_KEY
        above); None while it has none, or when the env file names one. With the accounts on
        the service identity replaces it (start): no users row, no engine.key."""
        if (self.cfg.get("QSE_CHAT_ENGINE_KEY") or "").strip() or self.users is None or \
                self.service_key is not None:
            return None
        try:
            if not self.users.users_active():
                return None
        except Exception:                                            # noqa: BLE001
            return None
        path = os.path.join(self.data, ENGINE_KEY_FILE)
        try:
            with open(path) as f:
                key = f.read().strip()
        except OSError:
            key = ""
        if key and self.users.user_for_key(key) == ENGINE_USER:
            return key
        for u in self.users.users():
            if u["label"] == ENGINE_USER and u["source"] == "database":
                self.users.delete_user(u["id"], who="controller")
        _row, key = self.users.create_user({"label": ENGINE_USER}, who="controller")
        os.makedirs(self.data, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        os.replace(tmp, path)
        self.log(f"[chat] chatd has a key of its own for this controller (user {ENGINE_USER})")
        return key

    # ------------------------------------------------------------------ the process
    def child_env(self) -> dict:
        env = {k: v for k, v in self.env.items() if k in PASS or k.startswith("LC_")}
        env.update(self.cfg)
        env.pop("CHATD_PY", None)
        env.update(QSE_CHAT_URL=self.url, QSE_CHAT_DATA=self.data,
                   QSE_CHAT_ENGINE=self.engine_url(), QSE_CHAT_LOG=self.log_path,
                   QSE_CHAT_HOST=self.host_kind(), PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
        key = self.engine_key()
        if key:
            env["QSE_CHAT_ENGINE_KEY"] = key
        return env

    def _scoped(self) -> bool:
        return bool(self.env.get("INVOCATION_ID")) and bool(self.env.get("XDG_RUNTIME_DIR")) and \
            self.which("systemd-run") is not None

    def argv(self) -> list[str]:
        cmd = [self.python, "-u", "-m", "chatd", "--host", self.host, "--port", str(self.port),
               "--data", self.data]
        if self._scoped():
            cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   f"--unit=tandem-app-chatd-{int(self.clock() * 1000)}", "--"] + cmd
        return cmd

    def health(self, timeout: float = 1.0) -> dict | None:
        h = f"[{self.host}]" if ":" in self.host else self.host
        req = urllib.request.Request(f"http://{h}:{self.port}/health")
        key = self.forward_key()
        if key:
            req.add_header("X-Tandem-Forward", key)         # its sandbox's words come with it
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.loads(r.read() or b"{}")
        except Exception:                                            # noqa: BLE001
            return None
        return doc if isinstance(doc, dict) else {}

    def pid(self) -> int | None:
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None                         # reaped: it ended
        try:
            with open(self.pid_path) as f:
                raw = "".join(c for c in f.read() if c.isdigit())
            return int(raw) if raw else None
        except OSError:
            return None

    def status(self) -> dict:
        pid = self.pid()
        alive = is_chatd(pid)
        h = self.health() if alive else None
        sb = (h or {}).get("sandbox")
        return {"running": alive, "pid": pid if alive else None,
                "answers": bool(h and h.get("service") == "chatd"),
                "version": (h or {}).get("version"),
                # SRV-835: chatd's own word on its sandboxes (backend, state, why); None from a
                # chatd that does not answer or says nothing of them
                "sandbox": sb if isinstance(sb, dict) else None}

    def running(self) -> bool:
        return self.status()["running"]

    def _tail(self, n: int = 20) -> list[str]:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 16384))
                return f.read().decode(errors="replace").splitlines()[-n:]
        except OSError:
            return []

    def start(self) -> tuple[int, str]:
        """chatd started (or already running): (0, words); (1, why) when it did not."""
        st = self.status()
        if st["running"]:
            return 0, f"already running (pid {st['pid']})"
        try:
            os.unlink(self.pid_path)
        except FileNotFoundError:
            pass
        h = self.health()
        if h is not None:
            return 1, (f"a chatd this data folder does not know answers on {self.url} (another "
                       f"install?)" if h.get("service") == "chatd" else
                       f"port {self.port} is taken by another program (QSE_CHAT_URL={self.url})")
        os.makedirs(self.data, mode=0o700, exist_ok=True)
        os.makedirs(os.path.dirname(self.log_path), mode=0o700, exist_ok=True)
        out = open(self.log_path, "ab")
        try:
            env = self.child_env()
            if self.service_key is not None and not \
                    (self.cfg.get("QSE_CHAT_ENGINE_KEY") or "").strip():
                env["QSE_CHAT_ENGINE_KEY"] = self.service_key()      # SRV-869
            self.proc = subprocess.Popen(self.argv(), cwd=ROOT, env=env,
                                         stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, start_new_session=True,
                                         close_fds=True)
        except OSError as exc:
            return 1, f"chatd did not start: {type(exc).__name__}: {exc}"
        finally:
            out.close()
        pid = self.proc.pid
        tmp = self.pid_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{pid}\n")
        os.replace(tmp, self.pid_path)
        end = time.monotonic() + START_WAIT_S
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                break
            h = self.health()
            if h is not None and h.get("service") == "chatd":
                return 0, f"started (pid {pid}, {self.url}, engine {self.engine_url()})"
            time.sleep(0.2)
        why = (f"pid {pid} did not answer {self.url}/health within {START_WAIT_S:g} s; stopped it"
               if self.proc.poll() is None else f"chatd exited at start ({self.python})")
        self._end(pid)
        tail = "; ".join(self._tail(3))
        return 1, why + (f"; log: {tail}" if tail else "")

    def _end(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        end = time.monotonic() + STOP_WAIT_S
        while time.monotonic() < end:
            if self.proc is not None and self.proc.pid == pid:
                if self.proc.poll() is not None:
                    break
            elif not is_chatd(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.proc is not None and self.proc.pid == pid:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        try:
            os.unlink(self.pid_path)
        except FileNotFoundError:
            pass

    def stop(self) -> tuple[int, str]:
        pid = self.pid()
        if not is_chatd(pid):
            try:
                os.unlink(self.pid_path)
                return 0, "not running (a stale pid file, removed)"
            except FileNotFoundError:
                return 0, "not running"
        self._end(pid)
        return 0, "stopped"

    # ------------------------------------------------------------------ the watch file
    def _watch(self) -> dict:
        try:
            with open(self.watch_path) as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _update(self, **kw) -> None:
        doc = self._watch()
        for k, v in kw.items():
            if v is None:
                doc.pop(k, None)
            else:
                doc[k] = v
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.watch_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, self.watch_path)

    def hour(self) -> str:
        return time.strftime("%Y%m%d%H", time.localtime(self.clock()))

    # ------------------------------------------------------------------ what runs
    def view(self) -> dict:
        st = self.status()
        w = self._watch()
        job = self.job
        return {"own": True, "owner": "controller", "state": "running" if st["running"] else
                "stopped", "pid": st["pid"], "answers": st["answers"], "version": st["version"],
                "url": self.url, "data": self.data, "engine": self.engine_url(),
                "host": self.host_kind(),
                "env_file": self.env_path, "env_file_present": bool(
                    self.env_path and os.path.isfile(self.env_path)),
                "log": self.log_path,
                "watch": {"hour": w.get("hour"), "starts": int(w.get("n") or 0),
                          "gave_up": int(w.get("n") or 0) > MAX_STARTS,
                          "stopped_by_hand": bool(w.get("hand_stop"))},
                "job": dict(job) if job else None}

    # ------------------------------------------------------------------ hand verbs
    def act(self, action: str, who: str) -> dict:
        if action not in ACTIONS:
            raise ChatError(404, "not_found", f"no action {action}")
        with self.lock:
            if self.job is not None and self.job["state"] == "running":
                raise ChatError(409, "busy", f"chatd: a {self.job['action']} runs")
            self.job = {"id": f"chat-{int(self.clock() * 1000)}", "action": action,
                        "state": "running", "who": who, "started_at": self.clock(),
                        "ended_at": None, "rc": None, "message": None}
            job = self.job
        self.log(f"[chat] {action} by {who}")
        threading.Thread(target=self._job, args=(job,), name=f"chat-{action}",
                         daemon=True).start()
        return dict(job)

    def _job(self, job: dict) -> None:
        try:
            rc, msg = self.run_action(job["action"])
            job.update(state="done" if rc == 0 else "failed", rc=rc, message=msg)
        except Exception as exc:                                     # noqa: BLE001
            job.update(state="failed", message=f"{type(exc).__name__}: {exc}")
        job["ended_at"] = self.clock()
        self.log(f"[chat] {job['action']}: {job['state']}"
                 f"{' · ' + job['message'] if job['message'] else ''}")

    def run_action(self, action: str) -> tuple[int, str]:
        """A hand verb, now. A hand stop holds the watcher until a hand start or restart."""
        with self._run:
            if action == "stop":
                rc, msg = self.stop()
            elif action == "start":
                rc, msg = self.start()
            else:
                self.stop()
                rc, msg = self.start()
        if rc == 0:
            self._update(hand_stop=True if action == "stop" else None)
        return rc, msg

    # ------------------------------------------------------------------ the watcher
    def tick(self) -> str:
        """One look: running, started, failed, gave_up, stopped_by_hand or busy."""
        if self.job is not None and self.job["state"] == "running":
            return "busy"
        if not self._run.acquire(blocking=False):
            return "busy"
        try:
            w = self._watch()
            if w.get("hand_stop"):
                return "stopped_by_hand"
            if self.running():
                if w.get("hour") or w.get("n"):
                    self._update(hour=None, n=None)
                return "running"
            hour = self.hour()
            n = int(w.get("n") or 0) if w.get("hour") == hour else 0
            if n >= MAX_STARTS:
                if n == MAX_STARTS:
                    self.log("[chat] chatd: 3 starts this hour did not keep it running; giving "
                             "up until the next hour")
                    self._update(hour=hour, n=MAX_STARTS + 1)
                return "gave_up"
            self._update(hour=hour, n=n + 1)
            rc, msg = self.start()
            if rc == 0:
                self.log(f"[chat] chatd was not running; {msg} ({n + 1}/3 this hour)")
                return "started"
            self.log(f"[chat] chatd was not running and did not start ({n + 1}/3 this hour): "
                     f"{msg}")
            return "failed"
        finally:
            self._run.release()

    def start_watcher(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:                             # noqa: BLE001
                    self.log(f"[chat] the watcher failed once: {type(exc).__name__}: {exc}")
                self._stop.wait(self.every_s)
        self._thread = threading.Thread(target=loop, name="chat-watch", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """The watcher stops; chatd keeps running (a controller restart never ends a chat run)."""
        self._stop.set()

    # ------------------------------------------------------------------ the routes
    def handle(self, method: str, parts: list[str], who: str) -> tuple[int, dict]:
        try:
            if method == "GET" and parts == ["chat"]:
                return 200, self.view()
            if method == "POST" and len(parts) == 2 and parts[0] == "chat":
                return 200, self.act(parts[1], who)
            raise ChatError(404, "not_found", f"no route {method} /v1/fleet/{'/'.join(parts)}")
        except ChatError as exc:
            return exc.status, exc.body()


def external_view(env=None) -> dict:
    """GET /v1/fleet/chat while chatd is someone else's (`--chat external`)."""
    env = os.environ if env is None else env
    return {"own": False, "owner": "external", "url": env.get("QSE_CHAT_URL") or DEFAULT_URL}


def handle_off(method: str, parts: list[str]) -> tuple[int, dict]:
    """GET /v1/fleet/chat and its verbs while Chat is off (OPS-288)."""
    if method == "GET" and parts == ["chat"]:
        return 200, {"own": False, "owner": "off"}
    if method == "POST" and len(parts) == 2 and parts[0] == "chat":
        return 409, ChatError(409, "chat_off", "Chat is off on this TandemApp: switch it on in "
                                               "Settings, App, What runs here").body()
    return 404, ChatError(404, "not_found", f"no route {method} /v1/fleet/{'/'.join(parts)}").body()


def handle_external(method: str, parts: list[str], env=None) -> tuple[int, dict]:
    if method == "GET" and parts == ["chat"]:
        return 200, external_view(env)
    if method == "POST" and len(parts) == 2 and parts[0] == "chat":
        return 409, ChatError(409, "not_owned", "chatd is not this controller's: start it with "
                                                "`tandem-app serve --chat own` (TANDEM_CHAT=own)"
                              ).body()
    return 404, ChatError(404, "not_found", f"no route {method} /v1/fleet/{'/'.join(parts)}").body()
