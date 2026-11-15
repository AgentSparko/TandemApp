# Settings API contract v1

These files are the truth for `/v1/settings/*`, TandemApp's one settings interface over the stores it already has (SRV-845). Five scopes answer the same six operations with the same shapes; a change set may span scopes and applies in their dependency order. The controller's tests hold every answer to these files (`tests/test_settings*.py`, with `tools/contract_check.py`). `docs/settings.md` says the same in words.

| file | what |
|-|-|
| `scopes.json` | the scopes in dependency order with their store, targets and restart classes; the operations; every route with its body, answer and rights; the key types; every error type with its HTTP status |
| `common.schema.json` | the shapes the others share: a key of a schema, a value, a change, a part's plan and result, a target, a version |
| `overview.schema.json` | `GET /v1/settings`: the scopes in order, each with its targets (an engine reached over HTTP only is a read-only target `http:<backend id>`) |
| `schema.schema.json` | `GET /v1/settings/{scope}/schema[?target=]`: each key's type, default, allowed values, restart class (`none`, `app`, `hot`, `engine`), fingerprint flag, whether it is a secret (write-only) and whether it is writable here (`variable`: the environment sets it) |
| `values.schema.json` | `GET /v1/settings/{scope}[?target=]`: each key's value and its source; a secret's value is always `null`, `set` says whether one is stored |
| `versions.schema.json` | `GET /v1/settings/{scope}/versions[?target=]`: the versions newest first, each with its id, time, author, note, change set and keys |
| `scope-change.schema.json` | the body of `POST /v1/settings/{scope}/plan` and `/apply`: one part, `{target?, set?, unset?}` |
| `restore-body.schema.json` | the body of `POST /v1/settings/{scope}/restore`: `{target?, version}`; without `confirm` the answer is the plan, with it the result |
| `change-set.schema.json` | the body of `POST /v1/settings/plan` and `/apply`: `{changes: [{scope, target?, set?, unset?} or {scope, target?, restore}], confirm?, confirm_fingerprint?, when?, deadline_s?, note?}` |
| `plan.schema.json` | a plan: each part's changes with their restart class, the fingerprint keys, warnings; one `confirm` for the set and `confirm_fingerprint` when a key feeds an engine's disk-cache fingerprint |
| `result.schema.json` | an apply or restore: each part `applied`, `scheduled` (an engine restart runs it), `unchanged`, `failed` or `not_run`, with the version it had before and the restore that brings it back; the set's `state` `applied`, `partial`, `failed` or `unchanged` |
| `restart.schema.json`, `restarts.schema.json` | `GET /v1/settings/restarts[/{id}]` and `POST /v1/settings/restarts/{id}/cancel`: an engine restart a change scheduled, its state, countdown and the engine's job |
| `audit.schema.json` | `GET /v1/settings/audit[?change_set=&limit=]`: the audit rows of the settings and of every store write a change set made |
| `error.schema.json` | every refusal: `error.type` (one of `scopes.json` `errors`), a sentence and its facts |

## Rules

- Every scope answers `schema`, `get`, `plan`, `apply`, `versions` and `restore`. A plan writes nothing.
- A change set holds at most one part a scope and target. Its parts apply in the order app, backends, conductor, engine, hooks (targets by id within a scope), and the first failure stops the set: the parts before it stay applied and say how to restore them, the parts after it are `not_run`.
- `confirm` is bound to each part's changes and to the state its scope planned against (values, versions, the engine's own plan token). Apply without it is 428 `confirm_required`; with one that no longer matches, 409 `stale_plan`. A key that feeds an engine's disk-cache fingerprint needs `confirm_fingerprint` too (428 `fingerprint_confirm_required`). The same confirm applied again with the same body answers the same result, `replayed` true. While one change set applies, another is 409 `busy`.
- A secret (a backend's API key) is write-only: no answer, version, audit row, log line or export holds its value. A restore leaves secrets as they are.
- An engine change that needs a restart applies through `tandem remote settings apply --when idle|now|at=... --deadline S` (idle and 600 s by default); its backends answer 503 with `Retry-After` until the engine is back and has given one warm-up token. Hot keys only apply through the engine's `PATCH /v1/engine/settings` without a restart. A failed start is the engine's own rollback; the restart says what it restored.
- Reads follow the dashboard's rule; plans, applies, restores and a cancel follow the configuration's (the admin from another machine) and must come from the page itself (a JSON body, not another site or origin).

## Versioning

MINOR adds a scope, a key type, a route, an error type or an optional field; MAJOR removes or renames one, or changes a meaning.
