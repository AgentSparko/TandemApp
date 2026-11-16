# Decision providers

The conductor makes small decisions all the time: which kind of work a message is, which package fits it, whether the user asked to remember something, whether a tool's output shows a failure. Each is a typed question with a fixed set of answers, and a small decision model answers it in tens of milliseconds. A **decision provider** is who answers. The built-in one is the conductor's own decider; you can put your own model in its place, per question, behind an API of yours.

## What a decision is

A question has an id, its wording, its options (`{id: what it means}`) and the text it is about (a digest of the user's message, the session, or a tool's output). The answer is one option id with a probability. The conductor asks its stages in order (the **cascade**): the decider stage first, then the served model's own stage where the router document has it. A stage's answer is taken when its probability reaches the stage's threshold (0.8 for the decider by default); otherwise the next stage is asked, and with none left the turn falls back to the router's default.

A provider answers in the **decider stage's place**. Which stages a question asks is the router document's choice: to have a provider answer the class question, the decider stage must be enabled there.

| question id | asked by | answers |
|-|-|-|
| `classify` | the router, on a new user message | the router's classes |
| `packages` | the router and the library | the package names, and `none` |
| `deliverable` | the router | what the turn is to deliver |
| `memory_intent` | the memory notes | `yes` / `no`: does the message ask to remember, forget or recall |
| `memory_gate` | the memory writer | `yes` / `no`: anything worth a note in the session |
| `judge` | the tester's questions after a turn | `failure` / `clean` / `unclear`, `ok` / `failed` / `incomplete` |

## Where the questions are asked

Two conductors exist, and a provider serves one of them:

- **The controller's conductor** (TandemApp's own, switched on from the page) runs around requests relayed to backends: the harness asks it before a request goes out, its judge after the turn, its memory writer in the idle time. Every question above is asked in the controller's process, and **a provider can answer all of them today**. Its decider role names `self` by default, which on a controller answers nothing: without a provider the decider stage there says `decider_no_endpoint` and the served model's stage decides.
- **The engine's conductor** (inside a TandemLLM engine, through the engine extension) asks the engine's own decision models in process, in the engine's idle window. Its questions decide what goes into the prompt, so they touch the engine's tokens. **Providers do not serve it in this version.** Routing them needs one of: the extension asking the controller (`POST` a question to the controller, one LAN hop of a few milliseconds, inside the idle window, with the controller's answer or the built-in on any failure), or the engine reaching the provider itself (the provider set pushed to the engine with its keys sealed for that host). Either changes the engine's decisions and needs the GPU check (G3) in a stop window first.

## Kinds

| kind | what answers | configured with |
|-|-|-|
| `builtin` | the conductor's own decider (its `decider` role's endpoint; `tester` for the judge) at a model per question | models per question |
| `systemone-http` | any service that speaks the systemone wire below | endpoint, API key, model and timeout, each also per question |
| `hook` | a `decision.ask` hook of [hooks](hooks.md): an adapter you write for any other API shape | the hook's id |
| `plugin` | a running plugin's provider ([plugins](plugins.md#decision-providers)): `decision/ask` over its stdio | `target`: `<plugin>/<provider>` |

A running plugin's providers are also there on their own, in `shadow`, as `<plugin>/<provider>`: asked off the request's path and journaled, never used. A provider of kind `plugin` naming the same target takes its place, with your mode.

## The systemone wire

Three calls, the shapes pinned in [`contracts/engine-api-v1/decision/systemone.schema.json`](../contracts/engine-api-v1/decision/systemone.schema.json). With an API key every call carries `Authorization: Bearer <key>`.

`POST {endpoint}/v1/systemone`:

```json
{"state": "Rename the variable cnt to count in utils.py and run the tests.",
 "model": "auto",
 "questions": {"q": {"type": "choice",
                     "instructions": "What kind of work does this request ask for?",
                     "criteria": {"code_edit": "change code in a project",
                                  "chat": "a question or a conversation"}}}}
```

answers

