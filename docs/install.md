# Installing TandemApp

TandemApp is the controller in front of your engines: the gateway (one OpenAI-compatible API), the
usage ledger, the fleet and the page. It needs no GPU and no engine on its own host. This page says
which way to install it, what each way gives you, and what comes after the install. Each way has
its own page with the steps.

## Which way

| way | for | needs | page |
|-|-|-|-|
| one command | a Linux box (arm64 or x86_64, a Raspberry Pi too) or a Mac | Python 3.11 or newer; no root, no Node, no git | [install-linux.md](install-linux.md) |
| one container | any host with Docker or Podman | a container runtime | [install-linux.md](install-linux.md#the-container) |
| Helm | a Kubernetes cluster | Kubernetes 1.27 or newer (1.33 for sandbox pods), a storage class, an ingress controller | [install-kubernetes.md](install-kubernetes.md) |

All three run the same code and the same page, and start the same way: bare.

## What a new install runs

A new TandemApp runs only the gateway in front of the engines you pair (OPS-288):

- no backend until you pair an engine host or add a server;
- Chat off: no chatd, no sandbox runtime needed, and the Chat tab says "Chat is off";
- the conductor off: no profiles, router, packages, notes or decision model in front of the
  backends;
- no MCP server, no hook, no plugin.

Each part is switched on from the page: **Settings → App → What runs here**. Every switch first
shows what it changes and asks to confirm. The switch is stored in the controller's config
database and wins over the install's setting from then on. An install that set a value itself
(`TANDEM_CHAT=own` in `app.env`, `chat.mode: own` in Helm values) keeps it: a default changed in a
later release never flips a running install.

| part | on | off |
|-|-|-|
| Chat | chatd runs on the controller's host (its data in the state dir); with no sandbox runtime chat works without its sandbox tools, and the page says so | chatd stops (a chat that runs ends `interrupted`, Continue resumes it later); the chats stay |
| Conductor | the harness in front of the backends, from the next request; its documents in the folder `TANDEM_CONDUCTOR` names, else `<state dir>/conductor` | every backend relayed as it is; the documents stay |

The engine's own chat mode (the conductor and chat extension inside an engine) is the engine's
setting. The same page lists the engine hosts paired over SSH and switches each one
(`tandem remote mode set engine|chat` on the host does the same). It also lists what is connected
(backends, and Chat's MCP servers while Chat is on), each with disable and remove.

## The first page

Open the URL the install prints (`http://127.0.0.1:8080/` for the one command). A TandemApp with no
engine opens the setup wizard:

1. **Add engine.** It lists the engines the LAN advertises (mDNS `_tandem._tcp`), or takes an
   address you type (a tailnet name too).
2. **Pair.** On the engine host, `tandem pair` prints a 6-digit code, the SSH host key's
   fingerprint and the address. Type the code. The code authenticates the host key: nothing is
   trusted on first use. A code is good for 5 minutes and 5 tries, and works once. The other ways:
   a password used once (never stored), or a key line to paste.
3. **Model.** The bare engine is preselected; pick a model and follow its download.
4. **First answer.** With Chat on, the message goes to the Chat tab. With Chat off (the default),
   the Playground asks the engine through the gateway, or the page switches Chat on.

Each engine host gets its own SSH key pair, sealed with the controller's master key. Pairing needs
the admin from another machine: set an admin password first (Settings → App), or pair from the
controller's own host. More engines: Settings → Add engine. A Spark with TandemNode, or any
OpenAI-compatible server by its URL: Settings → Get started.

## Where state lives

| way | state | what is in it |
|-|-|-|
| one command | `~/.tandemllm` (`--state-dir`), mode 0700; releases in `~/.local/share/tandemapp` (`--prefix`) | `app.env`, `config.sqlite3` (backends, routes, users, settings and the page's switches), `ledger.sqlite3`, `fleet.sqlite3`, `secrets.key` (the master key), `admin.json` (the admin password's hash), `controller/` (its certificate, the engine hosts' SSH keys and pins), `chatd.env`, `chat/` once Chat ran, `conductor/` once the conductor ran, `logs/` |
| container | the `/data` volume | the same, plus `container.env` (the chat mode its first start chose) |
| Helm | one ReadWriteOnce volume at `/var/lib/tandemapp` | `state/` (the same files), `data/chat`, `conductor/`, `releases/<id>` and `current`, `ca/`, `home/`, `incoming/`, `backups/` |

## Upgrades

- One command: unpack the new release archive and run its `packaging/install.sh` again. It switches
  to the new release, restarts, and switches back when the new one does not answer `/health`. The
  old releases stay. `app.env` keeps its values; only `--listen`, `--port` and `--chat` change it.
- Container: pull or build the new image and recreate the container on the same volume.
- Helm: `helm upgrade` with the new `release.id` and archive; `helm rollback` goes back. See
  [install-kubernetes.md](install-kubernetes.md#upgrades-and-rollback).

## Backups

Stop the controller, or copy the SQLite files with `sqlite3 <file> ".backup <copy>"` while it runs,
and keep the whole state dir (or volume) together. `secrets.key` is the master key: without it the
stored API keys, the engine hosts' keys and chat's MCP secrets cannot be read. Keep it apart from
the copies, the way you keep any key. The page's export (Settings → App → Fleet configuration) is a
file of the registry without any secret: good for a record, not a backup.

## Uninstall

- One command: `systemctl --user disable --now tandem-app` and remove
  `~/.config/systemd/user/tandem-app.service` (Linux), or `launchctl bootout gui/$(id -u)/org.tandemllm.app`
  and remove `~/Library/LaunchAgents/org.tandemllm.app.plist` (macOS). Then remove the prefix
  folder. The state dir stays until you remove it.
- Container: `docker rm -f tandem-app`; the volume stays until `docker volume rm`.
- Helm: `helm uninstall`; the volume's claim stays (`persistence.keep`) until you delete it.

## Reference

- [install-linux.md](install-linux.md): the one command, a Raspberry Pi, macOS, the container.
- [install-kubernetes.md](install-kubernetes.md): Helm, the release archive, sandboxes as pods.
- [../charts/tandemapp/README.md](../charts/tandemapp/README.md): the chart's values.
- [proxy.md](proxy.md): the gateway, its configuration and every variable.
- [chat.md](chat.md): Chat and its sandboxes.
