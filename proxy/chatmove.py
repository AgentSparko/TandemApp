"""Move chatd's data to the controller's host (SRV-843, the plan's step M3): `tandem-app chat-move`.

    tandem-app chat-move --from USER@HOST:PATH [--ssh-port N] [--state-dir ~/.tandemllm]
                         [--from-secrets FILE] [--plan] [--replace] [--no-start]
                         [--without-mcp-secrets]
    tandem-app chat-move --from /local/path/to/chat ...        (a folder on this host)

It runs on the new host, as the controller's user, beside a controller that runs chatd itself
(`--chat own`, proxy/appchat.py: the destination is that chatd's data folder, QSE_CHAT_DATA of
<state dir>/chatd.env, default <state dir>/chat). The source is the old chatd's data folder (on an
engine host: TandemNode's chatd, QSE_CHAT_DATA there), reached with the operator's own SSH login
(BatchMode, the user's ssh config; never the controller's engine key, which runs `tandem remote`
only) or on this host. In order:

  1  LOOK at the source (a small Python script over SSH, or here): every file's size and sha256,
     whether a chatd still runs on it (its chatd.pid names a live `-m chatd`), the MCP servers'
     names and transports, whether the master key beside it (`../secrets.key`) is there. A chatd
     that runs is refused: stop it first (`tandem-node chat stop`, or its unit), so the database
     is still and the open runs end `interrupted`. `--plan` stops here and prints the plan.
  2  STOP the chatd here when it runs (a hand stop: the controller's watcher holds it).
  3  COPY: `tar -C PATH -cf - .` on the source, read here into `<data>.moving-<stamp>` member by
     member: plain files, folders and links that stay inside the folder (tarfile's data filter);
     a socket, a device or a link that points outside is left out and counted. chatd.pid,
     sandbox-instance.json and the database's -shm index stay behind (this host's chatd makes its
     own; its containers are this host's). Nothing on the source is written or removed: the old chatd on its untouched
     data is the way back.
  4  CHECK: the source looked again (nothing changed while it was read), every file here equal to
     the source's by size and sha256, the database's `integrity_check` ok.
  5  SEAL AGAIN: the MCP servers' env and header secrets were sealed with the old host's master
     key (chatd/mcp.py, `../secrets.key` of the data folder, or TANDEM_SECRETS_KEY there): each
     is opened with it (`--from-secrets FILE`, else that file read over the same login, into
     memory only) and sealed with the key chatd here uses (`../secrets.key` of the destination,
     or TANDEM_SECRETS_KEY of chatd.env). Without the old key the move is refused, unless
     `--without-mcp-secrets` clears them (the servers stay; their secrets are entered again).
  6  PLACE: an existing destination with chats in it is refused unless `--replace`, which keeps it
     as `<data>.before-move-<stamp>` (never removed here); then the staged folder becomes the data
     folder.
  7  START the chatd here (`--no-start`: leave it to the watcher or a hand start). Runs the old
     chatd left open come back `interrupted`; Continue resumes them.

Every value of a secret (the old master key, an MCP server's env or header) stays in memory and
is never printed; the plan and the result name files, counts, servers and transports only.
Exit 0 when moved (or the plan printed), 1 when refused or failed, 2 for a bad call.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

DB = "chat.sqlite3"
#: what stays behind: this host's chatd makes its own (the database's shared-memory index too:
#: SQLite builds it again from the WAL that travels)
LEFT = ("chatd.pid", "sandbox-instance.json", "chat.sqlite3-shm")
#: a folder with any of these has chats in it (a replace is needed)
HAS_CHATS = (DB,)
USERHOST_RE = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}@)?"
                         r"(?:[A-Za-z0-9][A-Za-z0-9.-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\])$")
SSH_TIMEOUT_S = 10

#: the source's look, run with `python3 - PATH` there or here: one JSON document on stdout
LOOK_PY = r'''
import hashlib, json, os, sqlite3, stat, subprocess, sys
root = os.path.abspath(sys.argv[1])
out = {"root": root, "exists": os.path.isdir(root), "files": {}, "skipped": [], "running": False,
       "pid": None, "mcp": [], "secrets_key": os.path.isfile(os.path.join(os.path.dirname(root),
                                                                           "secrets.key"))}
if out["exists"]:
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(d, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out["files"][rel] = ["link", os.readlink(p)]
            elif stat.S_ISREG(st.st_mode):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                out["files"][rel] = ["file", st.st_size, h.hexdigest()]
            else:
                out["skipped"].append(rel)
    try:
        pid = int("".join(c for c in open(os.path.join(root, "chatd.pid")).read() if c.isdigit()))
    except (OSError, ValueError):
        pid = None
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        cmd = ""
        if alive:
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
            except OSError:
                try:
                    cmd = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                                         text=True, timeout=5).stdout
                except (OSError, subprocess.SubprocessError):
                    cmd = ""
        out["pid"] = pid
        out["running"] = alive and " -m chatd " in f" {cmd} "
    db = os.path.join(root, "chat.sqlite3")
    if os.path.isfile(db):
        import shutil, tempfile
        tmp = tempfile.mkdtemp(prefix="chat-look-")
        try:
            # a copy of the database and its WAL, read there: the source is never opened
            for x in ("chat.sqlite3", "chat.sqlite3-wal"):
                if os.path.isfile(os.path.join(root, x)):
                    shutil.copy2(os.path.join(root, x), os.path.join(tmp, x))
            c = sqlite3.connect(os.path.join(tmp, "chat.sqlite3"), timeout=5)
            cols = {r[1] for r in c.execute("PRAGMA table_info(mcp_servers)")}
            sel = "name, transport, " + ("env_enc IS NOT NULL" if "env_enc" in cols else "0") + \
                ", " + ("headers_enc IS NOT NULL" if "headers_enc" in cols else "0")
            out["mcp"] = [{"name": r[0], "transport": r[1], "sealed": bool(r[2] or r[3])}
                          for r in c.execute(f"SELECT {sel} FROM mcp_servers ORDER BY name")]
            c.close()
        except (sqlite3.Error, OSError) as exc:
            out["db_error"] = type(exc).__name__
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
print(json.dumps(out))
'''


class MoveError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message, self.code = message, code


class Source:
    """The old chatd's data folder: `user@host:/path` over SSH, or a path here."""

    def __init__(self, spec: str, port: int = 22, ssh: str = "ssh"):
        self.ssh_bin, self.port = ssh, port
        host, sep, path = spec.partition(":") if not spec.startswith("/") and ":" in spec \
            else ("", "", spec)
        if sep:
            if not USERHOST_RE.match(host) or host.startswith("-"):
                raise MoveError("--from: USER@HOST:PATH (a login name and a host name)", 2)
            self.host = host
        else:
            self.host = None
            path = os.path.expanduser(path)
        if not path.startswith("/") or "\0" in path or "\n" in path:
            raise MoveError("--from: the data folder is an absolute path", 2)
        self.path = os.path.normpath(path)

    def __str__(self) -> str:
        return f"{self.host}:{self.path}" if self.host else self.path

    def argv(self, cmd: list[str]) -> list[str]:
        """`cmd` on the source's host: an argv here, or ssh with it as one quoted command."""
        if self.host is None:
            return cmd
        return [self.ssh_bin, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT_S}",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
                "-T", "-p", str(self.port), "--", self.host, shlex.join(cmd)]

    def look(self) -> dict:
        py = "python3" if self.host else sys.executable
        try:
            r = subprocess.run(self.argv([py, "-I", "-", self.path]), input=LOOK_PY.encode(),
                               capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MoveError(f"the source cannot be read ({type(exc).__name__})") from None
        if r.returncode != 0:
            last = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
            raise MoveError(f"the source cannot be read (exit {r.returncode}: {last[0][:200]})")
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise MoveError("the source's look is not JSON") from None

    def master_key(self) -> str | None:
        """The text of `../secrets.key` beside the data folder, into memory; None without it."""
        path = os.path.join(os.path.dirname(self.path), "secrets.key")
        if self.host is None:
            try:
                with open(path) as f:
                    return f.read().strip() or None
            except OSError:
                return None
        try:
            r = subprocess.run(self.argv(["cat", "--", path]), capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        text = r.stdout.decode(errors="replace").strip()
        return text if r.returncode == 0 and text else None

    def tar(self) -> subprocess.Popen:
        return subprocess.Popen(self.argv(["tar", "-C", self.path, "-cf", "-", "."]),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=dict(os.environ, COPYFILE_DISABLE="1"))


def _vault(text: str, where: str):
    from proxy import secrets as vault_mod
    return vault_mod.Vault(vault_mod._decode_master(text, where), "move")


def _safe(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """The member as it may be written under `dest`, or None (left out)."""
    if hasattr(tarfile, "data_filter"):
        try:
            return tarfile.data_filter(member, dest)
        except tarfile.FilterError:
            return None
    name = os.path.normpath(member.name)
    if name.startswith(("/", "..")) or not (member.isreg() or member.isdir() or member.issym()):
        return None
    if member.issym():
        target = os.path.normpath(os.path.join(os.path.dirname(name), member.linkname))
        if os.path.isabs(member.linkname) or target.startswith(".."):
            return None
    return member


def copy(src: Source, dest: str) -> dict:
    """The source's tar into `dest` (a new folder); {members, bytes, left_out}."""
    os.makedirs(dest, mode=0o700)
    p = src.tar()
    n, size, left = 0, 0, []
    try:
        with tarfile.open(fileobj=p.stdout, mode="r|*") as tf:
            for m in tf:
                rel = os.path.normpath(m.name)
                if rel == ".":
                    continue
                if rel in LEFT:
                    left.append(rel)
                    continue
                ok = _safe(m, dest)
                if ok is None:
                    left.append(rel)
                    continue
                tf.extract(ok, dest, set_attrs=True, **({"filter": "fully_trusted"} if hasattr(
                    tarfile, "data_filter") else {}))
                n += 1
                size += m.size if m.isreg() else 0
    except (tarfile.TarError, OSError) as exc:
        p.kill()
        raise MoveError(f"the copy failed ({type(exc).__name__})") from None
    finally:
        rc = p.wait()
    if rc not in (0, 1):                       # 1: GNU tar's "file changed as we read it"
        err = p.stderr.read().decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise MoveError(f"tar on the source exited {rc} ({err[0][:200]})")
    return {"members": n, "bytes": size, "left_out": sorted(left)}


def compare(want: dict, got: dict) -> list[str]:
    """The files that differ between two looks (LEFT and the members the copy left out aside)."""
    bad = []
    a = {k: v for k, v in want["files"].items() if k not in LEFT}
    b = {k: v for k, v in got["files"].items() if k not in LEFT}
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            bad.append(k)
    return bad


def reseal(db: str, old, new, drop: bool) -> dict:
    """The MCP servers' sealed env and headers opened with `old` and sealed with `new` (or, with
    `drop`, cleared). {servers, sealed_again, cleared}."""
    c = sqlite3.connect(db)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(mcp_servers)")}
        fields = [f for f in ("env_enc", "headers_enc") if f in cols]
        rows = c.execute(f"SELECT id, {', '.join(fields)} FROM mcp_servers").fetchall() \
            if fields else []
        again = cleared = 0
        for row in rows:
            sid, vals = row[0], dict(zip(fields, row[1:]))
            for f, blob in vals.items():
                if not blob:
                    continue
                if drop or old is None:
                    if not drop:
                        raise MoveError("the MCP servers hold secrets sealed with the old host's "
                                        "master key: give --from-secrets FILE, or "
                                        "--without-mcp-secrets to enter them again")
                    c.execute(f"UPDATE mcp_servers SET {f} = NULL WHERE id = ?", (sid,))
                    cleared += 1
                    continue
                d = json.loads(blob)
                text = old.decrypt(sid, base64.b64decode(d["ct"]), base64.b64decode(d["nonce"]),
                                   d["kid"])
                ct, nonce, kid = new.encrypt(sid, text)
                c.execute(f"UPDATE mcp_servers SET {f} = ? WHERE id = ?", (json.dumps({
                    "ct": base64.b64encode(ct).decode(), "nonce": base64.b64encode(nonce).decode(),
                    "kid": kid}).encode(), sid))
                again += 1
        c.commit()
        return {"servers": len(rows), "sealed_again": again, "cleared": cleared}
    finally:
        c.close()


def check_db(db: str) -> dict:
    c = sqlite3.connect(db)
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise MoveError(f"the copied database fails its integrity check ({ok[:100]})")
        count = {}
        for t in ("conversations", "messages", "runs"):
            try:
                count[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                count[t] = None
        try:
            count["open_runs"] = c.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued', "
                                           "'running')").fetchone()[0]
        except sqlite3.Error:
            count["open_runs"] = None
        return count
    finally:
        c.close()


def has_chats(path: str) -> bool:
    return any(os.path.exists(os.path.join(path, x)) for x in HAS_CHATS)


def move(src: Source, chat, *, plan: bool = False, replace: bool = False, start: bool = True,
         from_secrets: str | None = None, without_mcp_secrets: bool = False,
         log=lambda m: print(m, flush=True), clock=time.time) -> dict:
    """The steps of the module's header; `chat` is this host's proxy/appchat.AppChat."""
    dest = os.path.abspath(chat.data)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(clock()))
    look = src.look()
    if not look.get("exists"):
        raise MoveError(f"{src}: no such folder")
    if DB not in look["files"]:
        raise MoveError(f"{src}: no {DB} there; not a chatd data folder")
    sealed = [m for m in look.get("mcp") or [] if m.get("sealed")]
    out = {"from": str(src), "to": dest, "files": len(look["files"]),
           "bytes": sum(v[1] for v in look["files"].values() if v[0] == "file"),
           "mcp": [{"name": m["name"], "transport": m["transport"]} for m in look.get("mcp") or []],
           "mcp_sealed": len(sealed), "running_there": look["running"],
           "here_running": chat.status()["running"], "here_has_chats": has_chats(dest),
           "old_master_key": bool(from_secrets) or look.get("secrets_key", False)}
    problems = []
    if look["running"]:
        problems.append(f"a chatd runs on the source (pid {look['pid']}): stop it first "
                        f"(`tandem-node chat stop` there, or its unit)")
    if out["here_has_chats"] and not replace:
        problems.append(f"{dest} holds chats already: --replace keeps them as "
                        f"{dest}.before-move-<stamp>")
    if sealed and not out["old_master_key"] and not without_mcp_secrets:
        problems.append("MCP servers hold sealed secrets and the old master key is not there: "
                        "--from-secrets FILE, or --without-mcp-secrets")
    out["problems"] = problems
    if plan:
        out["plan"] = True
        return out
    if problems:
        raise MoveError("; ".join(problems))
    old = None
    if sealed and not without_mcp_secrets:
        text = None
        if from_secrets:
            try:
                with open(os.path.expanduser(from_secrets)) as f:
                    text = f.read().strip()
            except OSError:
                raise MoveError("--from-secrets cannot be read") from None
        else:
            text = src.master_key()
        if not text:
            raise MoveError("the old master key cannot be read; --from-secrets FILE")
        old = _vault(text, "the old master key")
    if out["here_running"]:
        rc, msg = chat.run_action("stop")
        log(f"[chat-move] the chatd here: {msg}")
        if rc != 0:
            raise MoveError(f"the chatd here did not stop: {msg}")
    stage = f"{dest}.moving-{stamp}"
    log(f"[chat-move] copying {out['files']} files ({out['bytes']} bytes) from {src}")
    try:
        got = copy(src, stage)
        again = src.look()
        if compare(look, again):
            raise MoveError("the source changed while it was copied (a chatd started there?)")
        here = Source(stage).look()
        bad = [k for k in compare(look, here) if k not in got["left_out"]]
        if bad:
            raise MoveError(f"{len(bad)} files differ after the copy (first: {bad[0]})")
        out.update(copied=got["members"], left_out=got["left_out"])
        out["counts"] = check_db(os.path.join(stage, DB))
        if sealed:
            from proxy import secrets as vault_mod
            new = vault_mod.Vault.load(os.path.dirname(dest), env=chat.child_env())
            out["mcp_secrets"] = reseal(os.path.join(stage, DB), old, new, without_mcp_secrets)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)            # our own staging folder only
        raise
    if os.path.lexists(dest):
        if has_chats(dest):
            keep = f"{dest}.before-move-{stamp}"
            os.rename(dest, keep)
            out["kept"] = keep
        else:
            # a data folder with no chats (a chatd that never ran a chat): its forward key and
            # pid file give way to the moved ones; it is kept aside all the same
            keep = f"{dest}.empty-{stamp}"
            os.rename(dest, keep)
            out["kept"] = keep
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    os.rename(stage, dest)
    os.chmod(dest, 0o700)
    log(f"[chat-move] {out['counts'].get('conversations')} chats moved to {dest}")
    if start:
        rc, msg = chat.run_action("start")
        out["started"] = rc == 0
        out["start"] = msg
        if rc != 0:
            raise MoveError(f"moved, but the chatd here did not start: {msg}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="tandem-app chat-move",
                                 description="Move chatd's data folder to this host (SRV-843).")
    ap.add_argument("--from", dest="src", required=True,
                    help="the old data folder: USER@HOST:PATH (your SSH login) or a path here")
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--state-dir", default="~/.tandemllm",
                    help="the controller's state dir (its chatd.env names the data folder)")
    ap.add_argument("--from-secrets", default=None,
                    help="a file with the old host's master key (else ../secrets.key there)")
    ap.add_argument("--plan", action="store_true", help="print what would happen, change nothing")
    ap.add_argument("--replace", action="store_true",
                    help="a destination with chats is kept aside, not refused")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--without-mcp-secrets", action="store_true",
                    help="clear the MCP servers' secrets instead of sealing them again")
    a = ap.parse_args(argv)
    from proxy import appchat
    try:
        src = Source(a.src, a.ssh_port)
        chat = appchat.AppChat.load(a.state_dir, log=lambda m: print(m, flush=True))
        out = move(src, chat, plan=a.plan, replace=a.replace, start=not a.no_start,
                   from_secrets=a.from_secrets, without_mcp_secrets=a.without_mcp_secrets)
    except MoveError as exc:
        print(f"[chat-move] refused: {exc.message}", file=sys.stderr, flush=True)
        return exc.code
    except ValueError as exc:
        print(f"[chat-move] refused: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 1 if out.get("problems") and a.plan else 0