```json
{"contract_version": "1.0", "model": "my-decider",
 "answers": {"q": {"type": "choice", "choice": "code_edit",
                   "probabilities": {"code_edit": 0.93, "chat": 0.07}, "confidence": 0.93}},
 "usage": {"input_tokens": 16, "output_tokens": 0},
 "routing": {"model": "my-decider", "reason": "default"},
 "path": "single", "latency_ms": 21.4}
```

`GET {endpoint}/readyz` answers `{"status": "ready", ...}` when it can serve. `GET {endpoint}/v1/models` lists the models with their `limits` (`context_tokens`, `question_tokens`, `max_options`): the conductor sizes the text it sends to what the model reads, at 3.5 characters a token (512 and 192 tokens when not given). `model` is the one the provider maps for the question, `auto` by default.

[`tools/systemone_example.py`](../tools/systemone_example.py) is a whole provider in the standard library: run it, point a provider at it, then replace its `decide()` with your model.

```sh
python tools/systemone_example.py --port 8720 --key "$MY_KEY"
```

## Setting one up

The API is `/v1/fleet/decision-providers` (the configuration's rights, as backends and hooks). A provider that answers the class question with its own endpoint and key, and the memory gate at the default endpoint:

```sh
curl -X POST http://127.0.0.1:8080/v1/fleet/decision-providers \
  -H 'Content-Type: application/json' -d '{
  "name": "My decider", "kind": "systemone-http",
  "endpoint": "http://decider.example:8720", "api_key": "<key>",
  "timeout_ms": 400, "mode": "shadow",
  "questions": {
    "classify": {"endpoint": "http://classes.example:8720", "api_key": "<another key>",
                 "model": "classes-v2"},
    "memory_gate": {}}}'
```

- `questions` maps each question it answers; a question may set its own `endpoint`, `api_key`, `model` and `timeout_ms`, and takes the provider's for the rest.
- A key is write-only: a string sets it, `null` clears it, and every answer shows only whether it is set, its last 4 characters and a sha256 prefix. Keys are stored sealed under the master key, never logged, journaled or exported; a controller move leaves them behind (type them again on the new controller).
- The provider's own key goes only to its own endpoint's origin: a question with an endpoint on another host gets its own key or none.
- One enabled provider may answer a question `on` or `ab`; any number may watch it in `shadow`.
- `POST .../{id}/test` asks it once with a sample question; `GET .../{id}/health` probes it; `GET .../report` has the numbers below. The list (`GET /v1/fleet/decision-providers`) carries each provider's health from its last probes and probes nothing: an endpoint not probed since the provider last changed shows no probe, and the status `unknown`.
- A refusal is `{error: {type, code, message, field?}}` (contracts/conductor-v1 `error.schema.json`); `field` names the value it is about.

A provider's switches and keys are also keys of the settings interface's `hooks` scope (`providers.<id>.<field>`, [settings](settings.md#decision-providers)). There a change is planned, versioned and can be restored with the other settings. Its keys stay write-only there; a change set that adds a question may set that question's key (`providers.<id>.questions.<question>.api_key`) in the same change.

## The Decision providers pages

Settings, Hooks, Decision providers shows who answers each question and who watches in shadow, each provider with its health and mode, and the report with its arms side by side: on, shadow and both sides of an A/B. A provider's page has each endpoint's probe and breaker, the mapping per question with the last four characters of its key, a test call, the fallback reasons counted from its recent calls, and those calls. Add a provider offers the three kinds, and a new one starts in shadow.

The mode, the A/B share, the sticky key, the questions and the keys are staged and go through the settings' review. The name, endpoint, model and time limit are written at once.

## Modes

- **`on`**: the provider answers its questions. On any failure the built-in answer is used.
- **`shadow`**: the conductor decides as without the provider. The same question goes to the provider beside it, off the request's path, and the journal keeps its answer, its time and whether it agrees with the conductor's decision. Nothing it says is used. Start here.
- **`ab`**: a share of sessions (`ab_percent`, 0 to 100) get the provider as in `on`, the rest the built-in. The split is sticky: by `session` (default), `user` or `request`, the next one taken when the first is missing, and a provider's id seeds it, so two providers split the sessions differently. The router's class cache keeps each arm's answers apart.

## Health and fallbacks

A provider is not used for a call, and the built-in answer is, when:

| reason | what happened |
|-|-|
| `timeout` | no answer within its timeout (at most the stage's, 400 ms for the router's questions) |
| `unreachable`, `http_<code>` | the endpoint did not answer, or answered an error (`http_401`: the key) |
| `unknown_label` | the answer is not one of the options |
| `breaker_open` | five failures in a row, or a p95 at its timeout over the last minute: skipped for 30 s, then one probe call |
| `provider_down` | its last health probe (`/readyz`, kept 10 s) failed |
| `hook_off`, `no_answer`, `out_of_scope` | a hook provider's hook is off or in shadow, answered no decision, or its scope does not take the request |
| `replay_miss` | replaying recorded answers, and none recorded for this question |
| `not_granted` | the endpoint's host is not in the provider's grant, or is outside the LAN without its confirm (below) |

## Where the text goes

A `systemone-http` provider is sent the question's text, a digest of what people wrote, so it takes the rules of a hook at the full payload ([hooks](hooks.md)). Making one, or changing where it or one of its questions answers, is a grant: it takes pairing's rights (403 `trust_required` without them; with no admin password set, only the controller's own machine). The grant (`grant` in the provider's view) names each destination host and whether it is on the LAN, who granted it and when. A host outside the LAN asks a second time: the write answers 409 `confirm_needed` with a `confirm`, and the same write with it grants it and records who confirmed. A call to a host the grant does not hold is not made (`not_granted`). In the settings interface a change of `providers.<id>.questions` that adds such a host is planned the same way (`grant_keys`, `confirm_grant`).

The stage's row in the conductor's journal names the provider (`provider: {id, mode, arm?, fallback?}`) whenever one answered or fell back; without a provider the row is as it was. The health view (`GET .../{id}/health`) gives each endpoint's probe, its breaker and its questions, and a status: `ok`, `failing` (a breaker not closed, or the last call within a minute failed), `down` (a probe failed) or `off`; in the list's view also `unknown` (an endpoint not probed yet).

## The journal and the report

Every provider call, and every decision of an A/B arm `a`, is a row of the conductor's journal (`kind: provider`, the question id as its task): the provider, its mode and arm, which answer was used, the provider's answer (label, probabilities, model, time, error), the built-in decision, agreement, the fallback's reason, and a hash of the question's text. The text itself is never in the row.

`GET /v1/fleet/decision-providers/report?hours=24` reads them back per provider, question and bucket (`on`, `shadow`, `ab_a`, `ab_b`): calls, answered, failures, fallbacks and each fallback's reason counted (`fallback_reasons`, the reasons below), agreement in % (shadow), p50 and p95 of the provider's time (the built-in decider's in arm `a`) and the answers given.

**Replay.** `tools/decision_replay.py --ledger <the usage ledger's file> > answers.json` collects the recorded answers by their hash; a controller started with `TANDEM_DECISION_REPLAY=answers.json` answers every provider question from it, with no call, so a run replayed decides the same every time.

## A hook as the adapter

When your model's API has another shape, write a webhook for the `decision.ask` stage with the role `decider` ([hooks.md](hooks.md) has the signing), then a provider of kind `hook` that names it. The hook gets the question (its payload level decides whether the text comes too):

```json
{"question": {"id": "classify", "type": "choice",
              "instructions": "What kind of work does this request ask for?",
              "options": {"code_edit": "change code in a project", "chat": "a question"},
              "model": "auto", "deadline_ms": 400, "state_chars": 63},
 "content": {"state": "Rename the variable cnt to count in utils.py and run the tests."}}
```

and answers with an option id, or with its probability:

```json
{"action": "decide", "decision": {"answer": {"choice": "code_edit", "confidence": 0.91}}}
```

A bare id (`{"answer": "code_edit"}`) counts as sure (1.0). Keep the hook's own mode `on`: the provider's mode decides shadow and A/B.

## What is checked

With no provider (none stored, only disabled ones, or only ones for other questions), every decision of the conductor and every relayed byte is what it was; a provider in shadow changes no decision either (`tests/test_decision_providers.py`, `tests/test_decision_providers_api.py`). The same check with the engine's real models and tokens (G3) needs a GPU stop window; providers do not reach the engine's conductor in this version.
