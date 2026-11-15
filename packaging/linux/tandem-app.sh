#!/bin/bash
# TandemApp's controller on a plain Linux host (SRV-841): a host that runs no engine (arm64 or
# x86_64, Python 3.11 or newer). Its engines are fleet members it reaches over HTTP, and their
# control goes over SSH or their TandemNode agent; nothing of an engine is read on this host.
#
#   bash packaging/linux/tandem-app.sh [serve options]   the controller in the foreground (the
#                                                        unit's ExecStart, tandem-app.service)
#   bash packaging/linux/tandem-app.sh --print           the command it would run, then exit
#
# Where things are (the unit sets them, or the shell):
#   TANDEM_APP_STATE   the state dir, default ~/.tandemllm: config.sqlite3, ledger.sqlite3,
#                      fleet.sqlite3, controller/ (its certificate and SSH pins), logs/, and
#                      chatd.env when the controller runs chatd (TANDEM_CHAT=own)
#   TANDEM_APP_ENV     the env file read at the start, default $TANDEM_APP_STATE/app.env when it
#                      exists (packaging/app.env.example; mode 0600: it can hold keys)
#   TANDEM_APP_PYTHON  the interpreter, first match: this variable, $TANDEM_APP_STATE/venv,
#                      the checkout's venv/, python3 when it is 3.11 or newer
# A checkout (this file's ../../proxy is there) runs from its own tree; otherwise the installed
# package (`pip install 'tandemapp[app]'` into that venv).
#
# Defaults for a host with no engine, each only when neither the environment nor the env file
# sets it: TANDEM_NODE_AGENT=off (no TandemNode agent on this machine to read it through) and
# TANDEM_CONDUCTOR=off (a bare engine; the conductor is switched on in the settings). Chat stays
# TANDEM_CHAT=external until the env file says own: then this controller starts and watches chatd
# here with its own env file (proxy/appchat.py, packaging/linux/chatd.env.example).
set -eu
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
STATE="${TANDEM_APP_STATE:-$HOME/.tandemllm}"
ENVF="${TANDEM_APP_ENV:-$STATE/app.env}"
PRINT=0
if [ "${1:-}" = "--print" ]; then PRINT=1; shift; fi

version_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}
PY=""
for c in "${TANDEM_APP_PYTHON:-}" "$STATE/venv/bin/python" "$HERE/venv/bin/python"; do
    if [ -n "$c" ] && [ -x "$c" ] && version_ok "$c"; then PY="$c"; break; fi
    if [ -n "$c" ] && [ "$c" = "${TANDEM_APP_PYTHON:-}" ]; then
        echo "[tandem-app] TANDEM_APP_PYTHON=$c does not run Python 3.11 or newer" >&2; exit 2
    fi
done
if [ -z "$PY" ]; then
    PY="$(command -v python3 || true)"
    if [ -z "$PY" ] || ! version_ok "$PY"; then
        echo "[tandem-app] no Python 3.11 or newer: set TANDEM_APP_PYTHON or make $STATE/venv" >&2
        exit 2
    fi
fi

has() {   # the env file sets $1
    [ -f "$ENVF" ] && grep -Eq "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENVF"
}
for kv in TANDEM_NODE_AGENT=off TANDEM_CONDUCTOR=off; do
    k="${kv%%=*}"
    if [ -z "${!k+x}" ] && ! has "$k"; then export "$kv"; fi
done

ARGS=(-m proxy serve --config-db "$STATE/config.sqlite3"
      --usage-ledger "$STATE/ledger.sqlite3")
[ ! -f "$ENVF" ] || ARGS+=(--env-file "$ENVF")
if [ -f "$HERE/proxy/__main__.py" ]; then
    export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
    cd "$HERE"
fi
export PYTHONUNBUFFERED=1
if [ "$PRINT" = 1 ]; then
    for k in TANDEM_NODE_AGENT TANDEM_CONDUCTOR; do
        if [ -n "${!k+x}" ]; then printf '%s=%q ' "$k" "${!k}"; fi
    done
    printf '%q ' "$PY" "${ARGS[@]}" "$@"; echo
    exit 0
fi
mkdir -p "$STATE"
chmod 700 "$STATE" 2>/dev/null || true
exec "$PY" "${ARGS[@]}" "$@"
