# Settings

TandemApp keeps its settings where they always were: the page's switches and the backends in the controller's config database, the conductor's documents in their folder, each engine's settings on its own host. `/v1/settings` is one interface over all of them. Every part of it answers the same six questions, a change can span several parts, and every change is versioned, can be undone and is audited.

The contract is [`contracts/settings-v1`](../contracts/settings-v1/README.md).

## Scopes

| scope | target | store | restart class |
|-|-|-|-|
| `app` | `controller` | the page's switches: `chat` and `conductor`, `on` or `off` | none |
| `backends` | `controller` | the config database: `backends.<id>.<field>`, `backends.<id>.key`, `routes`, `users.<id>.enabled` | none |
| `conductor` | `controller` | this controller's conductor documents: `settings`, `router`, `roles`, `packages`, `monitor`, `residency`, `deciders`, `profiles.<name>` | hot |
| `engine` | an engine host's id | the engine's managed overlay, through `tandem remote settings` over SSH | per key: hot or engine |
| `hooks` | `controller` | the hook registry and the decision providers, through their adapter: `hooks.<id>.<field>`, `providers.<id>.<field>` | none; an in-process hook needs an engine restart |
| `plugins` | an installed plugin's id | the plugin registry: `enabled`, `settings.<property>`, `secrets.<NAME>`, `capabilities.<capability>` ([plugins](plugins.md#settings-secrets-and-capabilities)) | none |

They apply in this order. A change to the backends lands before a conductor document that may name one, and both land before an engine restart. Hooks apply before plugins, so a change set that turns off a plugin and the hooks that name it lets go of the hooks first.

The restart classes:

- `none`: the change applies as it is.
- `app`: this controller restarts.
- `hot`: the running service reads the change at once.
- `engine`: the engine restarts. This is scheduled and waits for the engine to be idle.

An engine reached over HTTP only, a TandemLLM backend whose host is not paired over SSH, is a read-only target, `http:<backend id>`. Its hot keys are read from the engine's `GET /v1/engine/settings`. To change it, use its own host (`tandem remote settings`), or pair it.

## The six operations

Every scope answers:

```
GET  /v1/settings/{scope}/schema[?target=]      each key: type, default, restart class, fingerprint flag, secret, writable
GET  /v1/settings/{scope}[?target=]             each key's value and where it comes from
POST /v1/settings/{scope}/plan     {target?, set?, unset?}
POST /v1/settings/{scope}/apply    {target?, set?, unset?, confirm, confirm_fingerprint?, confirm_grant?, when?, deadline_s?, note?}
GET  /v1/settings/{scope}/versions[?target=]
POST /v1/settings/{scope}/restore  {target?, version, confirm?, ...}     without confirm: the plan
```

`GET /v1/settings` lists the scopes in order with their targets.

- **Sources.** In `app`, a source is `install` while the env file decides (`TANDEM_CHAT`, `TANDEM_CONDUCTOR`) and `page` once switched. Unsetting a key hands it back to the install. In `backends`, a field the environment sets is `env` and not writable here. Its `variable` names the variable to change instead. Engine keys carry the engine's own layer names (`default`, `install`, `profile`, `setup`, `local`, `managed`, `mode`).
- **Versions.** The conductor and the engine have versions of their own: `<document>@<n>` and the overlay's `n`. For `app`, `backends` and `hooks`, the controller keeps the values after each change in its config database. Secrets are left out. A change made on another screen is recorded as a version of its own ("as found") before the next change.
- **Restore** writes a new version equal to the old one. It never deletes history. A key that no longer exists, or that the environment now sets, is left alone and the plan says so. A conductor document or an engine overlay that is no longer valid is refused with `restore_invalid`.

## Change sets

```
POST /v1/settings/plan  {changes: [{scope, target?, set?, unset?}, {scope, target?, restore: "<version>"}, ...]}
POST /v1/settings/apply {changes, confirm, confirm_fingerprint?, confirm_grant?, when?, deadline_s?, note?}
```

A change set has at most one part per scope and target.

Unsetting a key that is unset already is no change, in every scope: the plan drops it, and a part left with nothing applies as `unchanged`. In `backends`, a field that holds nothing is unset (`set` false).

**The plan** writes nothing. For each part it lists:

