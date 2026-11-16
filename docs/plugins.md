# Plugins

A plugin is a folder with a manifest, `plugin.json`, and a program that speaks MCP over stdio. It can add tools, hooks, decision providers, backends, page panels, skills and in-engine hooks. TandemApp installs it from a folder, a wheel or a git commit, checks it, records the sha256 of its files, and keeps its settings, secrets and granted capabilities.

The contract is [`contracts/plugin-v1`](../contracts/plugin-v1/README.md).

Once enabled, the plugin host runs it as one process and wires in what it contributes while it runs ([running plugins](#running-plugins)).

## Write one

The smallest plugin is [`examples/hello-tool`](../contracts/plugin-v1/examples/hello-tool): one MCP tool that greets a name.

```
hello-tool/
  plugin.json     the manifest
  server.py       an MCP server over stdio, the standard library only
```

```json
{
 "plugin_api": "1.0",
 "name": "hello-tool",
 "version": "0.1.0",
 "tandemapp": ">=0.15",
 "hooks_api": "1.0",
 "entry": {"command": ["python3", "server.py"]},
 "contributes": {
  "tools": [{
   "name": "greet",
   "description": "Say hello to someone by name.",
   "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                   "required": ["name"], "additionalProperties": false},
   "annotations": {"readOnlyHint": true, "destructiveHint": false,
                   "idempotentHint": true, "openWorldHint": false}
  }]
 },
 "settings": {"type": "object", "additionalProperties": false,
              "properties": {"greeting": {"type": "string", "default": "Hello"}}}
}
```

Check it the way an install does, without installing it:

```
python tools/plugin_check.py path/to/hello-tool
ok hello-tool 0.1.0 sha256 7c51... (2 files)
```

Two more examples: [`audit-hook`](../contracts/plugin-v1/examples/audit-hook) (an observer hook at `request.finished`) and [`classify-provider`](../contracts/plugin-v1/examples/classify-provider) (a decision provider for `classify`, with a secret and a network host).

## The manifest

Every field not listed here is refused, not ignored.

| field | what |
|-|-|
| `plugin_api` | the plugin-v1 version the manifest is written for, `1.0` |
| `name` | the plugin's id: lowercase letters, digits and dashes, at most 48. One plugin of a name. |
| `version` | `MAJOR.MINOR.PATCH`, with an optional `-pre` |
| `kind` | `plugin` (default): a program the plugin host runs. `engine-stats`: a package an engine loads to send its figures; recorded only. |
| `title`, `description`, `author`, `license`, `homepage` | for the page |
| `tandemapp` | the TandemApp versions it runs on, e.g. `>=0.15, <0.17` (`>=`, `>`, `<=`, `<`, `==`, `!=`, `~=`, `==0.16.*`) |
| `hooks_api` | the hooks-v1 version it speaks (kind `plugin`) |
| `entry` | `{command: [argv...], env?: {NAME: value}}`: started from the manifest's folder, never through a shell (kind `plugin`) |
| `trust` | `sandboxed` (default), `host` or `engine`: where it asks to run |
| `contributes` | `hooks`, `tools`, `decision_providers`, `backends`, `panels`, `skills`, `engine_hooks` (below) |
| `settings` | a JSON schema of type object: the plugin's settings, drawn as a form |
| `secrets` | `[{name, description?}]`, names like `API_KEY` |
| `capabilities` | what it asks for (below) |
| `engine_stats` | kind `engine-stats`: `{engine, package, entry_points, install?, env?}` |
| `git` | `{submodules: "ignore"}`, `{lfs: "ignore"}`: see [git sources](#install) |

### Contributions

| key | each item |
|-|-|
| `tools` | an MCP tool: `name`, `description`, `inputSchema`, `annotations`, optional `title` and `outputSchema` |
| `hooks` | `name`, `stages` (hooks-v1 stages), `role` (transform, guard, decider, observer: one the stage takes), `payload` (`meta` by default) |
| `decision_providers` | `name`, `questions` (the conductor's: classify, packages, deliverable, memory_intent, memory_gate, judge) |
| `backends` | `name`, `description`, `models` |
| `panels` | `name`, `title`, `path`: an HTML file in the plugin, shown in a sandboxed frame |
| `skills` | folders in the plugin, each holding `<skill>/SKILL.md`; each becomes one more skill folder |
| `engine_hooks` | `name`, `wheel` (a .whl in the plugin), `hooks` (the engine extension's: plan, prompt, at_request, at_block, at_tool_call, check_answer, at_finish, extras, follow_ups, finish); needs `trust: engine` |

### Tools: the four annotations and strict input

Every tool states all four MCP annotations as booleans: `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`. Its `inputSchema` is of type object and says `"additionalProperties": false` on every object in it, nested ones and array items included. A tool that misses one annotation, or a schema that allows extra properties (missing, `true`, a schema, `patternProperties`), is refused at install, naming the tool and the annotation or the place in the schema.

### Settings

The `settings` schema is drawn as a form (one generator for plugins, hooks and the conductor's documents: `proxy/schemaform.py`, and `dashboard/src/lib/schema-form.ts` for the page). To keep the form and the checks honest it must:

- be of type object, and say `"additionalProperties": false` on every object;
- use only the keywords a form reads: `type`, `properties`, `required`, `items`, `enum`, `const`, `anyOf`/`oneOf` (a nullable value, or a list of constants), local `$ref` to `$defs`, `title`, `description`, `default`, `examples`, `readOnly`, `format`, `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`;
- have defaults that hold against their own schema;
- nest at most 4 levels and hold at most 200 fields;
- hold no secret: a secret is a name in `secrets`, sealed apart and never shown again.

## Capabilities

A plugin gets nothing by default. It asks in `capabilities`; the admin grants each one in the settings interface. Granted is always a part of what was asked.

| capability | means | grant |
|-|-|-|
| `prompts:meta` | a hook or provider sees ids, model, sizes, counts, timings, class and tool names | pairing's rights |
| `prompts:redacted` | as meta, plus the content after the conductor's redaction | pairing's rights |
| `prompts:full` | prompts and answers as they are | pairing's rights and a second confirm |
| `network:<host>[:<port>]` | it may connect to this host (a DNS name or an IPv4 address) | pairing's rights; a second confirm outside the LAN |
| `settings:read` | it may read the settings interface's values (never a secret) | pairing's rights |
| `engine:control` | it may restart the engine or change its mode through the controller | pairing's rights and a second confirm |
| `secrets:<NAME>` | the declared secret NAME is passed to its process | pairing's rights |

A contributed hook's `payload` needs a `prompts:` capability at that level or above; a decision provider needs one. Whatever it asks for, a plugin never reads the config database, another plugin's secrets, pairing keys or backend keys.

The plugin host checks each capability where it is used, and a refusal is journaled: in the plugin's status (`refusals`) and in the audit table (`refused`, by `plugin:<id>`, the capability's name, never a value).

| capability | checked where |
|-|-|
| `prompts:<level>` | a hook's payload when it is registered and at each call; a decision provider's input |
| `network:<host>[:<port>]` | the sandbox's egress filter: only these hosts |
| `settings:read` | `tandem/settings` and the panel bridge's `settings.read` |
| `secrets:<NAME>` | the process's environment and `tandem/secret` |
| `engine:control` | refused, whatever is granted, until engine control for plugins exists |

## Install

```
POST /v1/fleet/plugins {"source": {"type": "folder", "path": "/srv/plugins/hello-tool"}}
POST /v1/fleet/plugins {"source": {"type": "wheel", "path": "/srv/hello_tool-0.1.0-py3-none-any.whl"}}
POST /v1/fleet/plugins {"source": {"type": "git", "url": "https://example.com/hello.git",
                                   "commit": "<40 hex digits>", "subdir": "plugins/hello"}}
```

`POST /v1/fleet/plugins/inspect` with the same `source` runs every check and installs nothing.

Nothing in a plugin runs at install. A folder is copied file by file. A wheel is read as a zip, never through pip: its manifest is at its root or in one top-level folder, every member stays inside the tree, no member is a symlink, and the hashes in its RECORD must hold. A git source is fetched into an empty repository with no system or user git config, no hooks and no prompt, over https, ssh or file only, and its files are read out of the commit one blob at a time: no checkout, so no filter, attribute or LFS smudge runs.

A git source is pinned to a commit: the full id (40 hex digits). A branch name, a tag name or a short id is refused, because each can move. A submodule or an LFS file refuses the install, since its content is not in the commit, unless the manifest says `"git": {"submodules": "ignore"}` or `{"lfs": "ignore"}`: then the submodule's folder stays empty and an LFS file stays a pointer.

The checks, each a refusal with its code and the manifest field it is about (`contracts/plugin-v1/api.json` `checks` lists them all):

- the manifest parses, fits the schema and its kind's rules;
- the tools' annotations and strict input schemas;
- unique names within each contribution; hooks at real stages with a role the stage takes; providers for real questions;
- paths that stay in the plugin and exist;
- `secrets:<NAME>` names a declared secret; payloads covered by a `prompts:` capability; `engine_hooks` only with `trust: engine`;
- the settings schema, as above;
- `tandemapp` holds for this TandemApp, `hooks_api` and `plugin_api` are not newer than this controller's (same major, minor not newer). A refusal names the needed version and this one's;
- a signature, when there is one, holds.

### The tree's sha256 and a signature

The install records the sha256 of the plugin's files: sha256 over `plugin-tree-v1\n` and a line a file, in byte order of the path, `<sha256 of the file> <x or -> <path>\n` (`x`: executable). `.git` and `__pycache__` folders, `.pyc`, `.DS_Store` and `._*` files are not part of the tree; a symlink or a special file refuses it. The registry checks the tree again when the plugin is enabled and before it is started: a tree that changed reads `mismatch` (or `missing`) and does not load until it is installed again.

A plugin may carry `plugin.sig.json` at its root: `{algorithm: "ed25519", public_key, signed, signature}`, where `signed` is the tree's sha256 and `signature` is over `tandemapp-plugin-v1\0` followed by that sha256 in hex. When it is present it must hold, or the install is refused. Which keys are trusted is not decided in this version: the registry records the key's id and `trusted: null`.

## Lifecycle

| | |
|-|-|
| install | the plugin is stored under the controller's state dir, `plugins/<name>/<version>/`, disabled |
| enable | checks the tree and the compatibility with this TandemApp first |
| disable | always works |
| upgrade | install the new version with `"upgrade": true`. Settings whose property is still there and whose value still holds, secrets still declared and capabilities still asked for are kept; the rest is dropped and listed. A new capability starts ungranted. The old version's folder is removed. The same version with other files is refused (raise the version); an older version needs `"allow_downgrade": true`. |
| remove | refused while a hook names the plugin (409 `referenced`, with the hooks); takes its folder and secrets |

An install cut short leaves nothing: the files move into place before the registry row is written, and the next start removes a staging folder or a version folder no row names.

## Running plugins

The plugin host in the controller starts one process for each enabled plugin of kind `plugin`, from `entry.command` (argv, never a shell) in the manifest's folder. It reads the registry every 2 seconds and at each change: a disabled or removed plugin stops, a plugin whose settings, secrets, grants or version changed starts again with them.

### Trust levels

| `trust` | where it runs | how |
|-|-|-|
| `sandboxed` (default) | a container (Docker or Podman) or a Kubernetes pod of the chat sandbox's class | always; it waits when there is no sandbox runtime here |
| `host` | a process of the app's user, outside any sandbox | only once the admin set its `trust` key to `host` (pairing's rights and the second confirm); until then it does not start |
| `engine` | a wheel inside the engine | refused: not available yet |

**The sandbox.** The sandbox image (`QSE_SANDBOX_IMAGE`, `tandem-sandbox:1`), uid 1000, every capability dropped, no-new-privileges, a read-only root, a small in-memory /tmp, no devices, 512 MiB (`TANDEM_PLUGIN_MEM_MB`), one CPU and 128 processes, and no network but its loopback. Mounted: the plugin's installed tree at `/plugin`, read-only; its own folder at `/workspace` (`TANDEM_PLUGIN_DATA`) and a home at `/home/agent`. Nothing else of the controller's: not the config database, not another plugin's folder or secrets. Its way out is the chat sandbox's egress proxy (`HTTP_PROXY` and `HTTPS_PROXY` are set) with a host list: only the hosts of its granted `network:` capabilities, a granted LAN address included; any other address is refused, and this box (loopback), link-local addresses and the Kubernetes API are refused even when granted.

**Which runtime.** The one chatd uses on this install: `QSE_SANDBOX_BACKEND`, `QSE_SANDBOX_SOCKET` and `QSE_SANDBOX_IMAGE` from the controller's environment or chatd's env file (`<state dir>/chatd.env`). `TANDEM_PLUGIN_SANDBOX` (auto, docker, kubernetes, off) overrides it. In a cluster a plugin's pod is made like a chat's sandbox pod, its tree a read-only subPath of the release's volume, and reaches only the plugin host's egress port (`sandbox.kubernetes.pluginEgressPort`, 3129, `TANDEM_PLUGIN_EGRESS_PORT`). With no runtime a sandboxed plugin does not start: its state is `unavailable` and the health page says why (`plugin_no_sandbox`).

**The environment** holds only what the grants allow, never the controller's:

| variable | what |
|-|-|
| `TANDEM_PLUGIN`, `TANDEM_PLUGIN_VERSION` | its id and version |
| `TANDEM_PLUGIN_SETTINGS` | its own settings as JSON, each schema default filled in |
| `TANDEM_PLUGIN_DATA` | its own data folder |
| `TANDEM_PLUGIN_CAPABILITIES` | the capabilities granted, a JSON list |
| `<NAME>` | each secret granted by `secrets:<NAME>` (a name the process itself uses, such as `PATH` or `HOME`, is not passed) |
| `entry.env`, `LANG`, `PYTHONUNBUFFERED` | the manifest's own values and two defaults |

A host plugin also gets this process's `PATH`, and its data folder as `HOME`.

### Supervision

A plugin that exits, or does not answer `initialize` within 20 seconds, is started again after 1, 2, 4, 8, 16, 32, then 60 seconds. At the fifth failure within an hour the host gives up: it disables the plugin (audited, by `plugin-host`) and the health page names it (`plugin_failed`) until it is enabled again. A stop by hand holds it until a start by hand. Removing a running plugin ends its process before its folder goes.

## The protocol

MCP over stdio, one JSON-RPC 2.0 message a line, protocol revision 2025-06-18: `initialize`, then `tools/list` when the manifest declares tools, `tools/call`, `ping`. Three more methods carry what MCP has no words for, and the plugin answers those it contributes:

| method | params | answer |
|-|-|-|
| `hooks/call` | the hooks-v1 envelope (`hooks`, `id`, `stage`, `ts`, `payload`, `mode`, `request`, `event`) with `hook` set to the contributed hook's name and `hook_id` to the runner's id | a hooks-v1 result: `{action: continue\|modify\|deny\|decide, patch?, decision?, reason?, labels?}` |
| `decision/ask` | `{provider, question, instructions, options, model, deadline_ms, payload, input}`; `input` is `{chars}` at meta, `{text}` at redacted (after the conductor's redaction) and full | `{label, confidence?, probabilities?}`: `label` one of `options` |
| `backend/request` | `{backend, method, path, body}` | `{status, body}`, or `{status, events: [...]}` for a stream (each event one SSE `data:` line) |

The plugin may ask the host, with requests of its own on the same stream:

| method | params | needs |
|-|-|-|
| `tandem/settings` | none | `settings:read`: its own settings |
| `tandem/secret` | `{name}` | `secrets:<name>`; another plugin's secret never |
| `tandem/engine` | any | refused until engine control for plugins exists |

A refusal is the JSON-RPC error `-32001` with `data.capability`. The calls are local, so they carry no signature; the payload levels still apply.

## Contributions

Each contribution is registered while the plugin runs and is gone when it stops. A plugin's status says what was registered and what was not, and why (`notes`).

### Tools

Offered to the Chat tab: chatd lists them with its MCP servers (`plugin:<id>`, transport `plugin`, read-only there) as `mcp__plugin_<id>__<tool>`, and calls them through the controller (`/chat-engine/v1/plugins/tools`, `/chat-engine/v1/plugins/tools/call`), so Chat must be on to use them. Only a tool the manifest declares and the server lists is offered, with the manifest's checked schema and annotations; arguments outside the schema are refused before the plugin sees them.

### Hooks

Each hook joins the runner as `<plugin>/<hook>`: priority 100, mode on, no scope, the manifest's stages, role and payload. A hook whose payload is above the plugin's granted `prompts:` level is not registered. To set its priority, scope, mode or policy, add a hook of kind `plugin` on the hooks' API with `target` `<plugin>/<hook>` (at the declared stages, role and payload or below): it takes the contributed hook's place. The runner's timeouts, breaker and policies apply as for any hook.

### Decision providers

Each provider is registered as `<plugin>/<provider>` in `shadow`: asked off the request's path and journaled, never used. To have it answer, add a decision provider of kind `plugin` with `target` `<plugin>/<provider>` and the mode you want (`on`, `ab`): it takes the contributed one's place.

### Backends

Each backend becomes a row of the backends store, `plugin-<id>-<name>` (kind generic, the manifest's models, labelled "(plugin <id>)"), whose URL is this controller's `/plugin-backends/<id>/<name>`: requests that the gateway routes there go to the plugin as `backend/request`, `GET /v1/models` answers the manifest's models. That route answers this machine only. The row is removed when the plugin stops.

### Skills

Each skill folder of a running plugin is one more skill folder of the controller's conductor, after the packages document's own `skill_dirs`, read from the installed tree (never written).

### Panels

A panel is served at `/plugin-panels/<id>/<panel>/` (its HTML; the files beside it under the same path) with `Content-Security-Policy: sandbox allow-scripts allow-forms; default-src 'none'; ...; connect-src 'none'`: in the page's iframe or on its own it has an opaque origin, so it reads no cookie, storage or route of the controller's. It talks to the page with `postMessage({type: 'tandem-bridge', id, method, params})`; the page relays the call to `POST /v1/fleet/plugins/<id>/bridge` and posts back `{type: 'tandem-bridge-result', id, result | error}`.

| bridge method | needs |
|-|-|
| `plugin.info`, `plugin.status` | nothing |
| `tools.call` `{tool, arguments}` | the tool is the plugin's own and read-only (`readOnlyHint`) |
| `settings.read` | `settings:read` |
| `engine.control` | refused: not available yet |

`GET /v1/fleet/plugins/<id>/panels` lists them with the bridge's methods this plugin may use.

## Write a sandboxed plugin

- Use the standard library, or vendor what you need into the plugin: the sandbox has the image's Python 3.12 and Node, its root is read-only, and its network reaches only the hosts you ask for.
- Read your settings from `TANDEM_PLUGIN_SETTINGS`; keep files in `TANDEM_PLUGIN_DATA`.
- Ask for `network:<host>[:<port>]` for each host you call; HTTP clients that honour `HTTPS_PROXY` (urllib, requests, curl) go through the proxy as they are.
- Write logs to stderr; stdout is the protocol.
- `examples/hello-tool`, `audit-hook` and `classify-provider` are complete plugins; the tests run each of them.

## Debugging

| what | where |
|-|-|
| running or why not, restarts, the last error, what was registered, the refusals | `GET /v1/fleet/plugins/<id>/status` |
| its stderr and the host's lines about it, every secret value replaced by `[secret NAME]` | `GET /v1/fleet/plugins/<id>/logs?tail=200`; `<state dir>/logs/plugins/<id>.log` (1 MiB, then one older file) |
| start again, stop (held), start | `POST /v1/fleet/plugins/<id>/restart`, `/stop`, `/start` |
| a hook's calls and breaker | `GET /v1/fleet/hooks/<id>/calls` for a hook of kind plugin; the runner's metrics for `<plugin>/<hook>` |
| a plugin that gave up or has no sandbox | the health page's controller findings |

## The API

| route | rights |
|-|-|
| `GET /v1/fleet/plugins` | the configuration's |
| `POST /v1/fleet/plugins/inspect {source}` | the configuration's |
| `POST /v1/fleet/plugins {source, upgrade?, allow_downgrade?}` | pairing's |
| `GET /v1/fleet/plugins/{id}` | the configuration's |
| `POST /v1/fleet/plugins/{id}/enable` | pairing's |
| `POST /v1/fleet/plugins/{id}/disable`, `/verify` | the configuration's |
| `DELETE /v1/fleet/plugins/{id}` | pairing's |
| `GET /v1/fleet/plugins/{id}/status`, `/logs`, `/panels` | the configuration's |
| `POST /v1/fleet/plugins/{id}/start`, `/stop`, `/restart`, `/bridge` | the configuration's |

Pairing's rights are the admin's, or this machine's while no admin password is set. No answer holds a secret. Starting a plugin takes it enabled first, which takes pairing's rights.

## Settings, secrets and capabilities

They are the settings interface's `plugins` scope, one target a plugin: `GET /v1/settings/plugins/schema?target=<id>`, then plan and apply as for any scope ([settings](settings.md#plugins)). Its keys are `enabled`, `settings.<property>`, `secrets.<NAME>` (write-only), `capabilities.<capability>` and, for a manifest that asks `trust: host`, `trust`. Each plugin keeps its own versions.

## The Plugins pages

The dashboard has all of this under Settings, Plugins.

- **The list.** Each installed plugin with its version, its kind, where it came from, its trust level, its signature and its state.
- **Install.** A folder or a wheel on the controller's machine, or a git URL with a full commit id. The source is checked first. The page then shows every check with how it ended, the sha256 of the files, where the plugin asks to run, each capability in plain words with its risk, and what it adds. Install comes after that. A refused source installs nothing; each reason stands with what to change and the controller's own sentence.
- **A plugin's page.** Its state, restarts and last error, with start, stop and restart; what it adds and where each shows; its settings as a form drawn from its schema; its secrets, set or removed and never shown; each capability with its grant; its log; its files, with a check against the sha256 of the install; a newer version; and its removal. A plugin that is off has one button that stages the switch and everything it asks for.
- **A panel.** The plugin's own page in a sandboxed frame. The page relays the frame's bridge calls to `/v1/fleet/plugins/<id>/bridge`, only the methods `GET .../panels` lists for it; anything else is refused on the page. A panel that cannot be shown says why.

The switch, the settings, the secrets, the grants and the trust level are staged and go through the settings' review, which asks a second time for `prompts:full`, `engine:control`, a network host outside the LAN and the host. Install, upgrade, removal, start, stop and restart are done at once.

A wheel is named by its path on the controller's machine: the page uploads no file.

## The engine-stats plugins

The two packages in [`plugins/`](../plugins/README.md) that an engine loads to send its figures have a manifest of kind `engine-stats`. The controller can check and record them; installing them into the engine stays `pip install plugins/<engine>`.
