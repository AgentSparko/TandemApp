#!/usr/bin/env python3
"""The chart's release archive from a source tree with its built page (OPS-292). Standard library
only; make-archive.sh runs it.

    python3 -I pack-archive.py SRC ID COMMIT EPOCH WHEELS OUT

SRC     a fresh `git archive` of COMMIT with dashboard/dist built (tools/dashboard_bundle.py build)
ID      the release id: COMMIT's first 12 characters, the archive's one folder
EPOCH   the commit's time (seconds): every entry's mtime, the gzip header's, RELEASE's date
WHEELS  a folder of wheels for the image's Python and the nodes' architecture (nothing else in it)
OUT     gets tandemapp-ID.tar.gz and tandemapp-ID.tar.gz.sha256

The archive holds one folder ID/: tools/app_release.py's release files (no tests, no page sources;
the page checked against its bundle.json), wheelhouse/ and RELEASE (commit, page bundle, date).
The same inputs give the same bytes: sorted entries, fixed times, owner 0, modes 0755 or 0644, no
name in the gzip header, no AppleDouble (._) files. It prints one JSON line: id, archive, sha256,
files, wheels, bundle.
"""
import datetime
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

if len(sys.argv) != 7:
    sys.exit(__doc__)
src, rid, commit, epoch, wheels, out = sys.argv[1:7]
epoch = int(epoch)
if len(rid) != 12 or not commit.startswith(rid):
    sys.exit("ID must be COMMIT's first 12 characters")
os.makedirs(out, exist_ok=True)
py = sys.executable

# the staging folder stays beside the archive (nothing is removed here)
work = tempfile.mkdtemp(prefix=".stage-", dir=out)
tree = os.path.join(work, rid)
subprocess.run([py, "-I", os.path.join(src, "tools", "app_release.py"), "stage", "--out", tree],
               check=True)
wh = os.path.join(tree, "wheelhouse")
os.makedirs(wh)
names = sorted(os.listdir(wheels))
if not names or not all(n.endswith(".whl") and not n.startswith(".") for n in names):
    sys.exit(f"{wheels} must hold wheels only: {names}")
for n in names:
    shutil.copyfile(os.path.join(wheels, n), os.path.join(wh, n))
    os.chmod(os.path.join(wh, n), 0o644)
with open(os.path.join(tree, "dashboard", "dist", "bundle.json")) as f:
    page = json.load(f)["sha256"]
shipped = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%SZ")
with open(os.path.join(tree, "RELEASE"), "w") as f:
    f.write(f"commit={commit}\nbundle={page}\nshipped={shipped}\n"
            f"built=tools/app_release.py stage + wheelhouse\n")
if not os.path.isfile(os.path.join(tree, "packaging", "linux", "tandem-app.sh")):
    sys.exit("the release tree has no packaging/linux/tandem-app.sh")

files = []
for d, dirs, fs in os.walk(tree):
    dirs.sort()
    for n in fs:
        p = os.path.join(d, n)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        if n.startswith("._"):
            sys.exit(f"an AppleDouble file in the tree: {p}")
        files.append(os.path.relpath(p, work).replace(os.sep, "/"))
files.sort()
raw = io.BytesIO()
with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
    for rel in files:
        p = os.path.join(work, rel)
        ti = tarfile.TarInfo(rel)
        ti.size = os.path.getsize(p)
        ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = epoch, 0, 0, "", ""
        ti.mode = 0o755 if os.access(p, os.X_OK) else 0o644
        with open(p, "rb") as fh:
            tar.addfile(ti, fh)
gz = io.BytesIO()
with gzip.GzipFile(fileobj=gz, mode="wb", mtime=epoch, filename="") as g:
    g.write(raw.getvalue())
arc = os.path.join(out, f"tandemapp-{rid}.tar.gz")
with open(arc, "wb") as f:
    f.write(gz.getvalue())
sha = hashlib.sha256(gz.getvalue()).hexdigest()
with open(arc + ".sha256", "w") as f:
    f.write(f"{sha}  {os.path.basename(arc)}\n")
print(json.dumps({"id": rid, "archive": arc, "sha256": sha, "files": len(files), "wheels": names,
                  "bundle": page}))
