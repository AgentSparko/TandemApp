"""Where a plugin comes from: a folder, a wheel or a git URL at a commit (SRV-857, plugin-v1).

    stage(source, into) -> Staged         the source's tree copied into the folder `into`

Nothing the source holds runs: a folder is copied file by file, a wheel is read as a zip (never
pip), and a git source is fetched into an empty repository of our own and its files are read out
of the object store one blob at a time (never a checkout: no hook, no filter, no attribute, no
LFS smudge runs).

A SOURCE (the install body's `source`):

    {"type": "folder", "path": "/abs/folder"}
    {"type": "wheel",  "path": "/abs/name-1.0.0-py3-none-any.whl"}
    {"type": "git",    "url": "https://host/repo.git" | "ssh://..." | "git@host:repo" |
                              "file:///abs/repo" | "/abs/repo",
                       "commit": "<40 or 64 hex digits>", "subdir"?: "plugins/x"}

A git source is pinned to a commit: a branch or tag name moves and is refused, and so is a short
id. The commit must resolve after the fetch. Only the https, ssh and file transports are allowed.
The fetch runs with no system or user git config (GIT_CONFIG_NOSYSTEM, GIT_CONFIG_GLOBAL=/dev/null),
no hooks and no prompt. A submodule (a gitlink) or an LFS file (a pointer blob, or a path with
filter=lfs in .gitattributes) refuses the install unless the manifest at that commit says
`"git": {"submodules": "ignore"}` or `{"lfs": "ignore"}`: then a submodule's folder stays empty and
a pointer file stays a pointer.

A wheel's members are checked before anything is written: no absolute path, no "..", no symlink,
no more than the tree's limits (proxy/pluginmanifest.py); RECORD's hashes, when the wheel has
one, must hold. The manifest is at the wheel's root or in one top-level folder (`manifest_dir`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile

from proxy import pluginmanifest as PM

COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
GIT_TIMEOUT_S = 300
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
TYPES = ("folder", "wheel", "git")


class SourceError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.field, self.status = code, message, field, status


class Staged:
    """A source copied into `dir`: `manifest_dir` the manifest's folder under it, `record` what
    the registry keeps of the source, `skipped` what a git source's declaration left out."""

    def __init__(self, dir: str, record: dict, manifest_dir: str = "", skipped=()):
        self.dir, self.record, self.manifest_dir = dir, record, manifest_dir
        self.skipped = list(skipped)


def check(source) -> dict:
    """The source as given, checked for its shape; raises SourceError."""
    if not isinstance(source, dict):
        raise SourceError("bad_source", "source: an object {type, ...}", "source")
    t = source.get("type")
    if t not in TYPES:
        raise SourceError("bad_source", f"source.type: one of {', '.join(TYPES)}", "source.type")
    allowed = {"folder": {"type", "path"}, "wheel": {"type", "path"},
               "git": {"type", "url", "commit", "subdir"}}[t]
    extra = sorted(set(source) - allowed)
    if extra:
        raise SourceError("bad_source", f"source.{extra[0]}: not a field of a {t} source",
                          f"source.{extra[0]}")
    if t in ("folder", "wheel"):
        p = source.get("path")
        if not isinstance(p, str) or not os.path.isabs(p) or len(p) > 4096 or "\0" in p:
            raise SourceError("bad_source", "source.path: an absolute path on the controller's "
                                            "host", "source.path")
        return {"type": t, "path": os.path.normpath(p)}
    url = source.get("url")
    commit = source.get("commit")
    if not isinstance(url, str) or not url or len(url) > 2048 or url.startswith("-") or \
            any(ord(c) < 33 for c in url):
        raise SourceError("bad_source", "source.url: a git URL (https, ssh, file or an absolute "
                                        "path)", "source.url")
    if "::" in url.split("/")[0] or not _transport_ok(url):
        raise SourceError("bad_source", "source.url: only the https, ssh and file transports",
                          "source.url")
    if not isinstance(commit, str) or not COMMIT_RE.match(commit):
        raise SourceError("commit_required", "source.commit: the full commit id (40 hex digits; "
                                             "64 for a sha256 repository). A branch or tag name "
                                             "moves and a short id may stop being unique: pin "
                                             "the commit", "source.commit")
    sub = source.get("subdir")
    if sub is not None and (not isinstance(sub, str) or not SUBDIR_RE.match(sub)
                            or ".." in sub.split("/")):
        raise SourceError("bad_source", "source.subdir: a relative folder in the repository",
                          "source.subdir")
    out = {"type": "git", "url": url, "commit": commit}
    if sub:
        out["subdir"] = sub
    return out


