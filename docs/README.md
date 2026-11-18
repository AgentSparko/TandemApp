# Documentation

TandemApp is one address and one dashboard in front of any number of engines and cloud APIs: the gateway, the dashboard, the Chat tab, the conductor and the fleet of engine hosts. These pages are the whole documentation; each covers one part and names the files that implement it. To install, start with [install.md](install.md); to put a first engine or cloud API behind it, [backends.md](backends.md).

## Installing

| page | what it covers |
|-|-|
| [install.md](install.md) | installing TandemApp: which way for whom, from a checkout, what a new install runs (the gateway only; Chat and the conductor switched on from the page), the first page, the prefixes a reverse proxy sends to TandemApp ([controller-routes.json](controller-routes.json)), where state lives, upgrades, backups, uninstall |
| [install-linux.md](install-linux.md) | the one command on Linux (a Raspberry Pi too) and macOS, its options, chat's sandboxes, and the container |
| [install-kubernetes.md](install-kubernetes.md) | Helm on Kubernetes: the release archive built reproducibly, the install, ingress and TLS, the values, Chat with sandbox pods, the MCP bridge sidecar, upgrades and rollback, troubleshooting |
| [auth.md](auth.md) | sign-in, accounts and roles, API tokens and their scopes, who may use the API from other devices, recovery |

## Backends, the gateway and the dashboard

| page | what it covers |
|-|-|
| [backends.md](backends.md) | engines and cloud APIs behind TandemApp: the Add backend form and its providers, the kinds, vLLM, SGLang, `tandem-app launch`, any OpenAI-compatible server, OpenAI, Anthropic, DeepSeek, another TandemEngine, routing by model id, fallbacks, keys at rest, prices and cost, what is measured |
| [proxy.md](proxy.md) | the gateway in depth: the Messages API and its translation, configuration, keys, the fleet, TandemNode, Add Spark, the data plane, the harness in front of a backend |
| [monitoring.md](monitoring.md) | several engines at once: every request in flight, vLLM's and SGLang's metrics pages (L1), their plugins (L2), the machine behind each backend, your own Prometheus |
| [dashboard.md](dashboard.md) | the dashboard view by view, the Live activity panel, the lanes, a request held for memory, the log export, the contract, access |
| [chat.md](chat.md) | the Chat tab (in beta) and chatd, the service behind it: who starts it, the sandbox, the port preview, the permission modes, Agents mode, the known limitations |
| [settings.md](settings.md) | one settings interface over the stores: scopes, change sets, versions and restore, engine restarts, audit |

## The conductor

| page | what it covers |
|-|-|
| [conductor.md](conductor.md) | the conductor in one page: modes and headers, profiles, the router and its classes, effort, packages, the note, side requests, sessions, the journal, measured effect |
| [session-monitor.md](session-monitor.md) | the session monitor: modes and policy, safe points and actions, every task, reasoning loops, the tool-call checks, the claim check, turn outcomes, session tasks, extending it |
| [status-lines.md](status-lines.md) | status lines in the client, the narrator and the voice, the shadow reader |
| [chat-mode.md](chat-mode.md) | the conductor inside a TandemEngine: the `TandemLLM` model, the package library, Laya for the model's scripts, the session monitor's rules |
| [packages.md](packages.md) | the packages the conductor attaches by class: their shape, the first packages, how each is measured |
| [memory.md](memory.md) | memory notes: what the model keeps about the user and the work, how it writes and reads them |

## Extending it

| page | what it covers |
|-|-|
| [hooks.md](hooks.md) | hooks: your code at each stage of a request (before routing, before the engine, on the answer, on an error), their order and limits, payload levels, signed webhooks in Python and Node, shadow mode |
| [plugins.md](plugins.md) | plugins: write one (a folder with plugin.json and an MCP server), the manifest, capabilities, install, trust, the protocol, contributions, sandboxed plugins, debugging |
| [plugins-sdk.md](plugins-sdk.md) | the plugin SDK: a plugin in Python as decorated tools, hooks, decision providers and backends, `tandemapp-plugin new`, the two examples built on it |
| [decision-providers.md](decision-providers.md) | decision providers: your own model in place of the conductor's decider, per question; the systemone wire, shadow and A/B, health and fallbacks, the agreement report |

## Reference

| page | what it covers |
|-|-|
| [reference/commands.md](reference/commands.md) | every flag of `tandem-app serve`, `launch` and `hostcollect` |
| [reference/variables.md](reference/variables.md) | every `TANDEM_*` variable, the `QSE_*` variables TandemApp reads, the `QWEN38_*` knobs of the conductor inside an engine |
| [reference/http.md](reference/http.md) | every route, who may call it, every header and every error |
| [reference/metrics.md](reference/metrics.md) | every Prometheus family, and the usage ledger's columns |
| [troubleshooting.md](troubleshooting.md) | symptoms, causes and what to do: the dashboard, the Chat tab, vLLM and SGLang, the gateway |
| [glossary.md](glossary.md) | TandemApp's terms |
| [threat-model.md](threat-model.md) | where untrusted input enters TandemApp, the parts that matter most for security |
| [gallery/README.md](gallery/README.md) | the dashboard's pictures the README and the site use |
| [CHANGELOG.md](../CHANGELOG.md) | every release |

`tools/gen_reference.py` writes the reference pages' tables from the code, between marked blocks. `python3 tools/gen_reference.py --check`, which `tests/test_docs_reference.py` runs, fails while a table is stale or the HTTP reference lacks a route or header the code has.

The API contracts are under [../contracts](../contracts): the engine API (`engine-api-v1`), the engine extension interface (`engine-ext-v1`), the fleet (`fleet-v1`), the dashboard (`dashboard-v1`), the conductor (`conductor-v1`), the hooks (`hooks-v1`), the settings interface (`settings-v1`) and the plugins (`plugin-v1`). The engine has its own documentation in its repository ([TandemEngine](https://github.com/TandemLLM/TandemEngine/blob/main/docs/README.md)); on these pages a path under `server/`, `engine/` or `models/` names a file of the engine's repository.

The pages in this folder are CC BY 4.0 ([LICENSE](LICENSE)).
