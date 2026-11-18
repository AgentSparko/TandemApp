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

## From a checkout

```sh
git clone https://github.com/TandemLLM/TandemApp.git && cd TandemApp
pip install '.[host]'  # the host extra adds psutil and NVML for the System view
tandem-app                 # http://127.0.0.1:8080/dashboard/ opens on Setup
```

With backends declared in an env file instead of the page:

```sh
cat > app.env <<'EOF'
TANDEM_BACKENDS=vllm1
TANDEM_BACKEND_VLLM1_KIND=vllm
TANDEM_BACKEND_VLLM1_URL=http://127.0.0.1:8100/v1
EOF
tandem-app check --env-file app.env        # prints what would run, writes nothing
tandem-app serve --env-file app.env
```

A first backend from the command line, a cloud API with its key, and one request through it:

```sh
printf 'TANDEM_BACKENDS=deepseek\nTANDEM_BACKEND_DEEPSEEK_KIND=deepseek\nTANDEM_BACKEND_DEEPSEEK_KEY=
    "$DEEPSEEK_API_KEY" > app.env && chmod 600 app.env
tandem-app serve --env-file app.env &
curl -s localhost:8080/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model": "deepseek-flash", "messages": [{"role": "user", "content": "Say hi"}]}'
```

Then open `http://127.0.0.1:8080/dashboard/#/usage`: the request is one row there, with its tokens, its time to first token and its cost at the model's price. [backends.md](backends.md) has a section for each kind of backend.

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

## Behind a reverse proxy

A reverse proxy in front of TandemApp must send it every path the controller owns, not only the
data plane. The page calls all of them from the same address: a proxy that sent `/v1/settings`
or `/v1/fleet/` to an engine leaves the Settings, Hooks, Decision providers, Plugins and Health
pages with the engine's 404 (OPS-292). The simple setup forwards everything to TandemApp and
blocks `/chat-engine` and `/plugin-backends`. A proxy that also fronts an engine on the same
address sends TandemApp every prefix it answers always, a "both" prefix while its condition
holds, and the rest to the engine.