def _transport_ok(url: str) -> bool:
    if os.path.isabs(url):
        return True
    m = re.match(r"^([a-z][a-z0-9+.-]*)://", url)
    if m:
        return m.group(1) in ("https", "ssh", "file")
    # scp-like: user@host:path (no scheme)
    return bool(re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^:]", url))


def stage(source: dict, into: str) -> Staged:
    src = check(source)
    os.makedirs(into, exist_ok=False)
    try:
        if src["type"] == "folder":
            return _folder(src, into)
        if src["type"] == "wheel":
            return _wheel(src, into)
        return _git(src, into)
    except OSError as exc:
        raise SourceError("unreadable", f"the source could not be read or copied: "
                                        f"{exc.strerror or exc}", "source") from None


# =========================================================================================== folder
def _folder(src: dict, into: str) -> Staged:
    root = src["path"]
    if not os.path.isdir(root):
        raise SourceError("not_found", f"source.path: no folder {root}", "source.path", 404)
    try:
        files = PM.tree_files(root)
    except PM.TreeError as exc:
        raise SourceError(exc.code, exc.message, "source.path") from None
    for rel, p, x, _ in files:
        dst = os.path.join(into, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(p, dst, follow_symlinks=False)
        os.chmod(dst, 0o755 if x else 0o644)
    sig = os.path.join(root, PM.SIGNATURE)
    if os.path.isfile(sig) and not os.path.islink(sig):
        shutil.copyfile(sig, os.path.join(into, PM.SIGNATURE), follow_symlinks=False)
    return Staged(into, {"type": "folder", "path": root})


# =========================================================================================== wheel
def _wheel(src: dict, into: str) -> Staged:
    path = src["path"]
    if not path.endswith(".whl"):
        raise SourceError("bad_source", "source.path: a .whl file", "source.path")
    if not os.path.isfile(path):
        raise SourceError("not_found", f"source.path: no file {path}", "source.path", 404)
    archive = PM.file_sha256(path)
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise SourceError("bad_wheel", f"not a wheel (zip): {exc}", "source.path") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > PM.MAX_FILES:
            raise SourceError("too_large", f"more than {PM.MAX_FILES} files", "source.path")
        if sum(i.file_size for i in infos) > PM.MAX_TREE_BYTES:
            raise SourceError("too_large", f"more than {PM.MAX_TREE_BYTES >> 20} MiB unpacked",
                              "source.path")
        names = []
        for i in infos:
            n = i.filename
            mode = (i.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SourceError("symlink", f"{n} is a symlink: a plugin's tree holds files "
                                             f"only", "source.path")
            parts = n.split("/")
            if n.startswith("/") or "\\" in n or any(p in ("..", "") for p in parts) or \
                    any(PM._bad_name(p) for p in parts):
                raise SourceError("bad_wheel", f"member {n!r} leaves the tree", "source.path")
            names.append(n)
        record = _wheel_record(z, names)
        written = 0
        for i in infos:
            dst = os.path.join(into, *i.filename.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            h = hashlib.sha256()
            with z.open(i) as r, open(dst, "wb") as w:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    written += len(chunk)
                    if written > PM.MAX_TREE_BYTES:
                        raise SourceError("too_large", "the wheel unpacks larger than it says",
                                          "source.path")
                    h.update(chunk)
                    w.write(chunk)
            if i.filename in record and record[i.filename] and \
                    record[i.filename] != h.digest():
                raise SourceError("bad_wheel", f"{i.filename}: its sha256 differs from the "
                                               f"wheel's RECORD", "source.path")
            x = (i.external_attr >> 16) & 0o100
            os.chmod(dst, 0o755 if x else 0o644)
    tops = sorted({n.split("/")[0] for n in names if "/" in n})
    if PM.MANIFEST in names:
        mdir = ""
    else:
        found = [t for t in tops if f"{t}/{PM.MANIFEST}" in names]
        if len(found) != 1:
            raise SourceError("manifest_missing",
                              f"no {PM.MANIFEST} at the wheel's root or in one top-level folder"
                              + (f" (found in {', '.join(found)})" if found else ""),
                              "source.path")
        mdir = found[0]
    return Staged(into, {"type": "wheel", "path": path, "archive_sha256": archive}, mdir)


def _wheel_record(z: zipfile.ZipFile, names: list[str]) -> dict:
    """{member: its sha256 digest bytes or None} from the wheel's RECORD, {} without one."""
    rec = [n for n in names if re.match(r"^[^/]+\.dist-info/RECORD$", n)]
    if len(rec) != 1:
        return {}
    out = {}
    for line in z.read(rec[0]).decode("utf-8", "replace").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) != 3 or not parts[0]:
            continue
        name, h = parts[0].strip('"'), parts[1]
        if h.startswith("sha256="):
            try:
                out[name] = base64.urlsafe_b64decode(h[7:] + "=" * (-len(h[7:]) % 4))
            except (ValueError, TypeError):
                raise SourceError("bad_wheel", f"RECORD: a bad hash for {name}", "source.path")
        else:
            out[name] = None
    return out


# =========================================================================================== git
def git_env() -> dict:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "GIT_SSL_CAINFO", "TMPDIR", "USER", "LOGNAME")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                "GIT_LFS_SKIP_SMUDGE": "1", "GIT_PROTOCOL_FROM_USER": "0",
                # never a prompt: an unknown host key or a passphrase fails the fetch instead
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
    return env


GIT_CONF = ("-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
            "-c", "protocol.file.allow=always", "-c", "credential.helper=",
            "-c", "core.fsmonitor=false", "-c", "transfer.fsckObjects=true",
            "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false")


def _git_run(repo: str, args: list[str], input: bytes | None = None,
             timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *GIT_CONF, "-C", repo, *args], input=input,
                              capture_output=True, env=git_env(), timeout=timeout)
    except FileNotFoundError:
        raise SourceError("git_missing", "git is not installed on the controller's host",
                          "source.url", 503) from None
    except subprocess.TimeoutExpired:
        raise SourceError("git_timeout", f"git took longer than {int(timeout)} s",
                          "source.url", 504) from None


