"""TandemApp in one command (OPS-282): `bash packaging/install.sh [options]` runs this with the
host's Python 3.11 or newer. Standard library only; it imports nothing of the tree but the page's
bundle check (tandemapp/bundle.py) and the health page's memory check (proxy/fleethealth.py).

WHAT IT DOES, in order (each step says what it did; `--dry-run` says what it would do):

  1  the page: dashboard/dist must be an intact bundle (its bundle.json matches the files). A
     release archive carries one (tools/app_release.py pack), so the host needs no Node. Without
     one: --bundle-url URL --bundle-sha256 HEX fetches a released bundle, --build-page builds it
     with Node; else it stops and says so.
  2  the release: the tree's files (tools/app_release.py's list: no tests, no page sources) are
     copied to <prefix>/releases/<version>-<12 hex of their hash>, once; a release that is there
     already is used as it is.
  3  the venv: <prefix>/venvs/py<X.Y>-<12 hex>, made with this Python (`-m venv`) and the
     dependencies of pyproject.toml's `app` extra (cryptography, psutil; never torch); one venv
     serves every release with the same Python and dependencies. --venv DIR uses one that is there,
     --wheels DIR installs offline from a folder of wheels. The release's `venv` links to it.
  4  the state dir (--state-dir, default ~/.tandemllm, mode 0700): a fresh master key
     (secrets.key, 32 random bytes, mode 0600) unless one is there or app.env names
     TANDEM_SECRETS_KEY; app.env (mode 0600) on the first run with the address and port and
     nothing else running (OPS-288, bare: TANDEM_CHAT=off, TANDEM_CONDUCTOR=off, no TandemNode
     agent; Chat and the conductor are switched on in Settings, App, What runs here, or with
     --chat own); later runs change only the address, port and --chat they are given. An address that is not loopback (--listen) gets an admin
     token (TANDEM_ADMIN_TOKEN, never printed) and the page's sign-in, unless an admin password or
     token is set already. With TANDEM_AUTH=accounts (app.env or the environment; SEC-26) no
     token is written: the summary prints a one-time setup code that makes the first owner. An
     address that is not loopback, and --behind-proxy, write TANDEM_LOCAL_MODE=off, so this
     machine's browser is never trusted without a password. chatd.env (2 GiB and 2 running
     sandboxes) when there is none: read only once Chat is on.
  5  --with-sandbox (Linux, with sudo): ops/sandbox/install.sh, rootless Podman under its own user
     with its socket, egress table and the ACLs on chatd's folders; chatd.env gets its socket.
     It switches nothing on: the sandboxes are used once Chat is on.
     Then the memory cgroup is checked: when the kernel does not cap a container's memory, the
     fix is printed (on a Raspberry Pi, cgroup_enable=memory in cmdline.txt); no boot file is
     edited here.
  6  <prefix>/current points to the release, and the service (--service, auto by default):
       systemd   Linux with a user manager: ~/.config/systemd/user/<--unit, tandem-app>.service
                 (packaging/linux/tandem-app.service with this prefix and state dir), enabled and
                 restarted; a user without linger is told the one command that keeps it after logout
       launchd   macOS: ~/Library/LaunchAgents/<--launchd-label, org.tandemllm.app>.plist, booted
                 out and in
       process   no service manager (a container, no user bus): a background process, its pid in
                 <state>/tandem-app.pid, its output in <state>/logs/tandem-app.log; not started at
                 boot (the summary says so)
       none      nothing is started; the summary names the command
     Every manager runs packaging/linux/tandem-app.sh of <prefix>/current (C1's launcher).
  7  /health on the address and port must answer with this release's version and page within
     --wait seconds. Else an update switches back to the release before and restarts it, and the
     run fails with the log's last lines.

Before any of it: a unit or launchd job of that name that runs another prefix or state dir, or a
port another listener holds (not this install's own controller), stops the run (exit 2) with
nothing changed. Two installs on one host need their own --unit, --state-dir and --port.

A re-run with the same release changes nothing that runs while it answers healthy (an unchanged
unit or plist, the same release): idempotent. Old releases stay; the summary says how many.

    bash packaging/install.sh [--prefix DIR] [--state-dir DIR] [--listen ADDR] [--port N]
        [--behind-proxy]
        [--service auto|systemd|launchd|process|none] [--unit NAME] [--launchd-label LABEL]
        [--chat off|own|external]
        [--with-sandbox] [--venv DIR]
        [--wheels DIR] [--bundle-url URL --bundle-sha256 HEX | --build-page] [--python PY]
        [--wait S] [--dry-run]

Exit 0 running (or planned), 1 a step failed, 2 a usage error or an unsupported host.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import ipaddress
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

UNIT = "tandem-app"                     # --unit
LABEL = "org.tandemllm.app"             # --launchd-label
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
LAUNCHER_TAIL = os.path.join("current", "packaging", "linux", "tandem-app.sh")
DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
MANAGERS = ("auto", "systemd", "launchd", "process", "none")
#: paths a release leaves out (tools/app_release.py's list): tests, the page's sources, caches
EXCLUDE_TOP = {".git", ".github", "tests", "bench", "node_modules", "build", "dist", ".venv",
               "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDE_ANY = {"__pycache__", ".DS_Store", "node_modules", ".pytest_cache"}
SUFFIX_SKIP = (".pyc", ".pyo", ".egg-info")
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}
SECRET_ENV = "TANDEM_SECRETS_KEY"


class Stop(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message, self.code = message, code


def say(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


# ======================================================================================== the tree
def release_files(root: str) -> list[str]:
    """The release's files, relative to `root` and sorted: every file but tests, the page's
    sources (dashboard/ except dist/), caches and links."""
    out = []
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        parts = [] if rel == "." else rel.split(os.sep)
        keep = []
        for name in dirs:
            p = parts + [name]
            if (len(p) == 1 and name in EXCLUDE_TOP) or name in EXCLUDE_ANY or \
                    name.endswith(SUFFIX_SKIP):
                continue
            if p[0] == "dashboard" and len(p) >= 2 and p[1] != "dist":
                continue
            if os.path.islink(os.path.join(d, name)):
                continue
            keep.append(name)
        dirs[:] = sorted(keep)
        for name in files:
            p = parts + [name]
            if name in EXCLUDE_ANY or name.endswith(SUFFIX_SKIP) or \
                    (len(p) == 1 and (name in EXCLUDE_TOP or name.startswith(".git"))):
                continue
            if p[0] == "dashboard" and (len(p) == 2 or p[1] != "dist"):
                continue
            full = os.path.join(d, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            out.append("/".join(p))
    return sorted(out)


def tree_hash(root: str, files: list[str]) -> str:
    h = hashlib.sha256()
    for rel in files:
        fh = hashlib.sha256()
        with open(os.path.join(root, rel), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                fh.update(chunk)
        mode = "x" if os.access(os.path.join(root, rel), os.X_OK) else "-"
        h.update(f"{rel}\0{mode}\0{fh.hexdigest()}\n".encode())
    return h.hexdigest()


def read_version(root: str) -> str:
    try:
        with open(os.path.join(root, "VERSION")) as f:
            v = f.read().strip()
    except OSError:
        raise Stop(f"{root} has no VERSION file: not a TandemApp tree", 2) from None
    if not v:
        raise Stop(f"{root}/VERSION is empty", 2)
    return v


def release_id(version: str, digest: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", version) + "-" + digest[:12]


def copy_release(root: str, files: list[str], dest: str) -> None:
    """The files into `dest` through a folder of its own beside it, then one rename."""
    tmp = f"{dest}.part-{os.getpid()}"
    for rel in files:
        src, dst = os.path.join(root, rel), os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    os.replace(tmp, dest)


def deps_of(root: str) -> list[str]:
    """pyproject.toml's dependencies plus its `app` extra (psutil): what the controller needs."""
    import tomllib
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        doc = tomllib.load(f)
    proj = doc.get("project") or {}
    return list(proj.get("dependencies") or []) + \
        list((proj.get("optional-dependencies") or {}).get("app") or [])


