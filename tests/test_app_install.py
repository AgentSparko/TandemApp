"""OPS-282: TandemApp in one command (packaging/install.sh, packaging/tandemapp_install.py), its
release archive (tools/app_release.py) and its container (packaging/Dockerfile.app,
packaging/container-start.sh).

Every install runs the real installer in a HOME of its own on a release archive packed from this
tree with a made-up page (its bundle.json recorded, so it verifies as a built one does), into its own
prefix and state dir, on a free port. The venv is the one running these tests (--venv: Python 3.11+
with cryptography and psutil), except in test_venv_from_the_index, which makes one from the
package index (TANDEM_INSTALL_NET=1, given to this file run alone: the suite runner passes no
such variable on). chatd's port and its sandbox socket are set in a
chatd.env written first, so nothing reaches this machine's own services; every process started is
stopped at the end.

  Gherkin (OPS-282, plan 13 items 5 and 6):
  1  Given a Linux box or a Mac, When the user runs the install command, Then the page answers and
     runs as a background service; Given a TandemApp with no engine, When the user opens the page,
     Then the wizard opens at "Add engine" (the app view's setup.needs_setup, first_page add_engine)
     And chat sandboxes are off with no socket, and the health page says so
                                                                  test_install_and_wizard_flag
  2  a second run with the same release changes nothing that runs (same pid, the release there
     already); a new release is switched to and restarted, the old one kept; a release that does
     not come up is switched back and the run fails with its log; --listen beyond loopback writes
     an admin token (never printed) and the page's sign-in        test_rerun_update_rollback_listen
  3  the systemd user unit: written from packaging/linux/tandem-app.service with this prefix and
     state dir, enabled and restarted (a fake systemctl that runs the unit's ExecStart with its
     Environment), the linger note                                test_systemd_unit
  4  launchd's plist and the choice of the manager; a dry run changes nothing; refusals: no page,
     --bundle-url without its sha256, a bad --listen, a manager of the other system
                                                                  test_launchd_auto_dry_refusals
  5  the archive: the same tree packs to the same bytes, under tandemapp-<version>/, with the page
     and the installer, without tests or the page's sources       test_release_archive
  6  --with-sandbox: ops/sandbox/install.sh's plan (no root): the drop-in for the socket, the image
     built before the egress table, the boot unit, the ACLs for the container's uid, the memory
     check; the installer's dry run names the sudo command; the memory note's words
                                                                  test_sandbox_plan
  7  the container: the Dockerfile carries the OpenSSH client, chat here, the start script and
     /data; the start script writes chatd.env once with the sandbox socket's path
                                                                  test_container_files
  8  the image built and run (TANDEM_TEST_DOCKER=1 with docker): /health, the wizard flag, ssh,
     sandboxes off                                                test_container_build
  9  a venv from the package index (TANDEM_INSTALL_NET=1)         test_venv_from_the_index

    python tests/test_app_install.py
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packaging"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_release  # noqa: E402
import tandemapp_install as inst  # noqa: E402
from tandemapp import bundle  # noqa: E402

from scriptmode import Skip  # noqa: E402

VENV = os.path.dirname(os.path.dirname(sys.executable))


# ---------------------------------------------------------------------------- the rig
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_page(dist: str) -> str:
    os.makedirs(os.path.join(dist, "assets"), exist_ok=True)
    with open(os.path.join(dist, "index.html"), "w") as f:
        f.write('<!doctype html><html><head><meta charset="utf-8"><title>TandemApp</title>'
                '<script type="module" src="./assets/app.js"></script></head>'
                '<body><div id="app"></div></body></html>\n')
    with open(os.path.join(dist, "assets", "app.js"), "w") as f:
        f.write("document.getElementById('app').textContent = 'page';\n")
    return bundle.write_manifest(dist, "record")["sha256"]


def extract(archive: str, dest: str) -> None:
    with tarfile.open(archive) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
        else:                                                        # pragma: no cover
            t.extractall(dest)


class Box:
    """A release unpacked from an archive, a HOME, a prefix and a state dir of its own."""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.page = os.path.join(tmp, "page")
        self.bundle = make_page(self.page)
        self.archive = os.path.join(tmp, "tandemapp.tar.gz")
        app_release.pack(self.archive, self.page)
        extract(self.archive, os.path.join(tmp, "unpacked"))
        self.version = inst.read_version(ROOT)
        self.tree = os.path.join(tmp, "unpacked", f"tandemapp-{self.version}")
        self.home = os.path.join(tmp, "home")
        self.prefix = os.path.join(tmp, "prefix")
        self.state = os.path.join(tmp, "state")
        os.makedirs(self.home)
        os.makedirs(self.state, mode=0o700)
        self.port = free_port()
        self.chat_port = free_port()
        self.sock = os.path.join(tmp, "no-runtime.sock")
        with open(os.path.join(self.state, "chatd.env"), "w") as f:
            f.write(f"QSE_CHAT_URL=http://127.0.0.1:{self.chat_port}\n"
                    f"QSE_SANDBOX_SOCKET={self.sock}\nQSE_SANDBOX_MEM_GB=1\n")
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)

    def env(self, **kw) -> dict:
        env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": self.home, "LANG": os.environ.get("LANG", "C.UTF-8"), "USER": "tester",
               "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
               "TANDEM_INSTALL_PYTHON": sys.executable}
        env.update(kw)
        return env

    def install(self, *args, tree: str | None = None, env: dict | None = None,
                venv: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
        argv = ["/bin/bash", os.path.join(tree or self.tree, "packaging", "install.sh"),
                "--prefix", self.prefix, "--state-dir", self.state, *args]
        if venv:
            argv += ["--venv", VENV]
        r = subprocess.run(argv, env=env or self.env(), capture_output=True, text=True,
                           timeout=timeout)
        return r

    def get(self, path: str, token: str | None = None) -> tuple[int, dict, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            c.request("GET", path, headers=h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        except OSError:
            return 0, {}, b""
        finally:
            c.close()

    def json(self, path: str) -> dict:
        st, _, body = self.get(path)
        assert st == 200, (path, st, body[:300])
        return json.loads(body)

    def pid(self) -> int | None:
        try:
            with open(os.path.join(self.state, "tandem-app.pid")) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def stop(self, *more_pids) -> None:
        pids = [self.pid(), *more_pids]
        try:
            with open(os.path.join(self.state, "chat", "chatd.pid")) as f:
                pids.append(int("".join(c for c in f.read() if c.isdigit()) or 0))
        except OSError:
            pass
        for pid in [p for p in pids if p]:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def ok_run(r: subprocess.CompletedProcess) -> str:
    assert r.returncode == 0, f"rc {r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
    return r.stdout


def wait_for(fn, timeout: float = 30.0, every: float = 0.5):
    t_end = time.monotonic() + timeout
    while True:
        got = fn()
        if got or time.monotonic() > t_end:
            return got
        time.sleep(every)


def releases(box: Box) -> list[str]:
    return sorted(x for x in os.listdir(os.path.join(box.prefix, "releases")) if ".part-" not in x)


# ---------------------------------------------------------------------------- 1 install, wizard

def test_install_and_wizard_flag(tmp_path):
    box = Box(str(tmp_path))
    try:
        out = ok_run(box.install("--port", str(box.port), "--service", "process"))
        assert f"TandemApp {box.version} is running (process)." in out, out
        assert f"Open http://127.0.0.1:{box.port}/" in out, out
        h = box.json("/health")
        assert h["status"] == "ok" and h["version"] == box.version, h
        assert h["dashboard"]["sha256"] == box.bundle and h["dashboard"]["verified"], h
        st, hd, body = box.get("/dashboard/")
        assert st == 200 and hd.get("X-Tandem-Bundle") == box.bundle[:16] and b"TandemApp" in body
        # the wizard first: nothing serves here yet
        app = box.json("/v1/fleet/app")
        assert app["setup"] == {"needs_setup": True, "engines": 0, "nodes": 0, "backends": 0,
                                "first_page": "add_engine"}, app["setup"]
        # the state: 0700, app.env 0600 with loopback, a fresh master key 0600
        assert os.stat(box.state).st_mode & 0o777 == 0o700
        envf = os.path.join(box.state, "app.env")
        assert os.stat(envf).st_mode & 0o777 == 0o600
        env = inst.read_env(envf)
        assert env["TANDEM_HOST"] == "127.0.0.1" and env["TANDEM_PORT"] == str(box.port)
        assert env["TANDEM_CHAT"] == "own" and env["TANDEM_CONDUCTOR"] == "off"
        assert env["TANDEM_NODE_AGENT"] == "off" and "TANDEM_ADMIN_TOKEN" not in env
        keyf = os.path.join(box.state, "secrets.key")
        assert os.stat(keyf).st_mode & 0o777 == 0o600
        with open(keyf) as f:
            import base64
            assert len(base64.b64decode(f.read().strip())) == 32
        # the layout: current -> releases/<version>-<hash>, its venv the one given
        rel = releases(box)
        assert len(rel) == 1 and rel[0].startswith(box.version.replace("+", "_") + "-"), rel
        assert os.readlink(os.path.join(box.prefix, "current")) == os.path.join("releases", rel[0])
        assert os.path.realpath(os.path.join(box.prefix, "current", "venv")) == \
            os.path.realpath(VENV)
        assert not os.path.exists(os.path.join(box.prefix, "current", "tests"))
        assert not os.path.exists(os.path.join(box.prefix, "current", "dashboard", "src"))
        # chat: the controller runs chatd; with no socket the health page says sandboxes are off
        def chat_up():
            st, _, b = box.get("/v1/fleet/health/controller?fresh=1")
            if st != 200:
                return None
            doc = json.loads(b)
            return doc if any(c["check"] == "chatd" and c["ok"] for c in doc["checks"]) else None
        doc = wait_for(chat_up, 40, 3.2)
        assert doc is not None, box.json("/v1/fleet/health/controller")
        off = [f for f in doc["findings"] if f["code"] == "sandbox_off"]
        assert off and off[0]["facts"]["path"] == box.sock, doc["findings"]
        assert any(f["code"] == "no_engine" for f in doc["findings"])
    finally:
        box.stop()
    return "installed, running, the wizard flag, the state's modes, sandboxes off said"


# ---------------------------------------------------------------------------- 2 rerun, update

def test_rerun_update_rollback_listen(tmp_path):
    box = Box(str(tmp_path))
    try:
        ok_run(box.install("--port", str(box.port), "--service", "process"))
        pid1 = box.pid()
        out = ok_run(box.install("--service", "process"))
        assert "there already" in out and box.pid() == pid1, out
        assert box.json("/health")["version"] == box.version
        # a new release: switched to, restarted, the old one kept
        v2 = box.version + "-t2"
        with open(os.path.join(box.tree, "VERSION"), "w") as f:
            f.write(v2 + "\n")
        out = ok_run(box.install("--service", "process"))
        assert f"TandemApp {v2} is running" in out and "(1 older kept)" in out, out
        assert box.pid() != pid1 and box.json("/health")["version"] == v2
        rel = releases(box)
        assert len(rel) == 2
        cur2 = os.readlink(os.path.join(box.prefix, "current"))
        # a release that does not come up: switched back, the run fails with its log
        v3 = box.version + "-t3"
        with open(os.path.join(box.tree, "VERSION"), "w") as f:
            f.write(v3 + "\n")
        with open(os.path.join(box.tree, "proxy", "__main__.py"), "w") as f:
            f.write("import sys\nprint('broken release', flush=True)\nsys.exit(3)\n")
        r = box.install("--service", "process", "--wait", "8")
        assert r.returncode == 1 and "switched back" in r.stdout and "broken release" in r.stdout, \
            r.stdout[-2000:]
        assert os.readlink(os.path.join(box.prefix, "current")) == cur2
        assert wait_for(lambda: box.get("/health")[0] == 200 and
                        box.json("/health")["version"] == v2, 30), "the old release runs again"
        assert len(releases(box)) == 3, "the failed release stays on disk, unused"
        box.stop()
        # --listen beyond loopback: an admin token, never printed, and the sign-in
        envf = os.path.join(box.state, "app.env")
        out = ok_run(box.install("--listen", "0.0.0.0", "--service", "none", tree=box.tree))
        env = inst.read_env(envf)
        assert env["TANDEM_HOST"] == "0.0.0.0" and env["TANDEM_DASHBOARD_LOGIN"] == "on", env
        tok = env["TANDEM_ADMIN_TOKEN"]
        assert len(tok) >= 32 and tok not in out and "TANDEM_ADMIN_TOKEN" in out, out
        assert "nothing was started" in out
        out = ok_run(box.install("--listen", "127.0.0.1", "--service", "none"))
        env = inst.read_env(envf)
        assert env["TANDEM_HOST"] == "127.0.0.1" and env["TANDEM_ADMIN_TOKEN"] == tok
        assert os.stat(envf).st_mode & 0o777 == 0o600
    finally:
        box.stop()
    return "idempotent rerun, update, rollback, --listen with a token"


# ---------------------------------------------------------------------------- 3 systemd

FAKE_SYSTEMCTL = r'''#!PYTHON
import json, os, shlex, signal, subprocess, sys, time
d = os.environ["FAKE_SYSTEMD"]
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:1] != ["--user"]:
    sys.exit(1)
cmd, rest = args[1], args[2:]
unit = [a for a in rest if not a.startswith("-")]
pidf = os.path.join(d, "unit.pid")
def alive():
    try:
        pid = int(open(pidf).read())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None
if cmd in ("show-environment", "daemon-reload", "enable"):
    sys.exit(0)
if cmd == "is-active":
    sys.exit(0 if unit == ["tandem-app"] and alive() else 3)
if cmd == "restart" and unit == ["tandem-app"]:
    pid = alive()
    if pid:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
    path = os.path.join(os.environ["HOME"], ".config", "systemd", "user", "tandem-app.service")
    env, argv = dict(os.environ), None
    for line in open(path):
        line = line.strip()
        if line.startswith("Environment="):
            k, _, v = shlex.split(line[len("Environment="):])[0].partition("=")
            env[k] = v
        elif line.startswith("ExecStart="):
            argv = shlex.split(line[len("ExecStart="):])
    log = open(os.path.join(d, "unit.log"), "ab")
    p = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    open(pidf, "w").write(str(p.pid))
    sys.exit(0)
sys.exit(1)
'''


def test_systemd_unit(tmp_path):
    if not sys.platform.startswith("linux"):
        raise Skip("a systemd user unit is Linux's")
    box = Box(str(tmp_path))
    fake = os.path.join(str(tmp_path), "systemd")
    os.makedirs(fake)
    with open(os.path.join(box.bin, "systemctl"), "w") as f:
        f.write(FAKE_SYSTEMCTL.replace("PYTHON", sys.executable))
    with open(os.path.join(box.bin, "loginctl"), "w") as f:
        f.write("#!/bin/sh\necho Linger=no\n")
    for name in ("systemctl", "loginctl"):
        os.chmod(os.path.join(box.bin, name), 0o755)
    upid = None
    try:
        out = ok_run(box.install("--port", str(box.port), "--service", "systemd",
                                 env=box.env(FAKE_SYSTEMD=fake)))
        with open(os.path.join(fake, "unit.pid")) as f:
            upid = int(f.read())
        assert "is running (systemd)" in out and "sudo loginctl enable-linger tester" in out, out
        unit = os.path.join(box.home, ".config", "systemd", "user", "tandem-app.service")
        with open(unit) as f:
            text = f.read()
        launcher = os.path.join(box.prefix, "current", "packaging", "linux", "tandem-app.sh")
        assert f"ExecStart=/bin/bash {launcher}" in text, text
        assert f'Environment="TANDEM_APP_STATE={box.state}"' in text
        assert f'Environment="TANDEM_APP_PYTHON={box.prefix}/current/venv/bin/python"' in text
        assert "%h/tandemapp" not in text and "WantedBy=default.target" in text
        assert "Restart=on-failure" in text
        with open(os.path.join(fake, "calls.jsonl")) as f:
            calls = [json.loads(x) for x in f]
        assert ["--user", "daemon-reload"] in calls and \
            ["--user", "enable", "--quiet", "tandem-app"] in calls and \
            ["--user", "restart", "tandem-app"] in calls, calls
        assert box.json("/health")["version"] == box.version
        n = len(calls)
        out = ok_run(box.install("--service", "systemd", env=box.env(FAKE_SYSTEMD=fake)))
        with open(os.path.join(fake, "calls.jsonl")) as f:
            calls = [json.loads(x) for x in f][n:]
        assert ["--user", "restart", "tandem-app"] not in calls, ("unchanged: not restarted", calls)
    finally:
        box.stop(upid)
    return "the unit from C1's template, enabled and restarted, linger note, idempotent"


# ---------------------------------------------------------------------------- 4 launchd, dry

def test_launchd_auto_dry_refusals(tmp_path):
    doc = plistlib.loads(inst.plist_bytes("/p", "/s"))
    assert doc["Label"] == "org.tandemllm.app" and doc["KeepAlive"] and doc["RunAtLoad"]
    assert doc["ProgramArguments"] == ["/bin/bash", "/p/current/packaging/linux/tandem-app.sh"]
    assert doc["EnvironmentVariables"]["TANDEM_APP_STATE"] == "/s"
    assert doc["EnvironmentVariables"]["TANDEM_APP_PYTHON"] == "/p/current/venv/bin/python"
    assert doc["StandardOutPath"] == "/s/logs/tandem-app.log"
    assert inst.manager_of("auto", "darwin") == "launchd"
    assert inst.manager_of("auto", "linux", which=lambda x: None) == "process"
    assert inst.manager_of("auto", "linux", which=lambda x: "/bin/x", call=lambda a: 0) == "systemd"
    assert inst.manager_of("auto", "linux", which=lambda x: "/bin/x", call=lambda a: 1) == "process"
    assert inst.manager_of("none", "linux") == "none"
    box = Box(str(tmp_path))
    # a dry run: nothing made
    out = ok_run(box.install("--dry-run", "--service", "process"))
    assert "dry run" in out and "would copy" in out and "would start" in out, out
    assert not os.path.exists(box.prefix) and sorted(os.listdir(box.state)) == ["chatd.env"]
    # refusals
    r = box.install("--listen", "not-an-address", "--service", "none")
    assert r.returncode == 2 and "--listen" in r.stdout, r.stdout
    other = "launchd" if sys.platform.startswith("linux") else "systemd"
    r = box.install("--service", other)
    assert r.returncode == 2 and f"--service {other} does not run" in r.stdout, r.stdout
    bare = os.path.join(str(tmp_path), "bare")
    shutil.copytree(box.tree, bare, ignore=shutil.ignore_patterns("dist"))
    r = box.install("--service", "none", tree=bare)
    assert r.returncode == 1 and "no built page" in r.stdout, r.stdout
    r = box.install("--service", "none", "--bundle-url", "http://127.0.0.1:9/x.tar.gz", tree=bare)
    assert r.returncode == 2 and "--bundle-sha256" in r.stdout, r.stdout
    r = subprocess.run(["/bin/bash", os.path.join(box.tree, "packaging", "install.sh"),
                        "--python", "/bin/false"], capture_output=True, text=True,
                       env=box.env())
    assert r.returncode == 2 and "not Python 3.11" in r.stderr, r.stderr
    return "plist, auto, dry run, refusals"


# ---------------------------------------------------------------------------- 5 the archive

def test_release_archive(tmp_path):
    page = os.path.join(str(tmp_path), "page")
    sha_page = make_page(page)
    a1, a2 = os.path.join(str(tmp_path), "a1.tar.gz"), os.path.join(str(tmp_path), "a2.tar.gz")
    s1, s2 = app_release.pack(a1, page), app_release.pack(a2, page)
    assert s1 == s2, "the same tree packs to the same bytes"
    with open(a1, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == s1
    with open(a1 + ".sha256") as f:
        assert f.read() == f"{s1}  a1.tar.gz\n"
    version = inst.read_version(ROOT)
    with tarfile.open(a1) as t:
        members = {m.name: m for m in t.getmembers()}
    top = f"tandemapp-{version}/"
    assert all(n.startswith(top) for n in members)
    names = {n[len(top):] for n in members}
    for need in ("packaging/install.sh", "packaging/tandemapp_install.py",
                 "packaging/linux/tandem-app.sh", "packaging/linux/tandem-app.service",
                 "proxy/__main__.py", "chatd/__main__.py", "ops/sandbox/install.sh", "VERSION",
                 "pyproject.toml", "dashboard/dist/index.html", "dashboard/dist/bundle.json"):
        assert need in names, need
    assert not any(n.startswith(("tests/", "dashboard/src/", ".git")) for n in names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert members[top + "packaging/install.sh"].mode == 0o755
    assert members[top + "proxy/app.py"].mode == 0o644
    # the page inside is the one given, intact
    extract(a1, os.path.join(str(tmp_path), "x"))
    d = os.path.join(str(tmp_path), "x", top, "dashboard", "dist")
    assert bundle.verify(d)["ok"] and bundle.read_manifest(d)["sha256"] == sha_page
    # no page: refused
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "app_release.py"), "pack",
                        "--out", os.path.join(str(tmp_path), "n.tar.gz"), "--dist",
                        os.path.join(str(tmp_path), "none")], capture_output=True, text=True)
    assert r.returncode != 0 and "no built page" in (r.stderr + r.stdout)
    return f"{len(names)} files, reproducible, the page intact"


# ---------------------------------------------------------------------------- 6 the sandbox

def test_sandbox_plan(tmp_path):
    data = os.path.join(str(tmp_path), "data", "chat")
    r = subprocess.run(["/bin/bash", os.path.join(ROOT, "ops", "sandbox", "install.sh"), "--plan",
                        "--owner", "tester", "--data", data], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "podman.socket.d/tandem-owner.conf" in out and "systemctl --user restart podman.socket" \
        in out, out
    assert out.index("podman build -t tandem-sandbox:1") < \
        out.index("systemctl enable --now tandem-sbx-nft.service"), "the image before the table"
    assert "/etc/nftables.d/tandem-sbx.nft" in out and "/etc/nftables.conf" not in out
    first = 200000
    try:
        with open("/etc/subuid") as f:
            for line in f:
                if line.startswith("tandem-sbx:"):
                    first = int(line.split(":")[1])
    except OSError:
        pass
    for d in ("workspaces", "homes", "egress"):
        assert f"setfacl -m u:{first + 999}:rwX,d:u:{first + 999}:rwX,d:u:tester:rwX " \
               f"{data}/{d}" in out, out
    assert "== 6. the memory cgroup" in out
    assert out.rstrip().splitlines()[-1].startswith("QSE_SANDBOX_SOCKET=/run/user/")
    assert not os.path.exists(data), "--plan made nothing"
    r = subprocess.run(["/bin/bash", os.path.join(ROOT, "ops", "sandbox", "install.sh"), "--owner",
                        "tester"], capture_output=True, text=True)
    assert (r.returncode == 1 and "sudo" in r.stderr) or os.getuid() == 0
    # the installer's dry run names the sudo command (Linux)
    if sys.platform.startswith("linux"):
        box = Box(str(tmp_path / "box"))
        out = ok_run(box.install("--dry-run", "--with-sandbox", "--service", "none"))
        assert "would run: sudo bash" in out and "ops/sandbox/install.sh --owner tester --data " \
            + os.path.join(box.state, "chat") in out, out
    # the memory note: the health page's words, nothing edited
    root = str(tmp_path / "cg")
    os.makedirs(root)
    with open(os.path.join(root, "cgroup.controllers"), "w") as f:
        f.write("cpu io pids\n")
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert inst.memory_note(ROOT, None, root) is False
    assert "cgroup_enable=memory cgroup_memory=1" in buf.getvalue() and "nothing was edited" in \
        buf.getvalue(), buf.getvalue()
    with open(os.path.join(root, "cgroup.controllers"), "w") as f:
        f.write("cpu io memory pids\n")
    assert inst.memory_note(ROOT, None, root) is True
    return "plan order and ACLs, sudo needed, the dry run's command, the memory note"


# ---------------------------------------------------------------------------- 7 the container

def test_container_files(tmp_path):
    with open(os.path.join(ROOT, "packaging", "Dockerfile.app")) as f:
        df = f.read()
    with open(os.path.join(ROOT, "packaging", "Dockerfile.app.dockerignore")) as f:
        ign = f.read().splitlines()
    assert "openssh-client" in df and "--no-install-recommends" in df
    assert "COPY packaging/container-start.sh /usr/local/lib/tandemapp/container-start.sh" in df
    assert 'ENTRYPOINT ["/bin/sh", "/usr/local/lib/tandemapp/container-start.sh"]' in df
    assert 'CMD ["serve", "--no-keychain"]' in df and 'VOLUME ["/data"]' in df
    for k in ("TANDEM_CHAT=own", "TANDEM_CONDUCTOR=off", "TANDEM_NODE_AGENT=off",
              "TANDEM_APP_STATE=/data", "TANDEM_CONFIG_DB=/data/config.sqlite3"):
        assert k in df, k
    assert "rm -rf" not in df
    assert "!packaging/container-start.sh" in ign
    # every COPY source is let in by the dockerignore
    for line in df.splitlines():
        if line.startswith("COPY ") and "--from=" not in line:
            for src in line.split()[1:-1]:
                assert any(x.lstrip("!").rstrip("/") == src.rstrip("/") or
                           src.startswith(x.lstrip("!")) for x in ign if x.startswith("!")), src
    script = os.path.join(ROOT, "packaging", "container-start.sh")
    assert subprocess.run(["sh", "-n", script]).returncode == 0
    fakebin = os.path.join(str(tmp_path), "bin")
    os.makedirs(fakebin)
    with open(os.path.join(fakebin, "tandem-app"), "w") as f:
        f.write('#!/bin/sh\necho "ran: $*"\n')
    os.chmod(os.path.join(fakebin, "tandem-app"), 0o755)
    data = os.path.join(str(tmp_path), "data")
    env = {"PATH": fakebin + ":/usr/bin:/bin", "TANDEM_APP_STATE": data}
    r = subprocess.run(["sh", script, "serve", "--no-keychain"], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ran: serve --no-keychain", r
    chatf = os.path.join(data, "chatd.env")
    assert inst.read_env(chatf)["QSE_SANDBOX_SOCKET"] == "/run/tandem/sandbox.sock"
    assert os.stat(chatf).st_mode & 0o777 == 0o600 and os.path.isdir(os.path.join(data, "logs"))
    with open(chatf, "a") as f:
        f.write("QSE_SANDBOX_MEM_GB=3\n")
    subprocess.run(["sh", script, "serve"], env=env, capture_output=True)
    assert inst.read_env(chatf)["QSE_SANDBOX_MEM_GB"] == "3", "written once, kept after"
    return "ssh, chat here, the start script, chatd.env once"


def test_container_build(tmp_path):
    if os.environ.get("TANDEM_TEST_DOCKER") != "1" or shutil.which("docker") is None:
        raise Skip("TANDEM_TEST_DOCKER=1 and docker run this")
    tag = f"tandemllm-app:test-{os.getpid()}"
    name = f"tandem-app-test-{os.getpid()}"
    port = free_port()
    r = subprocess.run(["docker", "build", "-q", "-f", "packaging/Dockerfile.app", "-t", tag, "."],
                       cwd=ROOT, capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stderr[-3000:]
    try:
        r = subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080",
                            tag], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

        def up():
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                c.request("GET", "/health")
                return c.getresponse().status == 200
            except OSError:
                return False
        assert wait_for(up, 60), subprocess.run(["docker", "logs", name], capture_output=True,
                                                text=True).stdout[-2000:]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path)
            return json.loads(c.getresponse().read())
        assert get("/v1/fleet/app")["setup"]["needs_setup"] is True
        h = get("/v1/fleet/health/controller")
        assert any(f["code"] == "sandbox_off" and f["facts"]["path"] == "/run/tandem/sandbox.sock"
                   for f in h["findings"]), h["findings"]
        r = subprocess.run(["docker", "exec", name, "ssh", "-V"], capture_output=True, text=True)
        assert r.returncode == 0 and "OpenSSH" in r.stderr, r
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "rmi", tag], capture_output=True)
    return "built, /health, the wizard flag, sandboxes off, ssh inside"


def test_venv_from_the_index(tmp_path):
    if os.environ.get("TANDEM_INSTALL_NET") != "1":
        raise Skip("TANDEM_INSTALL_NET=1 runs this (it installs from the package index)")
    box = Box(str(tmp_path))
    try:
        out = ok_run(box.install("--port", str(box.port), "--service", "process", venv=False,
                                 timeout=900))
        assert "making the venv" in out and box.json("/health")["version"] == box.version, out
        venvs = os.listdir(os.path.join(box.prefix, "venvs"))
        assert len(venvs) == 1 and venvs[0].startswith(f"py{sys.version_info[0]}."), venvs
        out = ok_run(box.install("--service", "process", venv=False))
        assert "(there already)" in out, out
    finally:
        box.stop()
    return "a venv from the index, reused"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
