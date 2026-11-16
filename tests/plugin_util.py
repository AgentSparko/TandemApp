"""What the plugin tests share (SRV-857): plugin folders made from the examples, wheels built with
zipfile (never pip), git repositories made with the git binary, and a registry over a temporary
config database. Nothing here runs a plugin."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
EX = os.path.join(ROOT, "contracts", "plugin-v1", "examples")
VERSIONS = {"tandemapp": "0.15.0", "hooks_api": "1.0"}
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def example(name: str) -> dict:
    with open(os.path.join(EX, name, "plugin.json")) as f:
        return json.load(f)


def write_tree(d: str, files: dict) -> str:
    for rel, body in files.items():
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w" if isinstance(body, str) else "wb") as f:
            f.write(body)
    return d


def folder(name: str = "hello-tool", manifest: dict | None = None, extra: dict | None = None,
           base: str | None = None) -> str:
    """A plugin folder: the example `name` (its files), its manifest replaced or changed."""
    d = tempfile.mkdtemp(prefix=f"plugin-{name}-", dir=base)
    src = os.path.join(EX, name)
    for n in os.listdir(src):
        shutil.copyfile(os.path.join(src, n), os.path.join(d, n))
    if manifest is not None:
        with open(os.path.join(d, "plugin.json"), "w") as f:
            json.dump(manifest, f, indent=1)
    write_tree(d, extra or {})
    return d


def bumped(name: str, version: str, change=None) -> dict:
    m = copy.deepcopy(example(name))
    m["version"] = version
    if change:
        change(m)
    return m


def _record_line(name: str, data: bytes) -> str:
    h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
    return f"{name},sha256={h},{len(data)}"


def wheel(path: str, files: dict, record: bool = True, bad_hash: str | None = None,
          symlink: str | None = None) -> str:
    """A wheel at `path` with `files` {member: text or bytes}; a dist-info RECORD with their
    hashes (`bad_hash`: that member's hash is wrong); `symlink`: one member stored as a link."""
    dist = "hello_tool-0.1.0.dist-info"
    members = dict(files)
    members[f"{dist}/WHEEL"] = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    members[f"{dist}/METADATA"] = "Metadata-Version: 2.1\nName: hello-tool\nVersion: 0.1.0\n"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        lines = []
        for name, body in members.items():
            data = body.encode() if isinstance(body, str) else body
            z.writestr(name, data)
            lines.append(_record_line(name, b"tampered" if name == bad_hash else data))
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/hostname")
        if record:
            lines.append(f"{dist}/RECORD,,")
            z.writestr(f"{dist}/RECORD", "\n".join(lines) + "\n")
    return path


def example_files(name: str, under: str = "") -> dict:
    out = {}
    src = os.path.join(EX, name)
    for n in os.listdir(src):
        with open(os.path.join(src, n), "rb") as f:
            out[(under + "/" if under else "") + n] = f.read()
    return out


def git(repo: str, *args: str, input: bytes | None = None) -> str:
    p = subprocess.run(["git", *GIT_ID, "-C", repo, *args], capture_output=True, input=input,
                       timeout=60, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                                        "GIT_CONFIG_GLOBAL": os.devnull})
    assert p.returncode == 0, (args, p.stderr.decode()[-500:])
    return p.stdout.decode().strip()


def repo(files: dict) -> tuple[str, str]:
    """(a git repository with `files` in one commit on main, that commit's id)."""
    d = tempfile.mkdtemp(prefix="plugin-repo-")
    git(d, "init", "-q")
    write_tree(d, files)
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "one")
    return d, git(d, "rev-parse", "HEAD")


def commit(d: str, files: dict, message: str = "next") -> str:
    write_tree(d, files)
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", message)
    return git(d, "rev-parse", "HEAD")


class Reg:
    """A config database, the hook registry (for references) and the plugin registry."""

    def __init__(self, label: str, **versions):
        from proxy import config as config_mod
        from proxy import hooks as hooks_mod
        from proxy import pluginstore
        self.dir = tempfile.mkdtemp(prefix=f"plugin-reg-{label}-")
        self.cfg = config_mod.ConfigDB(os.path.join(self.dir, "config.sqlite3"), env={},
                                       use_keychain=False).open()
        self.runner = hooks_mod.Runner(self.cfg)
        self.logs: list[str] = []
        self.store = pluginstore.PluginStore(self.cfg, hooks=self.runner.store,
                                             log=self.logs.append, **{**VERSIONS, **versions})

    def install(self, path: str, **kw):
        return self.store.install({"type": "folder", "path": path}, who="admin", **kw)

    def refer(self, hid: str, pid_target: str, kind: str = "plugin") -> None:
        """A hook row that names a plugin (the registry refuses kind plugin until F2: written
        directly, as F2's registry would)."""
        self.runner.store.ready()
        with self.cfg._lock:
            self.cfg._conn.execute(
                "INSERT INTO hooks(id, name, kind, stages, role, target, enabled, created_at, "
                "updated_at) VALUES (?, ?, ?, '[\"request.finished\"]', 'observer', ?, 1, 0, 0)",
                (hid, hid, kind, pid_target))
            self.cfg._conn.commit()

    def audit(self) -> list[dict]:
        import sqlite3
        con = sqlite3.connect(self.cfg.path)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id")]
        con.close()
        return rows

    def close(self) -> None:
        self.runner.stop()
        self.cfg.close()