# ======================================================================================== the page
def bundle_mod(root: str):
    sys.path.insert(0, root)
    try:
        from tandemapp import bundle
    finally:
        sys.path.remove(root)
    return bundle


def ensure_page(root: str, a, dry: bool) -> dict:
    """The page's manifest (step 1); Stop with what to do when there is none."""
    bundle = bundle_mod(root)
    dist = os.path.join(root, "dashboard", "dist")
    if os.path.isfile(os.path.join(dist, "index.html")):
        v = bundle.verify(dist)
        if v["ok"]:
            return bundle.read_manifest(dist) or {}
        if not a.bundle_url and not a.build_page:
            raise Stop("dashboard/dist does not match its bundle.json (" + "; ".join(v["problems"])
                       + "): take the release archive again, or rebuild the page "
                         "(--build-page)")
    if dry:
        say("would get the page: " + ("fetch " + a.bundle_url if a.bundle_url else
                                      "build it with Node" if a.build_page else "nothing: none"))
        return {}
    if a.bundle_url:
        if not a.bundle_sha256:
            raise Stop("--bundle-url needs --bundle-sha256, the archive's sha256", 2)
        try:
            return bundle.fetch(a.bundle_url, a.bundle_sha256, dist)
        except bundle.BundleError as exc:
            raise Stop(f"the page's bundle could not be fetched: {exc}") from None
    if a.build_page:
        rc = subprocess.call([sys.executable, os.path.join(root, "tools", "dashboard_bundle.py"),
                              "build"])
        if rc != 0:
            raise Stop("the page's build failed (above)")
        return bundle.read_manifest(dist) or {}
    raise Stop("this tree has no built page (dashboard/dist). Take a release archive (it carries "
               "one), or fetch one (--bundle-url URL --bundle-sha256 HEX), or build it with Node "
               "20.19 or newer (--build-page)")


# ======================================================================================== the venv
def venv_python(d: str) -> str:
    return os.path.join(d, "bin", "python")