def _url_for_git(url: str) -> str:
    return "file://" + url if os.path.isabs(url) else url


def _git(src: dict, into: str) -> Staged:
    repo = into + ".git"
    os.makedirs(repo)
    try:
        commit, url = src["commit"], _url_for_git(src["url"])
        fmt = ["--object-format=sha256"] if len(commit) == 64 else []
        r = _git_run(repo, ["init", "-q", "--bare", *fmt])
        if r.returncode != 0:
            raise SourceError("git_failed", "git init failed: " + _tail(r), "source.url")
        r = _git_run(repo, ["fetch", "-q", "--no-tags", "--depth", "1", url, commit])
        if r.returncode != 0:
            # a server that does not serve a commit by its id: all its branches and tags, then
            # the commit must be among them
            r = _git_run(repo, ["fetch", "-q", "--no-tags", url,
                                "+refs/heads/*:refs/src/heads/*", "+refs/tags/*:refs/src/tags/*"])
            if r.returncode != 0:
                raise SourceError("git_failed", "git fetch failed: " + _tail(r), "source.url",
                                  502)
        r = _git_run(repo, ["cat-file", "-t", commit])
        if r.returncode != 0 or r.stdout.strip() != b"commit":
            raise SourceError("commit_not_found", f"source.commit: {commit} is not a commit of "
                                                  f"{src['url']}", "source.commit")
        entries, sizes = _ls_tree(repo, commit)
        sub = src.get("subdir")
        if sub:
            pre = sub.rstrip("/") + "/"
            entries = [(m, t, o, p[len(pre):]) for m, t, o, p in entries if p.startswith(pre)]
            if not entries:
                raise SourceError("not_found", f"source.subdir: no {sub} at {commit[:12]}",
                                  "source.subdir")
        blobs = {p: o for m, t, o, p in entries if t == "blob"}
        if len(blobs) > PM.MAX_FILES:
            raise SourceError("too_large", f"more than {PM.MAX_FILES} files", "source.url")
        if sum(sizes.get(o, 0) for o in blobs.values()) > PM.MAX_TREE_BYTES:
            raise SourceError("too_large", f"more than {PM.MAX_TREE_BYTES >> 20} MiB",
                              "source.url")
        allow = _declared(repo, blobs.get(PM.MANIFEST))
        skipped = []
        gitlinks = [p for m, t, o, p in entries if t == "commit"]
        if gitlinks and allow.get("submodules") != "ignore":
            raise SourceError("submodule", f"{gitlinks[0]} is a submodule: its code is not in "
                                           f"this commit. Vendor it, or say \"git\": "
                                           f"{{\"submodules\": \"ignore\"}} in {PM.MANIFEST}",
                              "source.url")
        skipped += [f"submodule {p}" for p in gitlinks]
        for m, t, o, p in entries:
            if m == "120000":
                raise SourceError("symlink", f"{p} is a symlink: a plugin's tree holds files "
                                             f"only", "source.url")
        lfs_paths = _lfs_paths(repo, blobs.get(".gitattributes"))
        datas = _cat_blobs(repo, [o for m, t, o, p in entries if t == "blob"])
        total = 0
        for m, t, o, p in entries:
            if t != "blob":
                continue
            data = datas[o]
            total += len(data)
            if total > PM.MAX_TREE_BYTES:
                raise SourceError("too_large", "the commit's tree is over the limits",
                                  "source.url")
            is_lfs = data.startswith(LFS_POINTER) or _matches(p, lfs_paths)
            if is_lfs:
                if allow.get("lfs") != "ignore":
                    raise SourceError("lfs", f"{p} is an LFS file: its content is not in git. "
                                             f"Commit the file itself, or say \"git\": "
                                             f"{{\"lfs\": \"ignore\"}} in {PM.MANIFEST}",
                                      "source.url")
                skipped.append(f"lfs {p}")
            parts = p.split("/")
            if any(x in ("", ".", "..") for x in parts) or any(PM._bad_name(x) for x in parts):
                raise SourceError("bad_name", f"a path that leaves the tree: {p!r}", "source.url")
            dst = os.path.join(into, *parts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            os.chmod(dst, 0o755 if m == "100755" else 0o644)
        rec = {"type": "git", "url": src["url"], "commit": commit}
        if sub:
            rec["subdir"] = sub
        return Staged(into, rec, "", skipped)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _tail(r) -> str:
    return (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:][0][:300] \
        if (r.stderr or b"").strip() else f"exit {r.returncode}"


def _ls_tree(repo: str, commit: str) -> tuple[list[tuple[str, str, str, str]], dict]:
    """([(mode, type, object id, path)], {object id: size}) of the commit's whole tree."""
    r = _git_run(repo, ["ls-tree", "-r", "-z", "-l", "--full-tree", commit])
    if r.returncode != 0:
        raise SourceError("git_failed", "git ls-tree failed: " + _tail(r), "source.url")
    out, sizes = [], {}
    for item in r.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, path = item.partition(b"\t")
        mode, typ, oid, size = meta.decode().split()
        if size.isdigit():
            sizes[oid] = int(size)
        try:
            p = path.decode("utf-8")
        except UnicodeDecodeError:
            raise SourceError("bad_name", "a path that is not UTF-8", "source.url") from None
        out.append((mode, typ, oid, p))
    return out, sizes


def _cat_blobs(repo: str, oids: list[str]) -> dict[str, bytes]:
    if not oids:
        return {}
    uniq = list(dict.fromkeys(oids))
    r = _git_run(repo, ["cat-file", "--batch"], input=("\n".join(uniq) + "\n").encode())
    if r.returncode != 0:
        raise SourceError("git_failed", "git cat-file failed: " + _tail(r), "source.url")
    out, buf, i = {}, r.stdout, 0
    for oid in uniq:
        nl = buf.index(b"\n", i)
        head = buf[i:nl].decode().split(" ")
        if len(head) != 3 or head[1] != "blob":
            raise SourceError("git_failed", f"object {oid[:12]} is not a blob", "source.url")
        size = int(head[2])
        out[oid] = buf[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return out


def _declared(repo: str, manifest_oid: str | None) -> dict:
    """The manifest's `git` declarations at that commit ({} when it has none or does not parse:
    the install's checks report the manifest itself)."""
    if not manifest_oid:
        return {}
    try:
        data = _cat_blobs(repo, [manifest_oid])[manifest_oid]
        doc = json.loads(data.decode("utf-8")) if len(data) <= PM.MAX_MANIFEST_BYTES else {}
        g = doc.get("git") if isinstance(doc, dict) else None
        return g if isinstance(g, dict) else {}
    except (ValueError, UnicodeDecodeError, SourceError):
        return {}


def _lfs_paths(repo: str, attrs_oid: str | None) -> list[str]:
    if not attrs_oid:
        return []
    text = _cat_blobs(repo, [attrs_oid])[attrs_oid].decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#") and "filter=lfs" in parts[1:]:
            out.append(parts[0])
    return out


def _matches(path: str, patterns: list[str]) -> bool:
    import fnmatch
    name = path.rsplit("/", 1)[-1]
    for pat in patterns:
        p = pat.lstrip("/")
        if fnmatch.fnmatch(path, p) or ("/" not in p and fnmatch.fnmatch(name, p)) or \
                fnmatch.fnmatch(path, p.rstrip("/") + "/**"):
            return True
    return False
