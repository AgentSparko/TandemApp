# Documentation

| page | what it covers |
|-|-|
| [install.md](install.md) | installing TandemApp: which way for whom, what a new install runs (the gateway only; Chat and the conductor switched on from the page), the first page, where state lives, upgrades, backups, uninstall |
| [install-linux.md](install-linux.md) | the one command on Linux (a Raspberry Pi too) and macOS, its options, chat's sandboxes, and the container |
| [install-kubernetes.md](install-kubernetes.md) | Helm on Kubernetes: the release archive built reproducibly, the install, ingress and TLS, the values, Chat with sandbox pods, the MCP bridge sidecar, upgrades and rollback, troubleshooting |
| [proxy.md](proxy.md) | TandemApp: backends, configuration, keys, the fleet, TandemNode, Add Spark, the data plane, install |
| [dashboard.md](dashboard.md) | the Live activity view, the lanes, a request held for memory, the log export, the other views, the contract, access |
| [chat.md](chat.md) | the Chat tab (in beta) and chatd, the service behind it: who starts it, the sandbox, the permission modes, Agents mode, the known limitations |
| [conductor.md](conductor.md) | the conductor in one page: modes and headers, profiles, the router, effort, packages, the note, side requests, sessions, the monitor and loop guards, the journal, measured effect |
| [settings.md](settings.md) | one settings interface over the stores: scopes, change sets, versions and restore, engine restarts, audit |
| [chat-mode.md](chat-mode.md) | the conductor inside a TandemLLM engine: the `TandemLLM` model, status lines and the narrator, the package library, Laya for the model's scripts, the session monitor's rules |
| [packages.md](packages.md) | the packages the conductor attaches by class: their shape, the first packages, how each is measured |
| [memory.md](memory.md) | memory notes: what the model keeps about the user and the work |
| [hooks.md](hooks.md) | hooks: your code at each stage of a request (before routing, before the engine, on the answer, on an error), their order and limits, payload levels, signed webhooks in Python and Node, shadow mode |
| [plugins.md](plugins.md) | plugins: write one (a folder with plugin.json and an MCP server), the manifest, the checks an install makes, capabilities, install from a folder, a wheel or a git commit, enable, upgrade, remove; running them: trust levels, the protocol (MCP plus hooks/call, decision/ask, backend/request), each contribution's wiring, sandboxed plugins, debugging |
| [plugins-sdk.md](plugins-sdk.md) | the plugin SDK: a plugin in Python as decorated tools, hooks, decision providers and backends, `tandemapp-plugin new`, the two examples built on it |
| [decision-providers.md](decision-providers.md) | decision providers: your own model in place of the conductor's decider, per question; the systemone wire with an example server, keys, shadow and A/B, health and fallbacks, the agreement report, replay, hooks as adapters |
| [threat-model.md](threat-model.md) | where untrusted input enters TandemApp, the parts that matter most for security |

The manual, `manual/tandemapp-manual.tex` (`make` in `manual/` builds the PDF), has the install, the backends, the dashboard view by view, the Chat tab, the hosts and the reference tables generated from the code.

The API contracts are under [../contracts](../contracts): the engine API (`engine-api-v1`), the engine extension interface (`engine-ext-v1`), the fleet (`fleet-v1`), the dashboard (`dashboard-v1`), the conductor (`conductor-v1`), the hooks (`hooks-v1`), the settings interface (`settings-v1`) and the plugins (`plugin-v1`). The engine has its own documentation in its repository ([TandemEngine](https://github.com/TandemLLM/TandemEngine/blob/main/docs/README.md)); on these pages a path under `server/`, `engine/` or `models/` names a file of the engine's repository.

The pages in this folder are CC BY 4.0 ([LICENSE](LICENSE)).
