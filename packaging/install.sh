#!/bin/bash
# TandemApp in one command (OPS-282): from a release archive (it carries the built page, so the
# host needs no Node) or a checkout with a built page, on Linux (arm64 or x86_64) or macOS.
#
#   tar -xzf tandemapp-<version>.tar.gz && bash tandemapp-<version>/packaging/install.sh
#
# It makes a venv with this host's Python 3.11 or newer (no torch), copies the release next to
# the ones before, writes the state dir (~/.tandemllm) with a fresh master key and app.env, starts
# the controller as a user service on 127.0.0.1:8080 and prints the URL; the first page is the
# setup wizard. Run it again to update: a new release is switched to and restarted, the old one
# stays (and is switched back to when the new one does not come up). Every option, and what each
# step does: `bash packaging/install.sh --help` (packaging/tandemapp_install.py).
#
# This file only finds the Python: --python PY, else TANDEM_INSTALL_PYTHON, else the first of
# python3.13, python3.12, python3.11 and python3 (PATH, then Homebrew's) that is 3.11 or newer.
set -eu
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ok_py() {
    [ -n "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}
PY="${TANDEM_INSTALL_PYTHON:-}"
prev=""
for a in "$@"; do
    if [ "$prev" = "--python" ]; then PY="$a"; fi
    case "$a" in --python=*) PY="${a#--python=}" ;; esac
    prev="$a"
done
if [ -n "$PY" ]; then
    if ! ok_py "$PY"; then
        echo "[install] $PY is not Python 3.11 or newer" >&2
        exit 2
    fi
else
    for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 \
             /usr/local/bin/python3; do
        p="$(command -v "$c" 2>/dev/null || true)"
        if ok_py "$p"; then PY="$p"; break; fi
    done
fi
if [ -z "$PY" ]; then
    echo "[install] TandemApp needs Python 3.11 or newer. Install it first:" >&2
    echo "[install]   Debian, Ubuntu, Raspberry Pi OS: sudo apt install python3 python3-venv" >&2
    echo "[install]   macOS: brew install python@3.12" >&2
    echo "[install] then run this again, or name it with --python PATH" >&2
    exit 2
fi
exec "$PY" -I "$HERE/packaging/tandemapp_install.py" --source "$HERE" "$@"
