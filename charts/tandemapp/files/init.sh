#!/bin/bash
# The init container: the release unpacked into the volume, its virtualenv, the CA bundle and the
# env file chatd reads. Runs in the stock Python image as the pod's user, at every start; a release
# that is ready (.ready) is used as it is.
set -euo pipefail
ROOT=$TA_ROOT
REL=$TA_RELEASE
DEST=$ROOT/releases/$REL
ARC=$ROOT/incoming/tandemapp-$REL.tar.gz
umask 027
mkdir -p "$ROOT/releases" "$ROOT/incoming" "$ROOT/state" "$ROOT/home" "$ROOT/ca" "$ROOT/backups"
chmod 700 "$ROOT/state" "$ROOT/backups"

if [ ! -f "$DEST/.ready" ]; then
  if [ -n "${TA_ARCHIVE_URL:-}" ] && [ ! -f "$ARC" ]; then
    echo "[init] fetching the release archive"
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "$TA_ARCHIVE_URL" "$ARC.part"
    mv "$ARC.part" "$ARC"
  fi
  if [ ! -f "$ARC" ]; then
    echo "[init] waiting for $ARC (kubectl cp it into this init container; see the chart's NOTES)"
    end=$((SECONDS + TA_WAIT_S))
    while [ ! -f "$ARC" ]; do
      [ $SECONDS -lt "$end" ] || { echo "[init] no archive after ${TA_WAIT_S}s"; exit 1; }
      sleep 3
    done
    # a copy in progress: wait until the size holds still
    s=-1; while [ "$(stat -c %s "$ARC")" != "$s" ]; do s=$(stat -c %s "$ARC"); sleep 3; done
  fi
  if [ -n "${TA_ARCHIVE_SHA256:-}" ]; then
    echo "$TA_ARCHIVE_SHA256  $ARC" | sha256sum -c - >/dev/null || { echo "[init] sha256 differs: $ARC"; exit 1; }
    echo "[init] archive sha256 ok"
  fi
  if [ -e "$DEST" ]; then
    # an unpack that did not finish: kept aside, never removed here
    mv "$DEST" "$ROOT/releases/.unfinished-$REL-$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  TMP=$ROOT/releases/.unpack-$REL-$$
  mkdir -p "$TMP"
  tar -xzf "$ARC" -C "$TMP" --no-same-owner
  [ -d "$TMP/$REL/proxy" ] || { echo "[init] the archive has no $REL/proxy"; exit 1; }
  mv "$TMP/$REL" "$DEST"
  rmdir "$TMP"
  python3 -m venv "$DEST/venv"
  # shellcheck disable=SC2086
  "$DEST/venv/bin/pip" install -q --no-index --disable-pip-version-check \
    --find-links "$DEST/wheelhouse" $TA_REQUIREMENTS
  "$DEST/venv/bin/python" -c 'import cryptography; print("[init] venv ok, cryptography", cryptography.__version__)'
  touch "$DEST/.ready"
fi
ln -sfn "releases/$REL" "$ROOT/current.next"
mv -T "$ROOT/current.next" "$ROOT/current"

# SSL_CERT_FILE of every container: the image's bundle plus the chart's extra CAs
cat /etc/ssl/certs/ca-certificates.crt > "$ROOT/ca/bundle.pem.next"
if [ -s /etc/tandemapp/trust/ca.pem ]; then cat /etc/tandemapp/trust/ca.pem >> "$ROOT/ca/bundle.pem.next"; fi
mv "$ROOT/ca/bundle.pem.next" "$ROOT/ca/bundle.pem"

# chatd's env file (chat.mode own); the controller reads <state>/chatd.env
if [ -f /etc/tandemapp/config/chatd.env ]; then
  install -m 600 /etc/tandemapp/config/chatd.env "$ROOT/state/chatd.env"
fi
mkdir -p "$ROOT/$TA_CHAT_DIR"
if [ -n "${TA_CONDUCTOR_DIR:-}" ]; then mkdir -p "$TA_CONDUCTOR_DIR"; fi
echo "[init] release $REL ready ($(cat "$DEST/RELEASE" 2>/dev/null | tr '\n' ' '))"