"Both" means TandemApp and an engine each answer the path for their own state. TandemApp answers
`/v1/conductor`, `/v1/library` and the session addresses only while its own conductor is on
(Settings → App → What runs here; a new install has it off); an engine answers them only through
the extension that runs the conductor in its process, and its engine-only screens are under
`/v1/engine`. [proxy.md](proxy.md#through-a-reverse-proxy) says when the data plane goes to
TandemApp. Block `/plugin-backends` even when the proxy runs on TandemApp's own host: the
controller takes it from loopback only, and a proxy there connects from loopback.

| prefix | answered by | TandemApp answers it | what |
|-|-|-|-|
| `/` | TandemApp | always | redirects to the page |
| `/dashboard` and below | TandemApp | always | the page and its files |
| `/v1/fleet` and below | TandemApp | always | the fleet: nodes, engine hosts, deployments, health, hooks, decision providers, plugins, the App page, the gateway for a page whose /v1 is an engine's |
| `/v1/settings` and below | TandemApp | always | the settings interface: every scope, change sets, versions, the audit |
| `/v1/config` and below | TandemApp | always | the Backends and Setup screens: backends, rules, prices, users |
| `/v1/auth` and below | TandemApp | always | the sign-in, the first-run setup, sessions and accounts |
| `/v1/chat-app` and below | TandemApp | always | the Chat tab: chatd behind the controller (off, own or external) |
| `/v1/collect` and below | TandemApp | always | a plugin's or a host collector's batches, with its own token |
| `/plugin-panels` and below | TandemApp | always | a running plugin's panels, in the page's frame |
| `/v1/chat/completions` | both | always | the data plane: TandemApp's gateway (proxy.md says when a proxy sends it to TandemApp or to an engine) |
| `/v1/completions` | both | always | the data plane |
| `/v1/embeddings` | both | always | the data plane |
| `/v1/responses` | both | always | the data plane |
| `/v1/messages` and below | TandemApp | always | the data plane in the Messages API: a message, count_tokens; an engine answers 404 |
| `/v1/models` and below | both | always | the data plane: the model list and one model's card |
| `/v1/cache/peek` | both | when a deployment routes the cache | routed to the member of a Replicas deployment that holds the conversation |
| `/v1/cache/prewarm` | both | when a deployment routes the cache | as peek |
| `/v1/cache/release` | both | when a deployment routes the cache | as peek |
| `/v1/cache` and below | an engine | never | an engine's cache routes; clear is the engine's only |
| `/v1/dashboard` and below | both | always | summary, usage, requests, live, logs, metrics, the page's sign-in: TandemApp's own ledger (a page whose /v1/dashboard reaches an engine reads TandemApp's under /v1/fleet/ledger, proxy.md) |
| `/v1/conductor` and below | both | when its conductor is on | the conductor's documents, journal, sessions and memory notes; an engine answers it only through the extension that runs the conductor in its process, and the engine-only screens are /v1/engine |
| `/v1/library` and below | both | when its conductor is on | the package library |
| `/v1/laya` and below | both | when its conductor is on and the decider role names a Laya endpoint | Laya for scripts |
| `/s` and below | both | when its conductor is on | a session's address: /v1/library and /v1/laya under its token; /v1/memory under it is the engine's only |
| `/health` | both | always | liveness: each answers its own |
| `/metrics` | both | always | Prometheus: each answers its own |
| `/v1/engine` and below | an engine | never | the engine-only screens: deciders, narrator, shadow, disk tier, residency, pairing |
| `/v1/setup` and below | an engine | never | an engine's Setup Guide; through TandemApp: /v1/fleet/nodes/<id>/setup |
| `/v1/memory` and below | an engine | never | the model's own memory under a session's address |
| `/v1/tokenize` | an engine | never | the engine's tokenizer |
| `/v1/systemone` | an engine | never | the engine's decision wire |
| `/v1/replica` and below | an engine | never | a replica's identity |
| `/readyz` | an engine | never | the engine's readiness |
| `/metrics/up` | an engine | never | the engine's up gauge |
| `/_tandem` and below | an engine | never | the engine's status responder while it restarts |
| `/chat-engine` and below | nobody: block it | never | chatd's way to the gateway on the controller's own host: block it at the proxy |
| `/plugin-backends` and below | nobody: block it | never | a plugin's backend, for the controller's own gateway: it is taken from loopback only, and a proxy on the same host connects from loopback, so block it |

The same list is machine-readable in [controller-routes.json](controller-routes.json);
`tests/test_route_list.py` checks it against the paths the page calls and the routes the
controller answers. Streaming answers need buffering off and long timeouts at the proxy
([install-linux.md](install-linux.md#behind-a-reverse-proxy),
[install-kubernetes.md](install-kubernetes.md#5-ingress-tls-and-a-reverse-proxy-in-front)).

## Where state lives

| way | state | what is in it |
|-|-|-|
| one command | `~/.tandemllm` (`--state-dir`), mode 0700; releases in `~/.local/share/tandemapp` (`--prefix`) | `app.env`, `config.sqlite3` (backends, routes, users, settings and the page's switches), `ledger.sqlite3`, `fleet.sqlite3`, `secrets.key` (the master key), `admin.json` (the admin password's hash), `controller/` (its certificate, the engine hosts' SSH keys and pins), `chatd.env`, `chat/` once Chat ran, `conductor/` once the conductor ran, `logs/` |
| container | the `/data` volume | the same, plus `container.env` (the chat mode its first start chose) |
| Helm | one ReadWriteOnce volume at `/var/lib/tandemapp` | `state/` (the same files), `data/chat`, `conductor/`, `releases/<id>` and `current`, `ca/`, `home/`, `incoming/`, `backups/` |

Each page's window layout per device class is kept in the config database too (`layouts/<page>`); the last write wins.

## Upgrades

- One command: unpack the new release archive and run its `packaging/install.sh` again. It switches
  to the new release, restarts, and switches back when the new one does not answer `/health`. The
  old releases stay. `app.env` keeps its values; only `--listen`, `--port` and `--chat` change it.
- Container: pull or build the new image and recreate the container on the same volume.
- Helm: `helm upgrade` with the new `release.id` and archive; `helm rollback` goes back. See
  [install-kubernetes.md](install-kubernetes.md#9-upgrades-and-rollback).

## Backups

Stop the controller, or copy the SQLite files with `sqlite3 <file> ".backup <copy>"` while it runs,
and keep the whole state dir (or volume) together. `secrets.key` is the master key: without it the
stored API keys, the engine hosts' keys and chat's MCP secrets cannot be read. Keep it apart from
the copies, the way you keep any key. The page's export (Settings → App → Fleet configuration) is a
file of the registry without any secret: good for a record, not a backup.

## Uninstall

- One command: `systemctl --user disable --now tandem-app` and remove
  `~/.config/systemd/user/tandem-app.service` (Linux), or `launchctl bootout gui/$(id -u)/org.tandemllm.app`
  and remove `~/Library/LaunchAgents/org.tandemllm.app.plist` (macOS); an install made with
  `--unit` or `--launchd-label` uses that name instead (the install's summary prints its stop
  command). Then remove the prefix folder. The state dir stays until you remove it.
- Container: `docker rm -f tandem-app`; the volume stays until `docker volume rm`.
- Helm: `helm uninstall`; the volume's claim stays (`persistence.keep`) until you delete it.

## Reference

- [install-linux.md](install-linux.md): the one command, a Raspberry Pi, macOS, the container.
- [install-kubernetes.md](install-kubernetes.md): Helm, the release archive, sandboxes as pods.
- [../charts/tandemapp/README.md](../charts/tandemapp/README.md): the chart's values.
- [proxy.md](proxy.md): the gateway, its configuration and every variable.
- [chat.md](chat.md): Chat and its sandboxes.
