#!/bin/sh
# TandemApp's container (OPS-282, packaging/Dockerfile.app): the controller, its state in /data.
# On the first start it writes /data/chatd.env: chatd runs here (TANDEM_CHAT=own) and finds its
# sandboxes' container runtime at /run/tandem/sandbox.sock. Without a socket mounted there the
# health page says that chat sandboxes are off; mount one to turn them on:
#   docker run ... -v /var/run/docker.sock:/run/tandem/sandbox.sock --group-add <its group id>
# The master key is /data/secrets.key, made at the first start. Then it runs `tandem-app ARGS`.
set -eu
STATE="${TANDEM_APP_STATE:-/data}"
mkdir -p "$STATE/logs"
if [ ! -f "$STATE/chatd.env" ]; then
    umask 077
    {
        echo "# chatd in TandemApp's container (packaging/container-start.sh): mount a container"
        echo "# runtime's socket at the path below to give chat its sandboxes"
        echo "QSE_SANDBOX_SOCKET=/run/tandem/sandbox.sock"
        echo "QSE_SANDBOX_MEM_GB=2"
        echo "QSE_SANDBOX_MAX_RUNNING=2"
    } > "$STATE/chatd.env"
fi
exec tandem-app "$@"