def venv_ok(py: str) -> bool:
    if not os.path.exists(py):
        return False
    r = subprocess.run([py, "-c", "import sys, cryptography, psutil; "
                        "sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
                       capture_output=True)
    return r.returncode == 0


def ensure_venv(prefix: str, deps: list[str], a, dry: bool) -> str:
    """The venv's folder (step 3)."""
    if a.venv:
        d = os.path.abspath(os.path.expanduser(a.venv))
        if not venv_ok(venv_python(d)):
            raise Stop(f"--venv {d}: its bin/python is not Python 3.11 or newer with cryptography "
                       f"and psutil", 2)
        say(f"venv: {d} (given)")
        return d
    tag = f"py{sys.version_info[0]}.{sys.version_info[1]}"
    key = hashlib.sha256(json.dumps([sorted(deps), sys.version, platform.machine()]).encode())
    d = os.path.join(prefix, "venvs", f"{tag}-{key.hexdigest()[:12]}")
    if venv_ok(venv_python(d)):
        say(f"venv: {d} (there already)")
        return d
    if dry:
        say(f"would make the venv {d} with {sys.executable} and {', '.join(deps)}")
        return d
    tmp = f"{d}.part-{os.getpid()}"
    os.makedirs(os.path.dirname(d), exist_ok=True)
    say(f"making the venv {d} ({sys.executable})")
    r = subprocess.run([sys.executable, "-m", "venv", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        hint = (" (Debian, Ubuntu, Raspberry Pi OS: sudo apt install python3-venv)"
                if "ensurepip" in (r.stdout + r.stderr) else "")
        raise Stop(f"python -m venv failed{hint}: {(r.stderr or r.stdout).strip()[-300:]}")
    pip = [venv_python(tmp), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if a.wheels:
        pip += ["--no-index", "--find-links", os.path.abspath(os.path.expanduser(a.wheels))]
    r = subprocess.run(pip + deps)
    if r.returncode != 0:
        raise Stop(f"pip could not install {', '.join(deps)} (above); the half-made venv is "
                   f"{tmp}: remove it by hand")
    os.replace(tmp, d)
    return d


# ======================================================================================== the state
def read_env(path: str) -> dict:
    out: dict = {}
    try:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("export "):
                    s = s[7:].lstrip()
                k, sep, v = s.partition("=")
                if sep:
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]
                    out[k.strip()] = v
    except FileNotFoundError:
        pass
    return out


def write_private(path: str, text: str) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_env(path: str, values: dict) -> bool:
    """Each KEY=VALUE of `values` in the env file: its line replaced, else appended. True when
    the file changed."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    left = dict(values)
    out = []
    for line in lines:
        s = line.strip()
        k = s[7:].lstrip().partition("=")[0] if s.startswith("export ") else s.partition("=")[0]
        k = k.strip()
        if not s.startswith("#") and k in left:
            out.append(f"{k}={left.pop(k)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items()]
    text = "\n".join(out) + "\n"
    if lines and text == "\n".join(lines) + "\n":
        return False
    write_private(path, text)
    return True


def loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_listen(host: str) -> str:
    if host in ("0.0.0.0", "::", "localhost"):
        return host
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        raise Stop(f"--listen {host}: an IP address (127.0.0.1, 0.0.0.0, ::, or one of this "
                   f"host's)", 2) from None


CHAT_LINE = {
    "off": "# Chat is off (no chatd): an admin switches it on in Settings, App, What runs here;\n"
           "# from then on that switch wins over this line\n",
    "own": "# chatd on this host, started and watched by the controller (<state dir>/chatd.env)\n",
    "external": "# chatd runs elsewhere: the Chat tab forwards to QSE_CHAT_URL\n",
}


def app_env_text(host: str, port: int, chat: str) -> str:
    return (f"# TandemApp's controller (packaging/install.sh, {time.strftime('%Y-%m-%d')}): one "
            f"KEY=VALUE a line, mode 0600.\n"
            f"# packaging/app.env.example lists every variable; run the installer again with "
            f"--listen or --port to move it.\n"
            f"TANDEM_HOST={host}\nTANDEM_PORT={port}\n"
            f"{CHAT_LINE[chat]}"
            f"TANDEM_CHAT={chat}\n"
            f"# a bare engine: the conductor is switched on in Settings, App, What runs here\n"
            f"TANDEM_CONDUCTOR=off\n"
            f"# no TandemNode agent on this machine\n"
            f"TANDEM_NODE_AGENT=off\n")


CHATD_ENV = ("# chatd on TandemApp's host (packaging/install.sh): packaging/linux/chatd.env.example "
             "lists the rest.\n# 2 GiB and 2 running sandboxes on a host with no engine; "
             "QSE_SANDBOX_SOCKET names the container\n# runtime's socket (--with-sandbox writes "
             "it).\nQSE_SANDBOX_MEM_GB=2\nQSE_SANDBOX_MAX_RUNNING=2\n")


def address_of(a, cur: dict) -> tuple[str, int]:
    """(host, port) the controller listens on: the options, else app.env's, else the defaults."""
    host = check_listen(a.listen) if a.listen else cur.get("TANDEM_HOST") or DEFAULT_HOST
    try:
        port = int(a.port if a.port is not None else cur.get("TANDEM_PORT") or DEFAULT_PORT)
    except ValueError:
        raise Stop("TANDEM_PORT in app.env is not a number", 2) from None
    if not 1 <= port <= 65535:
        raise Stop(f"--port {port}: 1 to 65535", 2)
    return host, port


def ensure_state(state: str, a, dry: bool) -> dict:
    """Step 4: {host, port, token, changed, new} of the controller's app.env."""
    envf = os.path.join(state, "app.env")
    cur = read_env(envf)
    new = not os.path.exists(envf)
    host, port = address_of(a, cur)
    lan = not loopback(host)
    has_admin = bool(cur.get("TANDEM_ADMIN_TOKEN") or cur.get("QSE_ADMIN_TOKEN")) or \
        os.path.exists(os.path.join(state, "admin.json"))
    accounts = accounts_on(cur)
    no_local = lan or bool(getattr(a, "behind_proxy", False))
    if dry:
        say(f"would keep the state in {state}: " + ("a new app.env" if new else "app.env there")
            + f", {host}:{port}"
            + (", a new admin token" if lan and not has_admin and not accounts else "")
            + (", TANDEM_LOCAL_MODE=off" if no_local and cur.get(LOCAL_ENV) != "off" else ""))
        return {"host": host, "port": port, "token": cur.get("TANDEM_ADMIN_TOKEN"),
                "changed": new, "new": new, "lan": lan, "accounts": accounts}
    os.makedirs(state, mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    os.makedirs(os.path.join(state, "logs"), mode=0o700, exist_ok=True)
    changed = False
    if new:
        write_private(envf, app_env_text(host, port, a.chat or "off"))
        changed = True
        say(f"wrote {envf}")
    else:
        want = {}
        if a.listen and cur.get("TANDEM_HOST") != host:
            want["TANDEM_HOST"] = host
        if a.port is not None and cur.get("TANDEM_PORT") != str(port):
            want["TANDEM_PORT"] = str(port)
        if a.chat and cur.get("TANDEM_CHAT") != a.chat:
            want["TANDEM_CHAT"] = a.chat
        if want and set_env(envf, want):
            changed = True
            say(f"app.env: {', '.join(f'{k}={v}' for k, v in want.items())}")
    token = read_env(envf).get("TANDEM_ADMIN_TOKEN")
    if accounts and read_env(envf).get("TANDEM_AUTH") != "accounts":
        set_env(envf, {"TANDEM_AUTH": "accounts"})          # the service starts with it too
        changed = True
    # SEC-26: an address beyond loopback, or a proxy in front, never gets local mode
    if no_local and read_env(envf).get(LOCAL_ENV) != "off":
        set_env(envf, {LOCAL_ENV: "off"})
        changed = True
        say(f"app.env: {LOCAL_ENV}=off (" + ("a proxy is in front" if not lan else
                                              f"{host} is not loopback") + ")")
    if lan and not has_admin and not accounts:
        token = secrets.token_urlsafe(32)
        set_env(envf, {"TANDEM_ADMIN_TOKEN": token, "TANDEM_DASHBOARD_LOGIN": "on"})
        changed = True
        say(f"{host} is not loopback: an admin token was written to {envf} "
            f"(TANDEM_ADMIN_TOKEN) and the page asks for it")
    # the master key: the environment's, else secrets.key (proxy/secrets.py), made once here
    keyf = os.path.join(state, "secrets.key")
    if not read_env(envf).get(SECRET_ENV) and not os.path.exists(keyf):
        fd = os.open(keyf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
        say(f"a fresh master key: {keyf}")
    chatf = os.path.join(state, "chatd.env")
    if not os.path.exists(chatf):
        write_private(chatf, CHATD_ENV)
    return {"host": host, "port": port, "token": token, "changed": changed, "new": new,
            "lan": lan, "accounts": accounts}


LOCAL_ENV = "TANDEM_LOCAL_MODE"


def accounts_on(cur: dict) -> bool:
    """TANDEM_AUTH=accounts in app.env or this run's environment (the new sign-in, SEC-26)."""
    v = os.environ.get("TANDEM_AUTH") or cur.get("TANDEM_AUTH") or ""
    return v.strip().lower() == "accounts"


def setup_code(rel: str, state: str) -> str | None:
    """A one-time setup code from the release's own CLI while no owner exists, else None."""
    py = os.path.join(rel, "venv", "bin", "python")
    r = subprocess.run([py, os.path.join(rel, "proxy", "__main__.py"), "users", "setup-code",
                        "--config-db", os.path.join(state, "config.sqlite3")],
                       capture_output=True, text=True, check=False, timeout=60)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("setup code: "):
            return line.split(": ", 1)[1].strip()
    return None


# ======================================================================================== the sandbox
def sandbox(release: str, state: str, a, dry: bool) -> None:
    """Step 5 (Linux only): rootless Podman for chatd's sandboxes, then the memory check."""
    if not sys.platform.startswith("linux"):
        raise Stop("--with-sandbox is for Linux: on macOS give chatd Docker Desktop's socket in "
                   "chatd.env (QSE_SANDBOX_SOCKET)", 2)
    data = read_env(os.path.join(state, "chatd.env")).get("QSE_CHAT_DATA") or \
        os.path.join(state, "chat")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["sudo", "bash", os.path.join(release, "ops", "sandbox", "install.sh"),
           "--owner", user, "--data", data]
    if dry:
        say("would run: " + shlex.join(cmd))
        return
    if shutil.which("sudo") is None:
        raise Stop("--with-sandbox needs sudo once (rootless Podman under its own user); run "
                   "as root: " + shlex.join(cmd[1:]))
    say("rootless Podman for the sandboxes (sudo asks once): " + shlex.join(cmd))
    out = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    sys.stdout.write(out.stdout)
    if out.returncode != 0:
        raise Stop("ops/sandbox/install.sh failed (above)")
    m = re.search(r"^QSE_SANDBOX_SOCKET=(\S+)$", out.stdout, re.M)
    if not m:
        raise Stop("ops/sandbox/install.sh named no socket")
    set_env(os.path.join(state, "chatd.env"), {"QSE_SANDBOX_SOCKET": m.group(1)})
    say(f"chatd.env: QSE_SANDBOX_SOCKET={m.group(1)}")
    memory_note(release, m.group(1))


def memory_note(release: str, sock: str | None, root: str = "/sys/fs/cgroup") -> bool:
    """Prints the fix when the kernel does not cap a container's memory (the health page's own
    check and words); True when it does."""
    sys.path.insert(0, release)
    try:
        from proxy import enginecodes, fleethealth
    finally:
        sys.path.remove(release)
    ok, why = fleethealth.memory_cgroup(sock, root)
    if not ok:
        say(f"WARNING: {enginecodes.CATALOG['no_memory_cgroup'][1]} ({why})")
        say(f"  {enginecodes.CATALOG['no_memory_cgroup'][2]}")
        say("  nothing was edited here; the health page shows this until it is fixed")
    return ok


# ======================================================================================== the service
def manager_of(want: str, plat: str | None = None, which=shutil.which, call=None) -> str:
    """`auto`: launchd on macOS, systemd when this user's manager answers, else process."""
    if want != "auto":
        return want
    plat = plat or sys.platform
    if plat == "darwin":
        return "launchd"
    if which("systemctl"):
        call = call or (lambda argv: subprocess.run(argv, capture_output=True).returncode)
        if call(["systemctl", "--user", "show-environment"]) == 0:
            return "systemd"
    return "process"


def _q(path: str) -> str:
    return f'"{path}"' if re.search(r"\s", path) else path


def unit_text(template: str, prefix: str, state: str, unit: str = UNIT) -> str:
    """packaging/linux/tandem-app.service with this prefix and state dir."""
    with open(template) as f:
        text = f.read()
    launcher = os.path.join(prefix, "current", "packaging", "linux", "tandem-app.sh")
    py = os.path.join(prefix, "current", "venv", "bin", "python")
    head = (f"# written by packaging/install.sh: TandemApp's controller from {prefix}/current\n"
            f"# (run the installer again to update; systemctl --user disable --now {unit} stops it)\n")
    body = []
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            body.append(f"ExecStart=/bin/bash {_q(launcher)}")
        elif line.startswith("Environment=TANDEM_APP_STATE="):
            body.append(f'Environment="TANDEM_APP_STATE={state}"')
            body.append(f'Environment="TANDEM_APP_PYTHON={py}"')
        elif line.startswith("#"):
            continue
        else:
            body.append(line)
    return head + "\n".join(body).lstrip("\n") + "\n"


def plist_bytes(prefix: str, state: str, label: str = LABEL) -> bytes:
    launcher = os.path.join(prefix, "current", "packaging", "linux", "tandem-app.sh")
    log = os.path.join(state, "logs", "tandem-app.log")
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": ["/bin/bash", launcher],
        "EnvironmentVariables": {
            "TANDEM_APP_STATE": state,
            "TANDEM_APP_PYTHON": os.path.join(prefix, "current", "venv", "bin", "python"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Interactive",
        "StandardOutPath": log, "StandardErrorPath": log})


def service_file(mgr: str, unit: str, label: str, home: str | None = None) -> str | None:
    """Where the unit (systemd) or the plist (launchd) of this name lives; None for the others."""
    home = home or os.path.expanduser("~")
    if mgr == "systemd":
        return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                            "systemd", "user", f"{unit}.service")
    if mgr == "launchd":
        return os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    return None


def owner_of(mgr: str, path: str) -> tuple[str | None, str | None]:
    """(prefix, state dir) the unit or plist at `path` runs; None where it names none."""
    prefix = state = None
    if mgr == "launchd":
        try:
            with open(path, "rb") as f:
                doc = plistlib.load(f)
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None, None
        args = doc.get("ProgramArguments") or []
        launcher = args[-1] if args and isinstance(args[-1], str) else ""
        state = (doc.get("EnvironmentVariables") or {}).get("TANDEM_APP_STATE")
    else:
        launcher = ""
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            return None, None
        for line in lines:
            line = line.strip()
            try:
                if line.startswith("ExecStart="):
                    argv = shlex.split(line[len("ExecStart="):])
                    launcher = argv[-1] if argv else ""
                elif line.startswith("Environment="):
                    for kv in shlex.split(line[len("Environment="):]):
                        k, _, v = kv.partition("=")
                        if k == "TANDEM_APP_STATE":
                            state = v
            except ValueError:
                continue
    if launcher.endswith(os.sep + LAUNCHER_TAIL):
        prefix = launcher[:-len(LAUNCHER_TAIL) - 1]
    return prefix, state


def check_owner(mgr: str, unit: str, label: str, prefix: str, state: str,
                home: str | None = None) -> None:
    """A unit or launchd job of this name that runs another prefix or state dir is never
    overwritten: the run stops (exit 2) and names both."""
    path = service_file(mgr, unit, label, home)
    if path is None:
        return
    if not os.path.exists(path) and mgr == "systemd" and shutil.which("systemctl"):
        r = run(["systemctl", "--user", "show", "-p", "FragmentPath", "--value",
                 f"{unit}.service"], check=False)
        found = r.stdout.strip() if r.returncode == 0 else ""
        path = found if found and os.path.exists(found) else path
    if not os.path.exists(path):
        return
    p, s = owner_of(mgr, path)
    same = p is not None and s is not None and \
        os.path.realpath(p) == os.path.realpath(prefix) and \
        os.path.realpath(s) == os.path.realpath(state)
    if not same:
        what = f"the unit {unit}" if mgr == "systemd" else f"the launchd job {label}"
        flag = "--unit" if mgr == "systemd" else "--launchd-label"
        raise Stop(f"{what} ({path}) runs prefix {p or '?'}, state {s or '?'}; this install is "
                   f"prefix {prefix}, state {state}: name another with {flag} (nothing changed)",
                   2)


def port_taken(host: str, port: int) -> bool:
    """Something listens where the controller would: it answers a connect, or the bind fails."""
    h = "127.0.0.1" if host in ("0.0.0.0", "") else "::1" if host == "::" else host
    try:
        with socket.create_connection((h, port), timeout=1):
            return True
    except OSError:
        pass
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return False
    except OSError as exc:
        return exc.errno == errno.EADDRINUSE
    finally:
        s.close()


def own_running(mgr: str, unit: str, label: str, state: str) -> bool:
    """This install's controller runs (its unit, job or process; check_owner made sure a unit or
    job of this name is this install's)."""
    if mgr == "systemd":
        return run(["systemctl", "--user", "is-active", "--quiet", unit],
                   check=False).returncode == 0
    if mgr == "launchd":
        return run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                   check=False).returncode == 0
    if mgr == "process":
        return process_pid(state) is not None
    return False


def check_port(mgr: str, unit: str, label: str, state: str, host: str, port: int) -> None:
    """The port is free, or this install's own controller holds it; else the run stops (exit 2)."""
    if mgr == "none" or not port_taken(host, port):
        return
    envf = os.path.join(state, "app.env")
    if os.path.exists(envf):
        cur = read_env(envf)
        try:
            own_port = int(cur.get("TANDEM_PORT") or DEFAULT_PORT)
        except ValueError:
            own_port = None
        if own_port == port and own_running(mgr, unit, label, state):
            return
    raise Stop(f"port {port} on {host} is taken by another listener: pick another --port "
               f"(nothing changed)", 2)


def write_if_changed(path: str, data: bytes) -> bool:
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Stop(f"{shlex.join(cmd)} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_controller(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                           text=True)
        cmd = r.stdout
    return "-m proxy serve" in cmd or "tandem-app.sh" in cmd


def process_pid(state: str) -> int | None:
    try:
        with open(os.path.join(state, "tandem-app.pid")) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and _pid_alive(pid) and _is_controller(pid) else None


def stop_process(state: str) -> None:
    pid = process_pid(state)
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)
    t_end = time.monotonic() + 30
    while time.monotonic() < t_end and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    say(f"stopped the controller (pid {pid})")


def start_process(prefix: str, state: str) -> int:
    launcher = os.path.join(prefix, "current", "packaging", "linux", "tandem-app.sh")
    env = dict(os.environ, TANDEM_APP_STATE=state,
               TANDEM_APP_PYTHON=os.path.join(prefix, "current", "venv", "bin", "python"))
    log = open(os.path.join(state, "logs", "tandem-app.log"), "ab")
    p = subprocess.Popen(["/bin/bash", launcher], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, env=env, start_new_session=True,
                         cwd=os.path.join(prefix, "current"))
    log.close()
    write_private(os.path.join(state, "tandem-app.pid"), f"{p.pid}\n")
    return p.pid


def service(mgr: str, prefix: str, release: str, state: str, restart: bool, dry: bool,
            unit: str = UNIT, label: str = LABEL) -> dict:
    """Step 6's service: {manager, started, note}. The unit (systemd) is named `unit`, the
    launchd job `label`."""
    if mgr == "systemd":
        path = service_file(mgr, unit, label)
        text = unit_text(os.path.join(release, "packaging", "linux", "tandem-app.service"),
                         prefix, state, unit).encode()
        if dry:
            say(f"would write {path} and enable and restart {unit}")
            return {"manager": mgr, "started": False, "note": None}
        if write_if_changed(path, text):
            restart = True
            say(f"wrote {path}")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--quiet", unit])
        active = run(["systemctl", "--user", "is-active", "--quiet", unit], check=False)
        if restart or active.returncode != 0:
            run(["systemctl", "--user", "restart", unit])
            say(f"started the user unit {unit}")
        user = os.environ.get("USER") or ""
        r = run(["loginctl", "show-user", user, "--property=Linger"], check=False) \
            if shutil.which("loginctl") and user else None
        note = None
        if r is not None and r.stdout.strip() == "Linger=no":
            note = (f"the unit stops when {user} logs out; keep it running with: "
                    f"sudo loginctl enable-linger {user}")
        if unit != "tandemapp" and run(["systemctl", "--user", "is-active", "--quiet",
                                        "tandemapp"], check=False).returncode == 0:
            note = ((note + "; ") if note else "") + (
                "the unit tandemapp (`tandem-app service install`) runs too: remove it with "
                "`tandem-app service uninstall` so one controller keeps the port")
        return {"manager": mgr, "started": True, "note": note,
                "stop": f"systemctl --user disable --now {unit}"}
    if mgr == "launchd":
        path = service_file(mgr, unit, label)
        data = plist_bytes(prefix, state, label)
        if dry:
            say(f"would write {path} and load it")
            return {"manager": mgr, "started": False, "note": None}
        if write_if_changed(path, data):
            restart = True
            say(f"wrote {path}")
        dom = f"gui/{os.getuid()}"
        loaded = run(["launchctl", "print", f"{dom}/{label}"], check=False).returncode == 0
        if restart or not loaded:
            run(["launchctl", "bootout", f"{dom}/{label}"], check=False)
            run(["launchctl", "bootstrap", dom, path])
            say(f"loaded the launchd job {label}")
        return {"manager": mgr, "started": True, "note": None,
                "stop": f"launchctl bootout {dom}/{label}"}
    if mgr == "process":
        if dry:
            say("would start the controller in the background (no service manager here)")
            return {"manager": mgr, "started": False, "note": None}
        if restart or process_pid(state) is None:
            stop_process(state)
            pid = start_process(prefix, state)
            say(f"started the controller in the background (pid {pid})")
        return {"manager": mgr, "started": True,
                "note": "no service manager here: it runs until this host restarts; run the "
                        "installer again to start it then",
                "stop": f"kill $(cat {shlex.quote(os.path.join(state, 'tandem-app.pid'))})"}
    launcher = os.path.join(prefix, "current", "packaging", "linux", "tandem-app.sh")
    return {"manager": "none", "started": False,
            "note": f"nothing was started; run it with: TANDEM_APP_STATE={shlex.quote(state)} "
                    f"bash {shlex.quote(launcher)}"}


def health(host: str, port: int, token: str | None, version: str, bundle: str | None,
           wait: float, accounts: bool = False) -> tuple[bool, str]:
    """(answers as this release, what it said): /health, then /dashboard/'s bundle. With the
    accounts on (SEC-26) /health tells its version only to someone signed in: an answer without
    it counts when it says `ok` and the page's bundle is this release's."""
    h = "127.0.0.1" if host in ("0.0.0.0", "") else "::1" if host == "::" else host
    base = f"http://[{h}]:{port}" if ":" in h else f"http://{h}:{port}"
    t_end = time.monotonic() + wait
    last = "nothing answered"
    while True:
        try:
            rq = urllib.request.Request(base + "/health")
            if token:
                rq.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(rq, timeout=3) as r:
                doc = json.loads(r.read() or b"{}")
            got_v = doc.get("version")
            got_b = (doc.get("dashboard") or {}).get("sha256")
            if accounts and got_v is None and doc.get("status") == "ok":
                got_v, got_b = version, bundle
            if got_v == version and (bundle is None or got_b == bundle):
                with urllib.request.urlopen(base + "/dashboard/", timeout=3) as r:
                    tag = r.headers.get("X-Tandem-Bundle") or ""
                    if r.status == 200 and (bundle is None or tag == bundle[:16]):
                        return True, f"{base}/"
                    last = f"/dashboard/ answered {r.status} with bundle {tag or 'none'}"
            else:
                last = f"/health says version {got_v}, page {str(got_b)[:16]}"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = f"{type(exc).__name__}: {exc}"[:200]
        if time.monotonic() >= t_end:
            return False, last
        time.sleep(0.5)


def log_tail(state: str, mgr: str, n: int = 20, unit: str = UNIT) -> str:
    if mgr == "systemd":
        r = run(["journalctl", "--user", "-u", unit, "-n", str(n), "--no-pager"], check=False)
        return r.stdout
    try:
        with open(os.path.join(state, "logs", "tandem-app.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError:
        return ""


def switch(prefix: str, rid: str) -> str | None:
    """`current` -> releases/<rid>, in one rename; the release it pointed to before."""
    cur = os.path.join(prefix, "current")
    if os.path.exists(cur) and not os.path.islink(cur):
        raise Stop(f"{cur} is not a link this installer made: move it away, then run this again")
    before = os.readlink(cur) if os.path.islink(cur) else None
    tmp = f"{cur}.tmp-{os.getpid()}"
    os.symlink(os.path.join("releases", rid), tmp)
    os.replace(tmp, cur)
    return os.path.basename(before) if before else None


# ======================================================================================== main
def parser() -> argparse.ArgumentParser:
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local",
                                                           "share")
    ap = argparse.ArgumentParser(prog="packaging/install.sh",
                                 description="TandemApp in one command (OPS-282)")
    ap.add_argument("--source", required=True, help=argparse.SUPPRESS)
    ap.add_argument("--prefix", default=os.path.join(data, "tandemapp"),
                    help="where the releases and venvs go (default: %(default)s)")
    ap.add_argument("--state-dir", default=os.environ.get("TANDEM_APP_STATE") or "~/.tandemllm",
                    help="the controller's state: app.env, the databases, the master key "
                         "(default: %(default)s)")
    ap.add_argument("--listen", default=None,
                    help="the address it listens on (default 127.0.0.1, or app.env's); not "
                         "loopback makes an admin token")
    ap.add_argument("--port", type=int, default=None, help=f"default {DEFAULT_PORT}, or app.env's")
    ap.add_argument("--behind-proxy", action="store_true",
                    help="a reverse proxy or tunnel on this machine sends other devices here: "
                         "this machine's browser is never trusted without a password "
                         "(TANDEM_LOCAL_MODE=off)")
    ap.add_argument("--service", choices=MANAGERS, default="auto")
    ap.add_argument("--unit", default=UNIT,
                    help="the systemd user unit's name (default: %(default)s); a unit of this "
                         "name that runs another prefix or state dir stops the run")
    ap.add_argument("--launchd-label", default=LABEL,
                    help="the launchd job's label on macOS (default: %(default)s); the same rule")
    ap.add_argument("--chat", choices=("off", "own", "external"), default=None,
                    help="who runs chatd: off (a new app.env: no chat until it is switched on "
                         "from the page), own (this host) or external; a later run with it "
                         "changes app.env")
    ap.add_argument("--with-sandbox", action="store_true",
                    help="Linux: rootless Podman for chatd's sandboxes (sudo once)")
    ap.add_argument("--venv", default=None, help="use this venv (Python 3.11+, cryptography, "
                                                 "psutil) instead of making one")
    ap.add_argument("--wheels", default=None, help="install the venv offline from these wheels")
    ap.add_argument("--bundle-url", default=None)
    ap.add_argument("--bundle-sha256", default=None)
    ap.add_argument("--build-page", action="store_true")
    ap.add_argument("--python", default=None, help="the Python to use (install.sh reads it)")
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return install(a)
    except Stop as exc:
        say(f"STOPPED: {exc.message}")
        return exc.code
    except KeyboardInterrupt:
        say("STOPPED by the user")
        return 1


def install(a) -> int:
    dry = a.dry_run
    osname = "linux" if sys.platform.startswith("linux") else \
        "macos" if sys.platform == "darwin" else None
    arch = ARCHES.get(platform.machine().lower())
    if osname is None or arch is None:
        raise Stop(f"TandemApp installs on Linux and macOS, arm64 or x86_64; this is "
                   f"{sys.platform} {platform.machine()}", 2)
    mgr = manager_of(a.service)
    if mgr == "launchd" and osname != "macos" or mgr == "systemd" and osname != "linux":
        raise Stop(f"--service {mgr} does not run on {osname}", 2)
    if a.with_sandbox and osname != "linux":
        raise Stop("--with-sandbox is for Linux: on macOS give chatd Docker Desktop's socket in "
                   "chatd.env (QSE_SANDBOX_SOCKET)", 2)
    if a.listen:
        check_listen(a.listen)
    unit = a.unit[:-len(".service")] if a.unit.endswith(".service") else a.unit
    if not NAME_RE.fullmatch(unit):
        raise Stop(f"--unit {a.unit!r}: letters, digits and _ . @ - (at most 64)", 2)
    if not NAME_RE.fullmatch(a.launchd_label):
        raise Stop(f"--launchd-label {a.launchd_label!r}: letters, digits and _ . @ - "
                   "(at most 64)", 2)
    label = a.launchd_label
    src = os.path.abspath(a.source)
    prefix = os.path.abspath(os.path.expanduser(a.prefix))
    state = os.path.abspath(os.path.expanduser(a.state_dir))
    # nothing changes while a unit or job of this name, or the port, belongs to someone else
    check_owner(mgr, unit, label, prefix, state)
    host, port = address_of(a, read_env(os.path.join(state, "app.env")))
    check_port(mgr, unit, label, state, host, port)
    version = read_version(src)
    say(f"TandemApp {version} on {osname} {arch}, Python {sys.version.split()[0]}"
        + (" (dry run: nothing changes)" if dry else ""))
    manifest = ensure_page(src, a, dry)
    files = release_files(src)
    if "proxy/__main__.py" not in files or "packaging/linux/tandem-app.sh" not in files:
        raise Stop(f"{src} is not a TandemApp release tree", 2)
    rid = release_id(version, tree_hash(src, files))
    rel = os.path.join(prefix, "releases", rid)
    deps = deps_of(src)
    if os.path.isdir(rel):
        say(f"release {rid}: there already")
    elif dry:
        say(f"would copy {len(files)} files to {rel}")
    else:
        os.makedirs(os.path.dirname(rel), exist_ok=True)
        copy_release(src, files, rel)
        say(f"release {rid}: {len(files)} files")
    venv = ensure_venv(prefix, deps, a, dry)
    if not dry:
        link = os.path.join(rel, "venv")
        if os.path.realpath(link) != os.path.realpath(venv):
            tmp = f"{link}.tmp-{os.getpid()}"
            os.symlink(venv, tmp)
            os.replace(tmp, link)
    st = ensure_state(state, a, dry)
    if a.with_sandbox:
        sandbox(rel if os.path.isdir(rel) else src, state, a, dry)
    if dry:
        say(f"would point {prefix}/current at releases/{rid}")
        service(mgr, prefix, rel, state, True, dry=True, unit=unit, label=label)
        return 0
    before = switch(prefix, rid)
    changed = before != rid or st["changed"]
    if before and before != rid:
        say(f"current: releases/{before} -> releases/{rid}")
    svc = service(mgr, prefix, rel, state, changed, dry=False, unit=unit, label=label)
    bundle = manifest.get("sha256")
    if svc["started"]:
        ok, said = health(st["host"], st["port"], st["token"], version, bundle, a.wait,
                          accounts=st.get("accounts", False))
        if not ok:
            tail = log_tail(state, mgr, unit=unit)
            if before and before != rid and os.path.isdir(os.path.join(prefix, "releases", before)):
                switch(prefix, before)
                service(mgr, prefix, os.path.join(prefix, "releases", before), state, True,
                        False, unit=unit, label=label)
                say(f"releases/{rid} did not come up ({said}); switched back to releases/{before}")
            raise Stop(f"the controller did not answer as {version} within {a.wait:.0f} s "
                       f"({said}). Its log:\n{tail}")
        url = said
    else:
        url = None
    olds = sorted(set(os.listdir(os.path.join(prefix, "releases"))) - {rid})
    olds = [x for x in olds if ".part-" not in x]
    print()
    print(f"TandemApp {version} " + ("is running" if url else "is installed")
          + f" ({svc['manager']}).")
    if url:
        print(f"  Open {url}  (the first page sets up an engine)")
        code = setup_code(rel, state) if st.get("accounts") else None
        if code:
            where = "http://<this host's address>" if st["lan"] else url.rstrip("/")
            print(f"  Setup code: {code}  (single use, valid 24 hours: open {where}"
                  f"{':' + str(st['port']) if st['lan'] else ''}/ and make the owner with it; "
                  f"`tandem-app users setup-code` prints a new one)")
        elif st["lan"] and not st.get("accounts"):
            print(f"  From another machine: http://<this host's address>:{st['port']}/; sign in "
                  f"with TANDEM_ADMIN_TOKEN from {os.path.join(state, 'app.env')}")
    if (read_env(os.path.join(state, "app.env")).get("TANDEM_CHAT") or "external") == "off":
        print("  Runs:    the gateway only; Chat and the conductor are switched on in Settings, "
              "App" + (" (the sandboxes are ready for Chat)" if a.with_sandbox else ""))
    print(f"  State:   {state}  (app.env, the databases, the master key secrets.key)")
    print(f"  Release: {os.path.join(prefix, 'current')} -> releases/{rid}"
          + (f"  ({len(olds)} older kept)" if olds else ""))
    if svc.get("stop"):
        print(f"  Stop:    {svc['stop']}")
    if svc.get("note"):
        print(f"  Note:    {svc['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
