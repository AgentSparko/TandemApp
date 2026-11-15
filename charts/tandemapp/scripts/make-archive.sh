#!/bin/bash
# The release archive the chart's init container unpacks: tandemapp-<id>.tar.gz with one folder <id>/
# holding the commit's tree (git archive), the built page (dashboard/dist) and a wheelhouse/ for the
# pod's virtualenv. No image is built.
#
#   bash charts/tandemapp/scripts/make-archive.sh COMMIT DIST_DIR OUT_DIR [PLATFORM] [PYTHON]
#
#   COMMIT    the commit to ship (its 12-character short hash is the release id)
#   DIST_DIR  the page built from that commit (python3 tools/dashboard_bundle.py build), with its
#             bundle.json
#   OUT_DIR   where tandemapp-<id>.tar.gz and its .sha256 go
#   PLATFORM  the nodes' wheel platform (default manylinux_2_28_x86_64; arm64: manylinux_2_28_aarch64)
#   PYTHON    the image's Python (default 3.12)
set -euo pipefail
[ $# -ge 3 ] || { sed -n '2,15p' "$0"; exit 2; }
COMMIT=$1 DIST=$2 OUT=$3 PLAT=${4:-manylinux_2_28_x86_64} PYV=${5:-3.12}
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
ID=$(git -C "$REPO" rev-parse --short=12 "$COMMIT")
[ -f "$DIST/bundle.json" ] || { echo "no $DIST/bundle.json: build the page first"; exit 1; }
mkdir -p "$OUT"
# the staging folder stays beside the archive (never removed here)
WORK=$(mktemp -d "$OUT/.stage-XXXXXX")
git -C "$REPO" archive --format=tar --prefix="$ID/" "$ID" | tar -C "$WORK" -xf -
mkdir -p "$WORK/$ID/dashboard/dist"
cp -R "$DIST/." "$WORK/$ID/dashboard/dist/"
printf 'commit=%s\nbundle=%s\nshipped=%s\n' "$(git -C "$REPO" rev-parse "$ID")" \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha256",""))' "$DIST/bundle.json")" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$WORK/$ID/RELEASE"
# manylinux tags older than PLAT are accepted too
python3 -m pip download -q --only-binary=:all: --implementation cp --python-version "$PYV" \
  --platform "$PLAT" --platform "${PLAT/2_28/2_17}" --platform "${PLAT/manylinux_2_28/manylinux2014}" \
  -d "$WORK/$ID/wheelhouse" "cryptography>=42" "psutil>=5.9"
COPYFILE_DISABLE=1 tar --no-xattrs -C "$WORK" -czf "$OUT/tandemapp-$ID.tar.gz" "$ID"
echo "$(shasum -a 256 < "$OUT/tandemapp-$ID.tar.gz" | cut -d" " -f1)  tandemapp-$ID.tar.gz" > "$OUT/tandemapp-$ID.tar.gz.sha256"
echo "release.id=$ID"
cat "$OUT/tandemapp-$ID.tar.gz.sha256"
