# Plugins

A plugin is a folder with a manifest, `plugin.json`, and a program that speaks MCP over stdio. It can add tools, hooks, decision providers, backends, page panels, skills and in-engine hooks. TandemApp installs it from a folder, a wheel or a git commit, checks it, records the sha256 of its files, and keeps its settings, secrets and granted capabilities.

The contract is [`contracts/plugin-v1`](../contracts/plugin-v1/README.md).

**In this version** a plugin is installed, checked, configured and granted capabilities, and nothing more: no plugin process is started, and a hook of kind `plugin` is still refused by the hooks' API. The plugin host that runs plugins comes next, and reads what is described here.

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

Pairing's rights are the admin's, or this machine's while no admin password is set. No answer holds a secret.

## Settings, secrets and capabilities

They are the settings interface's `plugins` scope, one target a plugin: `GET /v1/settings/plugins/schema?target=<id>`, then plan and apply as for any scope ([settings](settings.md#plugins)). Its keys are `enabled`, `settings.<property>`, `secrets.<NAME>` (write-only) and `capabilities.<capability>`. Each plugin keeps its own versions.

## The engine-stats plugins

The two packages in [`plugins/`](../plugins/README.md) that an engine loads to send its figures have a manifest of kind `engine-stats`. The controller can check and record them; installing them into the engine stays `pip install plugins/<engine>`.
