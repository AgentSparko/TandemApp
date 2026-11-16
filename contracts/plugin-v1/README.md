# Plugin contract v1

These files are the truth for a TandemApp plugin's manifest (`plugin.json`), the checks an install makes, the capabilities a plugin can ask for, the form its settings are drawn as, and `/v1/fleet/plugins/*`, the registry's admin API (SRV-857). The controller's tests hold the code and every answer to them (`tests/test_plugin_*.py`, `tests/test_schema_form.py`; the page's generator in `dashboard/tests/schema-form.test.ts`). [docs/plugins.md](../../docs/plugins.md) says the same in words.

| file | what |
|-|-|
| `manifest.schema.json` | `plugin.json`: every field, strict (an unknown field is refused). `proxy/pluginmanifest.py` `MANIFEST_SCHEMA` is the same schema; `tools/plugin_check.py <folder>` is the validator |
| `capabilities.json` | the capabilities, what each means, and which grants take a second confirm |
| `api.json` | the routes with their bodies, answers and rights; every error type with its HTTP status; every check code an install refusal can name |
| `common.schema.json` | the shapes the answers share: a source, a signature, a check's refusal, a plugin as listed and as one |
| `plugins.schema.json`, `plugin-one.schema.json`, `install.schema.json`, `inspect.schema.json`, `verify.schema.json`, `removed.schema.json`, `install-body.schema.json`, `error.schema.json` | the answers and bodies of `api.json`'s routes |
| `status.schema.json`, `logs.schema.json`, `panels.schema.json`, `bridge-body.schema.json`, `bridge.schema.json` | the plugin host's routes (SRV-859): a plugin's status, its log, its panels with the bridge's methods, one bridge call |
| `trust.schema.json`, `trust-body.schema.json` | a plugin's trust (SEC-18): its level, grants, signature and whether it runs; a grant or revoke |
| `plugin-trust.schema.json`, `key-body.schema.json`, `key.schema.json`, `key-remove-body.schema.json`, `key-removed.schema.json`, `policy-body.schema.json`, `policy.schema.json` | `/v1/fleet/plugin-trust` (SEC-18): the trusted publisher keys, one added or removed with its fingerprint typed back, the signature policy |
| `form.schema.json` | a JSON schema as a form: the fields, their controls, and the notes where the generator fell back to the raw editor |
| `forms/` | a schema and the form it gives, each: the Python and the TypeScript generator both give exactly that form |
| `examples/` | `hello-tool` (one MCP tool), `audit-hook` (an observer hook), `classify-provider` (a decision provider with a secret and a network host). The engine-stats manifests are `plugins/vllm/plugin.json` and `plugins/sglang/plugin.json` |
| `fixtures/refusals/` | one manifest a rule, each refused with its `code` on its `field` (and its `facts`) |

## The rules

- A plugin is a folder with `plugin.json` at its root. Kind `plugin` is a program that speaks MCP over stdio, started from `entry.command` (argv, never a shell); kind `engine-stats` is a package an engine loads, recorded only.
- Nothing in a plugin runs at install or inspect: a folder is copied, a wheel read as a zip, a git commit read blob by blob from an empty repository with no hooks and no user config.
- A git source names a full commit id. A branch, a tag or a short id is refused (`commit_required`); the commit must resolve (`commit_not_found`). Submodules and LFS files are refused unless the manifest's `git` says `ignore`.
- Every MCP tool states `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` as booleans, and its input schema says `additionalProperties: false` on every object. Otherwise `annotation_missing` or `extra_properties`, naming the tool.
- The tree's sha256 (`plugin-tree-v1`, docs/plugins.md) is recorded at install and checked again on enable and load (`integrity`: ok, mismatch, missing), and so is `plugin.sig.json` (the tree's sha256 leaves it out). A tree or signature that changed is refused (`integrity`, the health finding `plugin_tampered`).
- `plugin.sig.json`, when present, must hold (`signature_invalid`): an ed25519 signature over a statement naming the plugin, its version, the tree's sha256 and plugin.json's sha256 (`tools/plugin_sign.py` writes it; docs/plugins.md gives the bytes). It is trusted only when its key is on `/v1/fleet/plugin-trust` and the key's limits (plugin ids, levels) allow the plugin at its level; a key is added and removed with its fingerprint typed back.
- Capabilities are deny by default. A grant takes pairing's rights; `prompts:full`, `engine:control` and `network:` outside the LAN also take the settings interface's second confirm (`confirm_grant`). Granted is always within requested.
- A plugin's settings, secrets and grants are the settings interface's `plugins` scope (settings-v1 1.1), one target a plugin. Secrets are write-only.
- Removal is refused while a hook names the plugin (kind `plugin` or `mcp`, target `<id>` or `<id>/<hook>`): 409 `referenced`.
- The plugin host (SRV-859) runs an enabled plugin as one process speaking MCP over stdio plus `hooks/call`, `decision/ask` and `backend/request`, at its trust level (`capabilities.json` `trust`, SEC-18): `sandboxed` by default (unsigned, unless the policy `require_signature_sandboxed` is on), `host` only once granted (a trusted signature, or the plugin's id typed), `engine` granted per engine host on a trusted signature and the typed confirmation, and refused at runtime until the in-process chain exists. An upgrade that asks more trust waits for `confirm_trust` (409 `trust_confirm_needed`), the installed version running meanwhile. Every trust decision is in the audit table. Its contributions are registered while it runs and gone when it stops; each capability is checked at its use, and a refusal is journaled. docs/plugins.md describes the protocol.

## Versioning and compatibility

Three versions meet when a plugin is installed, each checked and each named in a refusal:

| version | where | compatible when |
|-|-|-|
| `plugin_api` | the manifest; this contract's `1.0` | the same major, a minor not newer than the controller's (`incompatible_plugin_api`) |
| `hooks_api` | the manifest; hooks-v1's version | the same major, a minor not newer than the controller's (`incompatible_hooks_api`) |
| `tandemapp` | the manifest, a range: `>=`, `>`, `<=`, `<`, `==`, `!=`, `~=` and `==X.Y.*`, comma-separated | the running TandemApp's version is in it (`incompatible_tandemapp`). A pre-release sorts before its release, and `<X.Y` leaves out X.Y's pre-releases |

The same checks run again when a plugin is enabled and before it is started, so a TandemApp that moved past a plugin's range stops it from loading (409 with the needed version).

Signing and trust (SEC-18) are part of 1.0: they landed before any release of this contract, so F1's first signature format (`signed`, no `format`) was dropped rather than kept beside it; such a file is refused with what to do.

This contract: MINOR adds a manifest field (optional), a contribution kind, a capability, a check, a route, an error type or an optional answer field; a plugin written for 1.0 keeps installing. MAJOR removes or renames one, makes an optional field required, or changes a meaning. The answers carry `contract_version`.
