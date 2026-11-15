# Hooks contract v1

A remote hook is code outside TandemApp that the controller calls at a stage of a request: before routing, before the engine, on the answer, on an error, after the ledger row. These files are the contract between the controller's hook runner (`proxy/hooks.py`) and a hook's author; TandemApp owns them. Its version is `1.0`, independent of the release. docs/hooks.md explains the concepts and how to write a webhook.

| file | what |
|-|-|
| `stages.json` | every stage: when it runs, what a hook may do there (`may`, `actions`, `roles`), its budget and default failure policy, the fields a patch may touch (`fields`; `content` needs payload full), the decision keys, whether it counts toward the 800 ms cap, and whether the controller calls it in this version (`wired`). `proxy/hookstages.py` is the same table |
| `call.schema.json` | the body of every call: `{hooks, id, hook, stage, ts, payload, mode, request, event}` |
| `events/<stage>.schema.json` | `event` per stage; `content` appears only at payload `redacted` or `full` |
| `result.schema.json` | what a hook answers: `{action: continue\|modify\|deny\|decide, patch, decision, reason, labels}` |
| `examples/<stage>.<payload>.json` | one call per stage as a `meta` hook gets it, and as a `full` one where the stage has content |
| `fake_server.py` | a receiver for tests and trying hooks by hand: it checks every call's signature, timestamp and replay as a receiver must |
| `hooks.schema.json`, `hook.schema.json`, `hook-one.schema.json`, `hook-new.schema.json`, `hook-patch.schema.json`, `hook-created.schema.json`, `hook-test.schema.json`, `hook-calls.schema.json`, `error.schema.json` | the controller's admin API, `/v1/fleet/hooks/*` (below) |

`tests/test_hooks_contract.py` holds `stages.json` to `proxy/hookstages.py`, every example and every call the runner makes to these schemas, and the admin API's answers to theirs.

## The rules

- **Order.** The hooks in scope at a stage run by integer `priority` (low first), ties broken by `id`.
  - Transforms chain: each sees the value the ones before it made.
  - Guards run after the transforms, on the transformed value. The first `deny` wins.
  - Deciders run after the guards. The first `decide` wins.
  - Observers see the final value, off the request's path.
- **Roles.** A transform answers `continue` or `modify`, a guard `continue` or `deny`, a decider `continue` or `decide`, an observer `continue`. Anything else is refused as `invalid` and counts as a failure.
- **Patches.** A `patch` sets top-level fields of the stage's value (`null` removes one). Only the stage's `fields`; a `content` field (messages, prompt, input, tools, the answer's choices, tool calls) only from a hook with payload `full`.
- **Budgets.** A stage's `budget_ms` is shared by its sync hooks per request; a hook's `timeout_ms` is at most that. Sync hooks before the first token share 800 ms per request (`cap_ms`): a hook with no time left is not called (outcome `cap`) and its failure policy applies.
- **Failure policy.** `open`: a failed hook is as if absent. `closed`: the request is refused with 503 `hook_unavailable`. Guards default to `closed`, every other hook to its stage's `on_failure`. Async stages drop failures.
- **Retries.** None on the request's path. An observer is called once more after a failure.
- **Circuit breaker**, per hook: five failures in a row, or a p95 at or above the hook's timeout across at least 20 calls within 60 s, opens it for 30 s. While open the hook is skipped (outcome `breaker_open`) and its policy applies. Then one probe: a success closes it, a failure opens it again.
- **Modes.** `off`: never called. `shadow`: called off the request's path on a copy of the value it would have seen; its result and diff are journaled, nothing is applied. `on`: as above.
- **Scope** per hook: `models` (globs over the asked id and the backend's), `backends`, `profiles`, `users`, `sessions`, `client_kinds`. An absent or empty list matches everything.
- **Streams.** `stream.chunk` observes each event. On a streamed answer `tool_call.detected` and `response.after_engine` run after the stream ended, as observers: a `modify` or `deny` there is journaled as `refused` and nothing is applied.
- **One retry.** `response.after_engine` may `decide` `{retry: true}` once per request: the same request is sent again and the new answer passes the stage again.
- **No hook, no change.** With no hook enabled the controller relays every request byte for byte as without the runner; a hook that answers `continue` changes no byte either.

## Kinds

`http` (a signed webhook) and `builtin` (code in the controller: `set`, `deny`, `route`, `fallback`, `noop`) run now. `plugin`, `mcp` and `inproc` are part of the contract and refused with `kind_unavailable` until the plugin host and the in-process chain exist.

## Payload levels

| level | `event` holds |
|-|-|
| `meta` (default) | ids, the model, sizes, counts, timings, the scalar parameters, tool names. Never a prompt's or an answer's text |
| `redacted` | meta plus `content`, every string through the conductor's secret redaction first. If the redaction fails the hook gets no call and its policy applies |
| `full` | meta plus `content` as it is. Granted per hook (who and when are kept). A destination outside the LAN needs a second confirm, and a new endpoint asks again. A grant for the LAN checks the address connected to on every call |

## Signing

Every call carries:

| header | value |
|-|-|
| `X-Tandem-Hook` | the hook's id |
| `X-Tandem-Delivery` | the call's id, also the body's `id` |
| `X-Tandem-Timestamp` | unix seconds |
| `X-Tandem-Signature` | `v1=` and the hex HMAC-SHA256 of `<timestamp>.` followed by the body's bytes, keyed with the hook's secret (its UTF-8 bytes). During a rotation's grace a second `v1=` for the previous secret follows, comma-separated |

A receiver recomputes the HMAC over the bytes it read, compares in constant time, refuses a timestamp more than 300 s from its clock, and refuses a body `id` it has seen within that window. The secret is answered once, when the hook is made or its secret rotated; it is stored encrypted under the controller's master key and is never logged, journaled, exported or answered again.

## The admin API

Under the controller's admin rights (the configuration's writes); granting `full`, moving a full hook's endpoint and rotating a secret take pairing's rights.

| route | answer |
|-|-|
| `GET /v1/fleet/hooks` | `hooks.schema.json` |
| `GET /v1/fleet/hooks/stages` | `stages.json` |
| `POST /v1/fleet/hooks` (`hook-new.schema.json`) | 201 `hook-created.schema.json` |
| `GET`, `PATCH` (`hook-patch.schema.json`) `/v1/fleet/hooks/{id}` | `hook-one.schema.json` |
| `DELETE /v1/fleet/hooks/{id}` | `{deleted}` |
| `POST /v1/fleet/hooks/{id}/enable`, `/disable`; `PUT .../mode {mode}` | `hook-one.schema.json` |
| `POST /v1/fleet/hooks/{id}/test {stage?, event?}` | `hook-test.schema.json` |
| `GET /v1/fleet/hooks/{id}/calls?limit=N` | `hook-calls.schema.json` |
| `POST /v1/fleet/hooks/{id}/secret {grace_s?}` | `hook-created.schema.json` |

Errors are `error.schema.json`. A grant of `full` outside the LAN answers 409 `confirm_needed` with `confirm`; the same request with that `confirm` applies it.

## The version

MINOR adds a stage, an action, an event field, a decision key, a kind that runs, or a route; a hook written for 1.0 keeps working. MAJOR removes or renames one, or changes a meaning. `stages.json` `wired` says which stages the controller calls in this version: the others are called by the work that owns them (decision providers, chatd's tool and session stages, the memory writer, engine control) in a minor that keeps their event shape.
