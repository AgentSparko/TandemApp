#!/bin/bash
# The plan's stronger sandbox runtime (OPS-103, Chat 6; OPS-282): rootless Podman under its own user
# `tandem-sbx`, so an escape from a container lands in a user that owns nothing. Needs root once:
#
#   sudo bash ops/sandbox/install.sh [--owner USER] [--data DIR] [--rebuild]
#   bash ops/sandbox/install.sh --plan [--owner USER] [--data DIR]    what it would do (no root)
#
# packaging/install.sh --with-sandbox runs it. OWNER is the user chatd runs as (default
# $SUDO_USER), DIR chatd's data folder (QSE_CHAT_DATA, default ~OWNER/.tandemllm/chat). The last
# line it prints is QSE_SANDBOX_SOCKET=<socket>, the line chatd.env needs.
#
# What it does (each step is undone by ops/sandbox/uninstall.sh); a second run repeats nothing
# that is in place:
#   1. podman crun passt uidmap acl from apt when podman is missing (Ubuntu 24.04 ships AppArmor
#      profiles for podman and crun, which allow their user namespaces under
#      kernel.apparmor_restrict_unprivileged_userns=1); another distribution installs them first
#   2. the user tandem-sbx (no login shell, no sudo), subuid/subgid ranges, linger, its
#      podman.socket user unit, and a drop-in on it that gives only OWNER the socket each time it
#      starts (the runtime folder is made again at every boot)
#   3. builds tandem-sandbox:1 as tandem-sbx, before the egress table: the build needs the
#      network, and the table drops a resolver on 127.0.0.1 (--rebuild builds it again)
#   4. an nftables table that drops everything tandem-sbx sends to private, loopback, link-local
#      and CGNAT ranges (defence in depth: chatd runs every sandbox with --network none anyway),
#      loaded now and at each boot by tandem-sbx-nft.service
#   5. ACLs on chatd's folders: the container's user (uid 1000 under tandem-sbx, the host uid
#      first subuid + 999) gets rwX on workspaces, homes and egress, with defaults for new folders;
#      tandem-sbx may traverse to them. Without them every bind mount is refused.
#   6. the memory cgroup: chatd caps each sandbox's memory, which needs the kernel's memory
#      controller, delegated to tandem-sbx's systemd. When it is missing the fix is printed (on a
#      Raspberry Pi: cgroup_enable=memory cgroup_memory=1 in /boot/firmware/cmdline.txt, then a
#      reboot); no boot file is edited here.
# Until this runs, chatd uses the rootful Docker socket with the same locked-down containers.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SBX=tandem-sbx
OWNER="${SUDO_USER:-}"
DATA=""
PLAN=0
REBUILD=0
while [ $# -gt 0 ]; do
    case "$1" in
        --owner) OWNER="${2:?--owner USER}"; shift 2 ;;
        --data) DATA="${2:?--data DIR}"; shift 2 ;;
        --plan) PLAN=1; shift ;;
        --rebuild) REBUILD=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) echo "unknown option $1 (--help)" >&2; exit 2 ;;
    esac
done
[ -n "$OWNER" ] || { echo "name the user chatd runs as: --owner USER (or run it with sudo)" >&2; exit 2; }
if [ "$PLAN" = 0 ] && [ "$(id -u)" != 0 ]; then
    echo "run it with sudo (or --plan to see what it does)" >&2
    exit 1
fi
OWNER_HOME="$(getent passwd "$OWNER" 2>/dev/null | cut -d: -f6 || true)"
[ -n "$OWNER_HOME" ] || OWNER_HOME="/home/$OWNER"
DATA="${DATA:-$OWNER_HOME/.tandemllm/chat}"

step() { echo "== $*"; }
run() {   # run CMD...: print it; run it unless --plan
    echo "   $*"
    if [ "$PLAN" = 0 ]; then "$@"; fi
}

step "1. podman, crun, passt, uidmap, acl"
if command -v podman >/dev/null 2>&1 && command -v setfacl >/dev/null 2>&1; then
    echo "   there"
elif command -v apt-get >/dev/null 2>&1; then
    run apt-get update
    run apt-get install -y podman crun passt uidmap acl
else
    echo "   no apt-get: install podman, crun, passt, uidmap and acl with this system's tools, then run this again" >&2
    [ "$PLAN" = 1 ] || exit 1
fi

step "2. the user $SBX, its subids, linger and its podman socket for $OWNER"
if ! id "$SBX" >/dev/null 2>&1; then
    run useradd --system --create-home --shell /usr/sbin/nologin "$SBX"
fi
grep -q "^$SBX:" /etc/subuid 2>/dev/null || run usermod --add-subuids 200000-265535 "$SBX"
grep -q "^$SBX:" /etc/subgid 2>/dev/null || run usermod --add-subgids 200000-265535 "$SBX"
run loginctl enable-linger "$SBX"
UID_SBX="$(id -u "$SBX" 2>/dev/null || echo UID)"
SBX_HOME="$(getent passwd "$SBX" 2>/dev/null | cut -d: -f6 || true)"
SBX_HOME="${SBX_HOME:-/home/$SBX}"
RUNDIR="/run/user/$UID_SBX"
SOCK="$RUNDIR/podman/podman.sock"
DROPIN="$SBX_HOME/.config/systemd/user/podman.socket.d/tandem-owner.conf"
SETFACL="$(command -v setfacl || echo /usr/bin/setfacl)"
echo "   $DROPIN: $OWNER may open the socket after each start"
if [ "$PLAN" = 0 ]; then
    for w in 1 2 3 4 5 6 7 8 9 10; do [ -d "$RUNDIR" ] && break; sleep 1; done
    sudo -u "$SBX" mkdir -p "$(dirname "$DROPIN")"
    printf '%s\n' "# written by ops/sandbox/install.sh: only $OWNER (chatd) may open this socket" \
        "[Socket]" \
        "ExecStartPost=$SETFACL -m u:$OWNER:x %t %t/podman" \
        "ExecStartPost=$SETFACL -m u:$OWNER:rw %t/podman/podman.sock" \
        | sudo -u "$SBX" tee "$DROPIN" >/dev/null
