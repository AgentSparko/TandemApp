# Reference: commands

The flags of every TandemApp command. `tools/gen_reference.py` reads them from the argument parsers, and `python3 tools/gen_reference.py --check` fails while a table is stale. `tandem-app serve` and `python -m proxy` take the same flags; `tandem-app check`, `secrets` and `backends` take them too and add their own action ([install.md](../install.md) has every `tandem-app` subcommand). The engine's flags are in [TandemEngine's reference](https://github.com/TandemLLM/TandemEngine/blob/main/docs/reference/commands.md).

<!-- generated: proxy-flags by tools/gen_reference.py; do not edit by hand -->

### tandem-app serve and python -m proxy

| Flag | Default | What it does |
|-|-|-|
| `--config-db` | `~/.tandemllm/config.sqlite3 (TANDEM_CONFIG_DB)` | the config database (created when missing). |
| `--env-file` | `none` | KEY=VALUE lines read before the start; the process environment wins. |
| `--host` | `127.0.0.1 (TANDEM_HOST)` | the address to bind. |
| `--port` | `8080 (TANDEM_PORT)` | the port to listen on. |
| `--usage-ledger` | `~/.tandemllm/ledger.sqlite3 (TANDEM_USAGE_LEDGER)` | the usage ledger (one row per request, no text); 'off' for none. |
| `--usage-retention-days` | `400` | days the usage ledger keeps its rows (at least 400). |
| `--fleet-store` | `beside (TANDEM_FLEET_STORE)` | the fleet's metric history, event log and alerts (SQLite); 'beside' puts fleet.sqlite3 next to the config database, 'off' keeps them in memory only. |
| `--fleet-retention-days` | `30 (TANDEM_FLEET_RETENTION_DAYS)` | days of 10-minute history, events and resolved alerts (at least 7). |
| `--dashboard-login` | `none` |   |
| `--dashboard-dir` | `none` | default: dashboard/dist. |
| `--trust-loopback` | `none` |   |
| `--no-keychain` | `off (a switch)` | do not ask the OS keychain for the master key. |
| `--conductor-config` | `~/.tandemllm/conductor (TANDEM_CONDUCTOR)` | the conductor's documents (profiles, router, packages) for the harness in front of the backends; 'off' relays every backend as does. |
| `--idle-timeout` | `60 (TANDEM_IDLE_TIMEOUT_S)` | seconds a client may take to send its request, or stay idle between requests, before the connection is closed (an answer has no timeout); 0 for none. |
| `--chat` | `external (TANDEM_CHAT)` | who runs chatd, the Chat tab's service: off (nobody: Chat is off until an admin switches it on from the page; what a new install writes), external (TandemNode, an engine's launchers or a unit; the default when unset) or own (this ... One of: off, external, own. |
| `--conductor-defaults` | `shipped` | what a new conductor directory starts with. One of: shipped, builtin. |
| `--verbose` | `off (a switch)` | more log lines. |

<!-- /generated -->

<!-- generated: launch-flags by tools/gen_reference.py; do not edit by hand -->

### tandem-app launch

| Flag | Default | What it does |
|-|-|-|
| `ENGINE` |   | the engine to start: vllm or sglang. |
| `--model` | `required` | the model id or path the engine serves. |
| `--port` | `none` | the engine's port. |
| `--host` | `127.0.0.1` | the engine's bind address. |
| `--id` | `none` | the backend id (default: the engine's name and port, e.g. vllm8100). |
| `--label` | `none` | the backend's label in the dashboard. |
| `--config-db` | `~/.tandemllm/config.sqlite3 (TANDEM_CONFIG_DB)` | the proxy's config database the backend is registered in. |
| `--collect-url` | `none (TANDEM_COLLECT_BASE)` | the proxy address the plugin posts to; default: this machine's proxy on its port. |
| `--engine-cmd` | `none` | the engine's command instead of `vllm serve` / `sglang serve`. |
| `--python` | `none` | the Python of the engine's environment, for the plugin check (default: the one behind the engine's command). |
| `--no-register` | `off (a switch)` | do not create or update the backend. |
| `--dry-run` | `off (a switch)` | print, start nothing. |

<!-- /generated -->

<!-- generated: hostcollect-flags by tools/gen_reference.py; do not edit by hand -->

### tandem-app hostcollect

| Flag | Default | What it does |
|-|-|-|
| `--host` | `127.0.0.1` | the address to bind; beyond loopback it needs a token. |
| `--port` | `9108` | the collector's port. |
| `--token-file` | `none` | a file whose first line is the bearer token; else TANDEM\_COLLECTOR\_TOKEN. |
| `--watch` | `none` | ports to report always, as id:port,id:port (a request may add more). |
| `--path` | `[]` | a path whose filesystem to report (repeatable). |

<!-- /generated -->