- every change, with its restart class;
- for a part that restarts an engine, how long the restart itself takes (`restart_estimate`, below);
- the keys that feed an engine's disk-cache fingerprint;
- warnings.

`restart_estimate` is `{seconds, source}`. The source is one of these, in this order:

| source | seconds |
|-|-|
| `measured` | what this controller saw the last time it restarted that engine: from the engine's job acting to the end of the warm-up. Kept while the controller runs. |
| `engine` | the engine's last start as its `status` reports it: its last start, restart or settings job that acted at once, from its creation to its end |
| `default` | 180 |

It leaves out the wait for idle, which `deadline_s` bounds.

It gives one `confirm` for the whole set. A key that feeds the fingerprint adds `confirm_fingerprint`: after that restart every saved prefix and parked chat misses, so it asks twice.

**A refusal** while a part is planned names the part: its `scope` and `target`, and the `key` when it is about one key's value (`unknown_key`, `locked`, `bad_value`, `invalid_document`, `extension_missing`). The same key on two engine hosts is told apart. A part of one key names that key even when its store's sentence does not.

**Applying** plans again and compares:

| situation | answer |
|-|-|
| no `confirm` | 428 `confirm_required` |
| the settings changed since the plan | 409 `stale_plan`, with the new plan |
| a fingerprint key without its second confirm | 428 `fingerprint_confirm_required` |
| another change set is applying | 409 `busy` |

The parts then apply in order. **The first failure stops the set.** The answer lists each part:

| state | meaning |
|-|-|
| `applied` | written |
| `scheduled` | an engine restart will run it (see below) |
| `unchanged` | it held already |
| `failed` | refused or failed; `error` says why |
| `not_run` | after the failure, untouched |

Each applied part carries the version it had before and the `restore` that brings it back. The set as a whole is `applied`, `partial`, `failed` or `unchanged`.

**Applying again.** The same confirm with the same body, sent again within five minutes, answers the same result (`replayed` true). Nothing is applied twice. After a partial failure, plan the same changes again: the parts that already hold are unchanged, and the rest is planned anew.

## Secrets

A backend's API key, and any key a hook adapter marks secret, is write-only:

- an answer shows `set` true or false, never the value;
- a plan shows the change as set or unset;
- the confirm binds the value through its hash only.

No answer, version, audit row, log line or export holds a secret. A restore leaves secrets as they are.

The engine's keys that may hold a secret are not managed by its overlay. Their values are not read into an answer either.

## Engine changes and restarts

**Hot changes.** A change of hot keys only (today `QSE_ALLOW_FROM`) goes through the engine's `PATCH /v1/engine/settings` (engine API 1.9). The plan's overlay version is the `base`. There is no restart.

**Other changes** go through `tandem remote settings apply`. `when` sets when the engine acts, `deadline_s` how long it waits:

- `when: idle` (the default) waits for idle, up to `deadline_s` (600 s by default);
- `when: now` acts at once;
- `when: at=<ISO time>` waits until that time, then for idle.

The engine's own job stops the engine, makes the new overlay version current and starts it. If the start fails, the engine puts the last good version back.

**The restart.** The controller follows the engine's job (`GET /v1/settings/restarts/{id}`, in the part's `restart`):

| state | what happens |
|-|-|
| `scheduled` | waiting for an `at=` time. Nothing is drained yet. |
| `draining` | the gateway answers 503 `node_draining` with `Retry-After: 30` for the backends on that engine host. Other backends keep serving. The engine's job waits for idle. `remaining_s` counts down to the deadline. |
| `restarting` | the job acts: stop, new version, start. |
| `warming` | the job is done. One token goes through each backend on the host, then the backends serve again. |
| `done`, `failed`, `timeout`, `cancelled` | the end. |

- `failed`: the engine's message, and `restored`, the version it runs again and whether it answers.
- `timeout`: the deadline passed while the engine was busy. It changed nothing.
- `cancelled`: see below.

**Cancel.** `POST /v1/settings/restarts/{id}/cancel` works while the restart is scheduled or draining. The controller sends `start` to the engine host. That replaces a job that still waits, and finds the engine healthy, so nothing changes. Once the job acts it is too late (409 `too_late`).

The drains live in the controller's process. A controller restart ends them; the engine's job goes on by itself.

## The engine's mode

Two keys of the engine scope stand for the engine's mode:

