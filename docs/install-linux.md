# TandemApp in one command, or in one container

The overview, the first page, upgrades and backups: [install.md](install.md).

## One command

A release archive carries the built page, so a host installs TandemApp with Python 3.11 or newer
alone: Linux on arm64 or x86_64 (a Raspberry Pi 4 or 5 too), or macOS. No root, no Node, no git.

    tar -xzf tandemapp-<version>.tar.gz
    bash tandemapp-<version>/packaging/install.sh

It prints each step, then the URL (`http://127.0.0.1:8080/`). What it does:

1. Checks the page in the archive against its `bundle.json`.
2. Copies the release to `~/.local/share/tandemapp/releases/<version>-<hash>` and points `current`
   at it.
3. Makes one venv with this host's Python and `cryptography` and `psutil` (never torch). The same
   Python and dependencies share one venv across releases.
4. Writes the state dir `~/.tandemllm` (0700): a fresh master key `secrets.key` (0600) and `app.env`
   (0600) with the address and port, `TANDEM_CHAT=off`, `TANDEM_CONDUCTOR=off` and
   `TANDEM_NODE_AGENT=off`. Also `chatd.env` (2 GiB and 2 running sandboxes), read once Chat is on.
5. Starts the controller: a systemd user unit `tandem-app` on Linux, a launchd agent
   `org.tandemllm.app` on macOS, or a background process where neither answers (a container, a
   shell without a user bus).
6. Waits until `/health` names this release and its page.

Run it again to update. An unchanged release keeps running; a new one is switched to and
restarted, and switched back when it does not come up. `app.env` keeps what it holds.

| option | what |
|-|-|
| `--listen ADDR` | the address to bind: `0.0.0.0`, `::` or one of the host's. An address beyond loopback writes an admin token (`TANDEM_ADMIN_TOKEN`, never printed) into `app.env` and turns the page's sign-in on |
| `--port N` | default 8080 |
| `--chat off\|own\|external` | who runs chatd. A new `app.env` gets `off`; the page switches it later. `own` here runs chatd on this host from the start |
| `--with-sandbox` | Linux, sudo once: rootless Podman for chat's sandboxes (below). It switches nothing on |
| `--service auto\|systemd\|launchd\|process\|none` | how it runs (default: what the host has) |
| `--prefix DIR`, `--state-dir DIR` | where the releases and the state go |
| `--wheels DIR` | install the venv offline from a folder of wheels |
| `--venv DIR` | use a venv that is there |
| `--bundle-url URL --bundle-sha256 HEX`, `--build-page` | a tree without a built page: fetch one, or build it with Node 20.19 or newer |
| `--dry-run` | print what it would do |

On Linux a user unit stops at logout unless the user lingers; the install prints the one command
(`sudo loginctl enable-linger <user>`).

### Behind a reverse proxy

Keep the controller on loopback and let the proxy end TLS. Forward everything to
`http://127.0.0.1:8080`, with streaming on and long timeouts (an answer can take minutes): in
nginx `proxy_buffering off`, `proxy_read_timeout 3600s`, `proxy_set_header X-Real-IP $remote_addr`,
`client_max_body_size 200m`. Do not route `/chat-engine`: that is chatd's way to the gateway on
the same host. Set an admin password before the page is reachable from another machine.
[proxy.md](proxy.md#through-a-reverse-proxy) has the rest.

### Chat's sandboxes

Chat works without a sandbox runtime: its files, shell and browser tools are off, and the page
says so. For sandboxes on Linux:

    bash tandemapp-<version>/packaging/install.sh --with-sandbox

runs `ops/sandbox/install.sh` with sudo once: rootless Podman under its own user `tandem-sbx` with
its socket, the sandbox image, an nftables table that keeps that user off private and loopback
addresses, and POSIX ACLs on chatd's folders. `chatd.env` gets the socket's path. Then switch Chat
on from the page.

### Raspberry Pi

- Raspberry Pi OS (64-bit) or Ubuntu on a Pi 4 or 5 with 8 GB or more. Install the venv module
  first: `sudo apt install python3 python3-venv`.
- Sandboxes need the kernel's memory controller. Raspberry Pi OS ships it off. The install (and
  the health page, `no_memory_cgroup`) says so; the fix is to add
  `cgroup_enable=memory cgroup_memory=1` to the one line of `/boot/firmware/cmdline.txt` and reboot.
  Nothing edits boot files for you.
- Rebuilding the sandbox image later needs the egress table off for the build (its resolver is on
  loopback): `sudo systemctl stop tandem-sbx-nft`, build as `tandem-sbx`, then start it again.
- Keep the state dir on an SSD rather than the SD card (`--state-dir`).

### macOS

The install runs a launchd agent; its log is `~/.tandemllm/logs/tandem-app.log`. macOS asks once
whether the controller may use the local network (Add engine's LAN search needs it). A controller
that launchd starts gets no such prompt: the App page shows the state and the way around it (type
the engine's address, or run the controller on a Linux host). `--with-sandbox` is Linux only; on a
Mac, name Docker Desktop's socket in `chatd.env` (`QSE_SANDBOX_SOCKET=...`). A Mac that sleeps
stops the API: the page warns while a Mac runs the controller.

### From a checkout

`python3 tools/app_release.py pack --out tandemapp-<version>.tar.gz` makes the archive from a tree
with a built page (`python3 tools/dashboard_bundle.py build` builds it with Node). The same tree
packs to the same bytes.

## The container

`packaging/Dockerfile.app` builds an image on `python:3.12-slim` with the page (built in a Node
stage) and the OpenSSH client, and no compiler or torch:

    docker build -f packaging/Dockerfile.app -t tandemllm-app .
    docker run -d --name tandem-app --network host -v tandem-app:/data \
        -e TANDEM_HOST=127.0.0.1 tandemllm-app

`--network host` lets Add engine find the engines the LAN advertises. Behind a published port
(`-p 127.0.0.1:8080:8080`) you type the engine's address instead. Inside the container the server
binds 0.0.0.0 unless `TANDEM_HOST` says otherwise: set an admin password, or
`TANDEM_DASHBOARD_LOGIN=on` with `TANDEM_ADMIN_TOKEN`, before anything but loopback reaches it.

Everything it keeps is in `/data`. A new `/data` starts bare: Chat and the conductor off. The start
script records its choice once in `/data/container.env`; `-e TANDEM_CHAT=own` overrides it, and the
page's switch overrides both. A `/data` that an older image made keeps Chat on. Chat in the
container needs a runtime's socket for its sandboxes:

    docker run ... -v /var/run/docker.sock:/run/tandem/sandbox.sock --group-add <the socket's gid>

or a rootless Podman socket, with `--user` its owner. Without one, chat runs without its sandbox
tools and the health page says so. The System view sees the container, not the host: add
`--pid=host`, or run `tandem-app hostcollect` on the host.
