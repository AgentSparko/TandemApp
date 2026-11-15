#!/bin/bash
# The release archive the chart's init container unpacks (OPS-292): tandemapp-<id>.tar.gz with one
# folder <id>/ holding the release's files of the commit (tools/app_release.py: no tests, no page
# sources), the built page (dashboard/dist) and a wheelhouse/ for the pod's virtualenv, and its
# .sha256. No image is built. Run it on a Linux or macOS build host with git and Python 3.11+.
#
#   bash charts/tandemapp/scripts/make-archive.sh COMMIT DIST_DIR OUT_DIR [PLATFORM] [PYTHON]
#
#   COMMIT    the commit to ship (its first 12 characters are the release id)
#   DIST_DIR  the page built from that commit (python3 tools/dashboard_bundle.py build in a fresh
#             `git archive` of it), with its bundle.json
#   OUT_DIR   where tandemapp-<id>.tar.gz and its .sha256 go
#   PLATFORM  the nodes' wheel platform (default manylinux_2_28_x86_64; arm64: manylinux_2_28_aarch64)
#   PYTHON    the image's Python (default 3.12)
#   WHEELS=DIR  (environment) take the wheels from DIR instead of the package index: the same
#             wheels, the same commit and the same page give the same archive, byte for byte
#             (scripts/pack-archive.py: sorted entries, the commit's time, owner 0)
set -euo pipefail
[ $# -ge 3 ] || { sed -n '2,19p' "$0"; exit 2; }
COMMIT=$1 DIST=$2 OUT=$3 PLAT=${4:-manylinux_2_28_x86_64} PYV=${5:-3.12}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
FULL=$(git -C "$REPO" rev-parse "$COMMIT^{commit}")
ID=${FULL:0:12}
EPOCH=$(git -C "$REPO" show -s --format=%ct "$FULL")
[ -f "$DIST/bundle.json" ] || { echo "no $DIST/bundle.json: build the page first"; exit 1; }
mkdir -p "$OUT"
# the work folder stays beside the archive (never removed here)
WORK=$(mktemp -d "$OUT/.work-XXXXXX")
git -C "$REPO" archive --format=tar "$FULL" | tar -C "$WORK" -xf -
mkdir -p "$WORK/dashboard/dist"
cp -R "$DIST/." "$WORK/dashboard/dist/"
( cd "$WORK" && python3 -I tools/dashboard_bundle.py verify )
if [ -n "${WHEELS:-}" ]; then
  W=$WHEELS
else
  # manylinux tags older than PLAT are accepted too
  W=$(mktemp -d "$OUT/.wheels-XXXXXX")
  python3 -m pip download -q --only-binary=:all: --implementation cp --python-version "$PYV" \
    --platform "$PLAT" --platform "${PLAT/2_28/2_17}" --platform "${PLAT/manylinux_2_28/manylinux2014}" \
    -d "$W" "cryptography>=42" "psutil>=5.9"
fi
python3 -I "$HERE/pack-archive.py" "$WORK" "$ID" "$FULL" "$EPOCH" "$W" "$OUT"
echo "release.id=$ID"
cat "$OUT/tandemapp-$ID.tar.gz.sha256"