| key | values | what it is |
|-|-|-|
| `mode` | `engine` or `chat` | `engine`: the bare engine. `chat`: TandemApp's conductor and Chat run inside it. |
| `mode.ext` | a module, `tandemapp.engine_ext` by default | the extension chat mode runs |

They show what the engine's `QSE_MODE` and `QSE_EXT` resolve to. Those two are not changed directly: a part that names them is refused (422 `locked`).

- **The mode alone.** A part that only sets `mode` and `mode.ext` goes through the engine's own `tandem remote mode set`. It plans, applies and restarts like any other engine change: the change set's id, the audit rows, `when` and `deadline_s`, the drain, the countdown, Cancel and the warm-up token. The restart's `kind` is `mode`.
- **With other keys.** Together with other keys of the same engine, the mode goes into the same overlay change as `QSE_MODE` and `QSE_EXT`, which is what `mode set` writes. The engine restarts once.
- **The extension.** When the result is chat mode, the plan asks the engine host for its extensions (`tandem remote ext list`). If none of that module loads there, the plan says so in a warning and names the wheel TandemApp installs first: its own, for its own version (`TANDEM_NODE_WHEEL`, else built from this controller's copy, as Add Spark sends it). The apply sends it (`tandem remote ext install`, its sha256 checked) before the switch. The install itself restarts nothing.
- **No wheel.** A module that is not TandemApp's, or no wheel on this controller, makes the plan refuse with 409 `extension_missing` on the key `mode.ext`. The sentence says what to do: set `TANDEM_NODE_WHEEL` to the tandemapp wheel of this release, or install the extension on the engine host.

`POST /v1/fleet/engines/{id}/mode {mode?, when?, ext?}`, the route the Add engine wizard uses, is a change set of one part with these keys. It answers as before: whether anything changed, the mode before and the engine's job. Its audit row names the change set.

## Audit

`GET /v1/settings/audit[?change_set=cs-...]` lists the settings' rows of the config database's audit table, which `/v1/config/audit` shows too:

- one row per part applied or restored (`settings.apply`, `settings.restore`) with its result;
- one row per change set (`settings.change_set`) with what applied, what failed and what did not run;
- one row per restart's end (`settings.restart`) and per cancel (`settings.cancel`).

Each row has the change set's id, the scope and target, the keys (never a value), who, when and the result. Every row a store writes during a change set carries the same id, for example a backend's update or a switch of the page.

## Rights

| request | rule |
|-|-|
| reads | the dashboard's |
| plans, applies, restores, a cancel | the configuration's: the admin from another machine, or a tool on this machine |

Every write must come from the page itself: a JSON body, not from another site or origin.

## Hooks

The hooks scope is an adapter (`settingscopes.HooksAdapter`). The controller plugs in the hook registry's (`proxy/hooksettings.py`). Without one, the scope answers an empty schema.

Each hook has these keys:

| key | what |
|-|-|
| `hooks.<id>.enabled` | whether it is called at all |
| `hooks.<id>.mode` | `off`, `shadow` or `on` |
| `hooks.<id>.priority` | its order among the hooks of a stage, low first |
| `hooks.<id>.scope` | the requests it runs on: lists of patterns under `models`, `backends`, `profiles`, `users`, `sessions`, `client_kinds`. Unset: every request. |
| `hooks.<id>.policy` | `open` or `closed` when it fails. Unset: the stage's default. |
| `hooks.<id>.timeout_ms` | its own time limit. Unset: the stage's budget. |
| `hooks.<id>.payload` | `meta`, `redacted` or `full` |
| `hooks.<id>.secret` | an http hook's signing secret, shown only as set or unset |

Each value carries the hook's name as its `label`. The checks are the registry's own. A change is written through the registry and audited there, and the hook runner reads it at the next request. The settings interface keeps a version after each change.

Adding or removing a hook, where and when it runs, and a new secret stay on the hooks' API (`/v1/fleet/hooks`, [hooks](hooks.md)). That API makes the secret and shows it once, so the settings interface never writes one.

### The full payload

Setting `payload` to `full` grants the hook the prompts and answers as they are. It keeps the rules of the hooks' API:

- Only a caller with pairing's rights may plan it: the admin, or this machine while no admin password is set. Anyone else gets 403 `trust_required`.
- The plan warns with the destination's host.
- A destination outside the LAN needs a second confirm. The plan lists the key under `grant_keys` and gives `confirm_grant`. Apply without it is 428 `grant_confirm_required`. The grant records who confirmed it.

### Decision providers

The decision providers are keys of the same scope. They sit beside the hooks because a `hook` provider is a decision.ask hook. Each provider has these keys:

| key | what |
|-|-|
| `providers.<id>.enabled` | whether it is asked at all |
| `providers.<id>.mode` | `on`, `shadow` or `ab` |
| `providers.<id>.ab_percent` | in `ab`, the share it answers, 0 to 100 |
| `providers.<id>.sticky` | in `ab`, what the share sticks to: `session`, `user` or `request` |
| `providers.<id>.questions` | the questions it answers, each `{model?, endpoint?, timeout_ms?}` |
| `providers.<id>.api_key` | a `systemone-http` provider's own key |
| `providers.<id>.questions.<question>.api_key` | the key for one question alone |

A key is write-only. A string sets it and an unset clears it. An answer says only whether one is set, with its hint, and a version never holds it. A question's key exists once the provider answers that question: add the question first, then its key.

One enabled provider serves a question `on` or `ab`. A change set may move a question from one provider to another. The provider that lets it go is written first.

Adding or removing a provider and its kind stay on its API (`/v1/fleet/decision-providers`, [decision providers](decision-providers.md)).

## Plugins

The plugins scope (settings-v1 1.1) has one target a plugin. Its keys come from the plugin's manifest, so they differ plugin by plugin; each plugin keeps its own versions, and restoring one never touches another or the hooks. Installing, upgrading and removing a plugin stay on its API (`/v1/fleet/plugins`, [plugins](plugins.md)).

| key | what |
|-|-|
| `enabled` | the plugin host may start it. The tree is checked against its recorded sha256 first. |
| `settings.<property>` | one property of the plugin's settings schema, with its type and default. A value must hold against the property's schema. Unset: the default. |
| `secrets.<NAME>` | a declared secret, write-only, sealed under the master key |
| `capabilities.<capability>` | a requested capability, `false` until granted. Unset revokes it. |

Enabling a plugin and every grant take pairing's rights (403 `trust_required` otherwise). A grant of `prompts:full`, `engine:control` or `network:<host>` outside the LAN is in the plan's `grant_keys` and needs `confirm_grant`. A restore that brings a grant back asks again.

## The pages

The dashboard shows this interface under Settings, App: one page per scope.

| page | what it holds |
|-|-|
| App | the `app` scope: the Chat and conductor switches under What runs here, above the controller's own page |
| Backends | the `backends` scope: a group for each backend, the routing rules, the users' keys |
| Conductor | the `conductor` scope: each document, edited whole |
| Engines | the `engine` scope: a page for each engine host, its mode first (the two choice cards, and the extension once chat is chosen). An HTTP-only one is read-only and says why. |
| Hooks | the `hooks` scope: a group for each hook and each decision provider, named after it. "No hooks yet" while there is none. |
| History | the audit rows and the restarts that ended. `#/app/history?change_set=cs-...` shows one change set. |

Each page draws its form from the scope's schema. A key gets the control its type asks for: a switch for `on` and `off`, a number field for an integer, a JSON editor for a document. The row also shows the default and the value's source. The environment locks some keys; such a row names the variable, for example `TANDEM_BACKENDS`. No page shows a secret. You set one or remove it.

Changing a field writes nothing. The page stages the change, and a bar at the bottom carries the staged changes from page to page. Review then reads the plan of the whole set from `POST /v1/settings/plan`:

- every part in the order it applies, with its changes and warnings;
- whether a part restarts an engine, and how long the restart takes ("about 3 min"). The plan then asks when: once idle (it waits 10 minutes by default), at a set time, or now;
- a second confirm for a key that feeds the fingerprint, and for a hook's full payload outside the LAN. That one names the hooks and where their payload goes.

A plan the controller refuses for one field marks that field, on the page of the part the refusal names, with the sentence under it.

Apply answers with each part's state. An applied part has a Restore button. After a failure you can plan the same changes again.

While an engine restarts, every page of the area shows it: the state, the countdown to the deadline, the engine's job, and Cancel while `can_cancel` holds. After a failed start the card names the version the engine runs again.

Versions sit at the bottom of each page, with what differs from now. A restore goes through the same plan as any other change.
