#!/usr/bin/env python3
"""TandemApp's release archive (OPS-282): the tree packaging/install.sh installs, with the built page
inside, so a host installs it with Python alone (no Node, no git).

    python3 tools/app_release.py files                      the files a release holds, one a line
    python3 tools/app_release.py stage --out DIR [--dist DIR]
    python3 tools/app_release.py pack --out FILE.tar.gz [--dist DIR]    (+ FILE.tar.gz.sha256)

A release holds every file of this tree but the tests, the page's sources (dashboard/ except
dist/), caches and links: packaging/tandemapp_install.py's `release_files`, the list the installer
copies too. The page is dashboard/dist, or --dist DIR (the dashboard-dist workflow's artifact); it
must be an intact bundle (tools/dashboard_bundle.py verify). `pack` writes the tree under the folder
tandemapp-<version>/ into a gzip'd tar with fixed times, owners and modes (the same tree gives the
same bytes), and its sha256 beside it. Then:

    tar -xzf tandemapp-<version>.tar.gz && bash tandemapp-<version>/packaging/install.sh

Exit 0 done, 1 a missing or broken page, 2 a usage error. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP, "packaging"))
sys.path.insert(0, APP)

import tandemapp_install as inst  # noqa: E402
from tandemapp import bundle  # noqa: E402

MTIME = 1_700_000_000


def _page(dist: str | None) -> str:
    d = os.path.abspath(dist or os.path.join(APP, "dashboard", "dist"))
    if not os.path.isfile(os.path.join(d, "index.html")):
        raise SystemExit(f"app_release: no built page at {d}: build it (python3 "
                         f"tools/dashboard_bundle.py build) or give --dist")
    v = bundle.verify(d)
    if not v["ok"]:
        raise SystemExit("app_release: the page does not match its bundle.json: "
                         + "; ".join(v["problems"]))
    return d


def entries(dist: str | None = None) -> list[tuple[str, str]]:
    """(the path in the release, the file on disk): the tree's files, the page from `dist`."""
    page = _page(dist)
    out = [(rel, os.path.join(APP, rel)) for rel in inst.release_files(APP)
           if not rel.startswith("dashboard/dist/")]
    for d, dirs, files in os.walk(page):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(d, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, page).replace(os.sep, "/")
            out.append((f"dashboard/dist/{rel}", full))
    return sorted(out)


def stage(out: str, dist: str | None = None) -> int:
    ents = entries(dist)
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit(f"app_release: {out} is not empty")
    for rel, src in ents:
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    return len(ents)


def pack(out: str, dist: str | None = None) -> str:
    ents = entries(dist)
    version = inst.read_version(APP)
    top = f"tandemapp-{version}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, src in ents:
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size = os.path.getsize(src)
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = MTIME, 0, 0, "", ""
            ti.mode = 0o755 if os.access(src, os.X_OK) else 0o644
            with open(src, "rb") as f:
                tar.addfile(ti, f)
    data = io.BytesIO()
    with gzip.GzipFile(fileobj=data, mode="wb", mtime=MTIME, filename="") as gz:
        gz.write(raw.getvalue())
    blob = data.getvalue()
    with open(out, "wb") as f:
        f.write(blob)
    sha = hashlib.sha256(blob).hexdigest()
    with open(out + ".sha256", "w") as f:
        f.write(f"{sha}  {os.path.basename(out)}\n")
    return sha


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tools/app_release.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("files")
    for name in ("stage", "pack"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--dist", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "files":
        for rel in inst.release_files(APP):
            print(rel)
        return 0
    if a.cmd == "stage":
        n = stage(a.out, a.dist)
        print(f"app_release: {n} files in {a.out}")
        return 0
    sha = pack(a.out, a.dist)
    print(f"app_release: {a.out} sha256 {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
