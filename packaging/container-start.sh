#!/bin/sh
# TandemApp's container (OPS-282, packaging/Dockerfile.app): the controller, its state in /data.
#
# Chat (OPS-288): a new /data starts bare, with Chat off until an admin switches it on in
# Settings, App, What runs here (the page's switch is kept in the config database and wins). A
# /data an older image made (its config.sqlite3 there before this script recorded a choice) keeps
# chat in the container as it ran. The choice is written once to /data/container.env;
# `-e TANDEM_CHAT=off|own|external` wins over it.
#
# chatd.env is written on the first start too, read only once Chat is on: chatd finds its
# sandboxes' container runtime at /run/tandem/sandbox.sock. Without a socket mounted there chat
# runs without its sandbox tools and the health page says so; mount one to give it them:
#   docker run ... -v /var/run/docker.sock:/run/tandem/sandbox.sock --group-add <its group id>
# The master key is /data/secrets.key, made at the first start. Then it runs `tandem-app ARGS`.
set -eu
STATE="${TANDEM_APP_STATE:-/data}"
mkdir -p "$STATE/logs"
umask 077
if [ ! -f "$STATE/container.env" ]; then
    if [ -f "$STATE/config.sqlite3" ]; then first=own; else first=off; fi
    {
        echo "# the container's first start (packaging/container-start.sh): Chat off on a new"
        echo "# /data, kept on one an older image made; the page's switch wins over this line"
        echo "TANDEM_CHAT=$first"
    } > "$STATE/container.env"
fi
if [ -z "${TANDEM_CHAT:-}" ]; then
    TANDEM_CHAT="$(sed -n 's/^TANDEM_CHAT=//p' "$STATE/container.env" | tail -n 1)"
    TANDEM_CHAT="${TANDEM_CHAT:-off}"
    export TANDEM_CHAT
fi
if [ ! -f "$STATE/chatd.env" ]; then
    {
        echo "# chatd in TandemApp's container (packaging/container-start.sh), once Chat is on:"
        echo "# mount a container runtime's socket at the path below to give chat its sandboxes"
        echo "QSE_SANDBOX_SOCKET=/run/tandem/sandbox.sock"
        echo "QSE_SANDBOX_MEM_GB=2"
        echo "QSE_SANDBOX_MAX_RUNNING=2"
    } > "$STATE/chatd.env"
fi
exec tandem-app "$@"
