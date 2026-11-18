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
| `--listen ADDR` | the address to bind: `0.0.0.0`, `::` or one of the host's. An address beyond loopback writes no secret and prints a one-time setup code (below) |
| `--behind-proxy` | a reverse proxy or tunnel on this host sends other devices here: with accounts, this machine's browser is never trusted without a password (`TANDEM_LOCAL_MODE=off`) and the summary prints a setup code |
| `--port N` | default 8080 |
| `--chat off\|own\|external` | who runs chatd. A new `app.env` gets `off`; the page switches it later. `own` here runs chatd on this host from the start |
| `--with-sandbox` | Linux, sudo once: rootless Podman for chat's sandboxes (below). It switches nothing on |
| `--service auto\|systemd\|launchd\|process\|none` | how it runs (default: what the host has) |
| `--unit NAME`, `--launchd-label LABEL` | the systemd user unit's name (default `tandem-app`), the launchd job's label (default `org.tandemllm.app`). A second install on the same host needs its own name, `--state-dir` and `--port` |
| `--prefix DIR`, `--state-dir DIR` | where the releases and the state go |
| `--wheels DIR` | install the venv offline from a folder of wheels |
| `--venv DIR` | use a venv that is there |
| `--bundle-url URL --bundle-sha256 HEX`, `--build-page` | a tree without a built page: fetch one, or build it with Node 20.19 or newer |
| `--dry-run` | print what it would do |

Nothing is changed when a unit or launchd job of that name runs another prefix or state dir, or
when another listener holds the port: the install stops (exit 2) and names both.

On Linux a user unit stops at logout unless the user lingers; the install prints the one command
(`sudo loginctl enable-linger <user>`).

### Sign-in with accounts

User accounts with roles, sessions and API tokens are the sign-in (there is no admin token and
no open default any more; an older install's variables are named on the health page and can go
once you will not roll back). [auth.md](auth.md) has the roles, tokens and recovery steps.

- **This machine only** (the default address, no proxy): local mode. The page opens without a
  password and offers "Set a password"; nothing else on the network reaches it. A tool on this
  machine needs no API key: a made-up one counts as none, and an API token as itself. The
  installer prints no code.
- **Beyond loopback** (`--listen`), or **behind a proxy** (`--behind-proxy`): no local mode
  (`TANDEM_LOCAL_MODE=off`). Nobody signs in until the first owner is made. The installer's
  summary prints a one-time setup code and the address; open the page and enter it with the
  owner's name and password. The code is single use, valid 24 hours and spent after 5 wrong
  tries. While no owner exists each start of the controller logs a fresh one, and
  `tandem-app users setup-code` prints another.
- **A lost password:** `tandem-app users reset-password NAME` on this machine prints a one-time
  code; sign in with it as the password, then set one. Whoever can read the state directory can
  do this, so no password is asked.
- **API exposure.** The OpenAI-compatible API and the Messages API each have a switch on the page:
  "this machine" (the default) or "network". Other devices need a token and the switch on
  network; an install that had API keys starts with the OpenAI switch on network so its clients
  keep working. A controller on loopback is not reachable from other devices whatever the switch
  says: change the listen address too.
- **A proxy that ends TLS** on another address: name it in `TANDEM_TRUSTED_PROXY` (addresses
  and CIDR ranges, IPv4 and IPv6, comma separated, e.g. `192.0.2.10,2001:db8::/64`), or every API
  token through it counts as sent over plain HTTP. A bad entry is ignored and named on the health
  page.
- `tandem-app service install --host 0.0.0.0` refuses while no owner has a password, unless
  `--setup-code`, which prints a code. `tandem-app check` says where the sign-in stands.
- An install that had the admin token keeps it working for scripts for 30 days, as an API token
  marked for review. It never signs in to the page. [auth.md](auth.md#moving-from-the-old-sign-in)
  lists the old variables; remove them from `app.env` once you will not go back to an older
  release. The health page names each one still set.

### Behind a reverse proxy

Keep the controller on loopback and let the proxy end TLS. Forward everything to
`http://127.0.0.1:8080`, with streaming on and long timeouts (an answer can take minutes): in
nginx `proxy_buffering off`, `proxy_read_timeout 3600s`, `proxy_set_header X-Real-IP $remote_addr`,
`client_max_body_size 200m`. Do not route `/chat-engine`: that is chatd's way to the gateway on
the same host. Set an admin password before the page is reachable from another machine; with
accounts install with `--behind-proxy`, so the proxy's requests are never taken for this machine.
A proxy that forwards only some paths must still send TandemApp every prefix in
[install.md](install.md#behind-a-reverse-proxy); [proxy.md](proxy.md#through-a-reverse-proxy) has the rest.

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
binds 0.0.0.0 unless `TANDEM_HOST` says otherwise. A container never gets local mode: nothing
answers without a sign-in, and the first log lines hold a setup code that makes the owner
(`docker logs tandem-app`).

Everything it keeps is in `/data`. A new `/data` starts bare: Chat and the conductor off. The start
script records its choice once in `/data/container.env`; `-e TANDEM_CHAT=own` overrides it, and the
page's switch overrides both. A `/data` that an older image made keeps Chat on. Chat in the
container needs a runtime's socket for its sandboxes:

    docker run ... -v /var/run/docker.sock:/run/tandem/sandbox.sock --group-add <the socket's gid>

or a rootless Podman socket, with `--user` its owner. Without one, chat runs without its sandbox
tools and the health page says so. The System view sees the container, not the host: add
`--pid=host`, or run `tandem-app hostcollect` on the host.

To build the image yourself from a checkout and run it:

```sh
docker build -f packaging/Dockerfile.app -t tandemllm-app app
cp packaging/app.env.example app.env  # fill in; no quotes around values
docker run -d --name tandem-app -p 127.0.0.1:8080:8080 -v tandem-app:/data \
    --env-file app.env tandemllm-app
```