fi
run sudo -u "$SBX" XDG_RUNTIME_DIR="$RUNDIR" systemctl --user daemon-reload
run sudo -u "$SBX" XDG_RUNTIME_DIR="$RUNDIR" systemctl --user enable podman.socket
run sudo -u "$SBX" XDG_RUNTIME_DIR="$RUNDIR" systemctl --user restart podman.socket
run sudo -u "$SBX" podman unshare true

step "3. the image tandem-sandbox:1, built as $SBX"
if [ "$PLAN" = 0 ] && [ "$REBUILD" = 0 ] && \
        sudo -u "$SBX" XDG_RUNTIME_DIR="$RUNDIR" podman image exists tandem-sandbox:1; then
    echo "   there (--rebuild builds it again)"
else
    if [ "$PLAN" = 0 ] && systemctl is-active --quiet tandem-sbx-nft.service; then
        run systemctl stop tandem-sbx-nft.service
    fi
    run mkdir -p "$SBX_HOME/sandbox-build"
    run cp -R "$HERE/." "$SBX_HOME/sandbox-build/"
    run chown -R "$SBX" "$SBX_HOME/sandbox-build"
    run sudo -u "$SBX" XDG_RUNTIME_DIR="$RUNDIR" podman build -t tandem-sandbox:1 \
        -f "$SBX_HOME/sandbox-build/Containerfile" "$SBX_HOME/sandbox-build"
fi

step "4. the egress table inet tandem_sbx, now and at each boot"
NFT="$(command -v nft || echo /usr/sbin/nft)"
run mkdir -p /etc/nftables.d
run install -m 644 "$HERE/tandem-sbx.nft" /etc/nftables.d/tandem-sbx.nft
UNITF=/etc/systemd/system/tandem-sbx-nft.service
echo "   $UNITF"
if [ "$PLAN" = 0 ]; then
    printf '%s\n' "# written by ops/sandbox/install.sh: tandem-sbx reaches no private address" \
        "[Unit]" "Description=TandemApp sandboxes: the egress table inet tandem_sbx" \
        "After=network-pre.target" "Before=network.target" "" \
        "[Service]" "Type=oneshot" "RemainAfterExit=yes" \
        "ExecStart=$NFT -f /etc/nftables.d/tandem-sbx.nft" \
        "ExecStop=$NFT delete table inet tandem_sbx" "" \
        "[Install]" "WantedBy=multi-user.target" > "$UNITF"
    systemctl daemon-reload
    $NFT delete table inet tandem_sbx 2>/dev/null || true
fi
run systemctl enable --now tandem-sbx-nft.service

step "5. ACLs on chatd's folders in $DATA"
FIRST="$(awk -F: -v u="$SBX" '$1 == u {print $2; exit}' /etc/subuid 2>/dev/null || true)"
FIRST="${FIRST:-200000}"
CUID=$((FIRST + 999))
echo "   the container's uid 1000 is host uid $CUID"
for d in workspaces homes egress; do
    if [ ! -d "$DATA/$d" ]; then
        run mkdir -p "$DATA/$d"
        run chown "$OWNER" "$DATA/$d"
    fi
    run setfacl -m "u:$CUID:rwX,d:u:$CUID:rwX,d:u:$OWNER:rwX" "$DATA/$d"
done
p="$DATA"
while [ -n "$p" ] && [ "$p" != / ]; do
    perms="$(stat -c %A "$p" 2>/dev/null || stat -f %Sp "$p" 2>/dev/null || echo ----------)"
    case "${perms:9:1}" in
        x|t) ;;                                   # anyone may traverse it already
        *) run setfacl -m "u:$SBX:x" "$p" ;;
    esac
    p="$(dirname "$p")"
done

step "6. the memory cgroup"
CTRL=/sys/fs/cgroup/cgroup.controllers
DELEG="/sys/fs/cgroup/user.slice/user-$UID_SBX.slice/user@$UID_SBX.service/cgroup.controllers"
if [ -r "$CTRL" ] && ! grep -qw memory "$CTRL"; then
    echo "   WARNING: the kernel does not cap a container's memory here, so a sandbox has no memory limit."
    echo "   Turn the memory controller on: on a Raspberry Pi add cgroup_enable=memory cgroup_memory=1"
    echo "   to the one line of /boot/firmware/cmdline.txt and reboot. Nothing was edited here."
elif [ -r "$DELEG" ] && ! grep -qw memory "$DELEG"; then
    echo "   WARNING: the memory controller is not delegated to $SBX's systemd: add a drop-in with"
    echo "   Delegate=cpu cpuset io memory pids for user@.service and reboot. Nothing was edited here."
else
    echo "   memory limits work (or are checked by the health page once $SBX runs)"
fi

echo
echo "done. chatd's env file (chatd.env) needs:"
echo "QSE_SANDBOX_SOCKET=$SOCK"
