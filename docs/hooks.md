# Hooks

A hook is your code at a point of a request's way through TandemApp: when it arrives, when the backend is chosen, just before it goes to the engine, on each streamed event, on the answer, on an error, after the ledger row. Add as many as you like at each point; they run in an order you set, and each one can watch, change, refuse or decide, as its stage allows.

The contract is [`contracts/hooks-v1`](../contracts/hooks-v1/README.md): the stages, the call and result schemas, examples and a fake receiver. This page explains it and shows how to write a webhook.

## Concepts

- **Stage.** Where the hook runs (table below).
- **Role.** What it does there:
  - a **transform** changes the value with a `patch`;
  - a **guard** may refuse the request;
  - a **decider** chooses (a backend, a fallback, one retry);
  - an **observer** only watches.
- **Kind.** How it is called: `http` (a webhook TandemApp POSTs to, signed), `builtin` (code shipped in TandemApp: `set`, `deny`, `route`, `fallback`, `noop`) or `plugin` (a running plugin's hook, `target` `<plugin>/<hook>`: [plugins](plugins.md#hooks)). `mcp` and `inproc` are reserved; until they exist a hook of those kinds is refused.
- **Priority.** An integer; low runs first, ties go by id.
- **Mode.** `off`, `shadow` (it runs and its answer is journaled, nothing is applied) or `on`.
- **Scope.** Which requests it sees: model globs, backends, profiles, users, sessions, client kinds. Empty means all.
- **Failure policy.** `open` (a hook that fails is skipped) or `closed` (the request is refused). Guards are closed unless you say otherwise.
- **Payload level.** How much of the request the hook sees: `meta` (default), `redacted` or `full`.

## Stages

| stage | when | may | budget | on failure | called now |
|-|-|-|-|-|-|
| `request.received` | after auth, before routing | modify, deny | 150 ms | open | yes |
| `route.select` | choose backend, model, profile | decide | 150 ms | open (the table's route) | yes |
| `request.before_engine` | the final body before sending | modify, deny | 250 ms | open | yes |
| `decision.ask` | a question for a decision provider | decide | 400 ms | open (the built-in answer) | with decision providers |
| `stream.chunk` | each event of a stream | observe | async | drop | yes |
| `tool_call.detected` | the answer holds a call | modify, deny | 500 ms | open | yes |
| `tool.before_exec`, `tool.after_exec` | Chat, around a tool | deny, modify | 500 ms | closed for guards | with Chat's stages |
| `response.after_engine` | the whole answer | modify, deny, one retry | 1 s | open | yes |
| `request.error` | an upstream error before any byte went out | choose a fallback | 250 ms | open | yes |
| `request.finished` | the ledger row is written | observe | async | drop | yes |
| `memory.write` | a note about to be stored | modify, deny | 500 ms | open | with the memory writer |
| `session.start`, `session.compact` | session events | observe, modify | 250 ms | open | with Chat's stages |
| `engine.lifecycle` | start, stop, mode, settings applied | observe | async | drop | with engine control |

`stages.json` has the fields a patch may touch at each stage. A field that holds what people wrote or the model answered (`messages`, `prompt`, `input`, `tools`, the answer's `choices`, the tool calls) can be changed only by a hook with payload `full`.

On a streamed answer, `tool_call.detected` and `response.after_engine` run after the stream has ended and only watch: the client already has the answer.

## The rules

- Transforms run first and chain: each sees what the ones before it made.
- Guards run next, on the transformed value, and the first `deny` wins. The client gets 403 `hook_denied` with the guard's `reason` and an `X-Tandem-Hook` header naming it.
- Deciders run after the guards; the first `decide` wins.
- Observers see the final value, off the request's path.
- A stage's budget is shared by its hooks per request. All sync hooks before the first token share **800 ms**: when that runs out, the hooks left are not called and their policy applies.
- No retries on the request's path; an observer is called once more if it fails.
- **Circuit breaker**, per hook: five failures in a row, or a p95 call time at its timeout over the last minute (20 calls at least), open it for 30 s. Meanwhile it is skipped and its policy applies; then one probe call closes it again or keeps it open.
- A closed hook that fails refuses the request with 503 `hook_unavailable`.
- With no hook enabled, TandemApp relays every request byte for byte as it did without hooks.

## Writing a webhook

TandemApp POSTs one JSON document per call (`call.schema.json`):

```json
{"hooks": "1.0", "id": "hd-...", "hook": "temperature-cap", "stage": "request.before_engine",
 "ts": 1763200000.0, "payload": "meta", "mode": "on",
 "request": {"request_id": "chatcmpl-px-...", "endpoint": "chat", "model": "example-model",
             "upstream_model": "example-model", "backend": "local", "profile": null,
             "user": "owner", "session": "s-1", "client_kind": "unknown", "stream": false},
 "event": {"sizes": {"body_bytes": 412, "messages": 2, "tools": 1,
                     "chars": {"system": 15, "user": 30, "assistant": 0, "tool": 0}},
           "params": {"temperature": 0.9, "max_tokens": 256}, "params_set": [],
           "tool_names": ["search"],
           "route": {"backend": "local", "model": "example-model", "attempt": 0}}}
```

and reads the answer (`result.schema.json`):

```json
{"action": "modify", "patch": {"temperature": 0.3}, "reason": "capped", "labels": {"team": "a"}}
```

`continue` (or an empty 200, or 204) changes nothing. `deny` refuses with your `reason`. `decide` carries a `decision`: `{backend, model, profile}` at `route.select`, `{fallback: {backend, model}}` at `request.error`, `{retry: true}` at `response.after_engine`, `{answer}` at `decision.ask` (a question of the conductor, asked through a `hook` [decision provider](decision-providers.md)). `labels` travel to `request.finished`. Any other answer, a status other than 200 or 204, or no answer within the timeout counts as a failure.

### Checking the signature

Every call carries `X-Tandem-Timestamp` and `X-Tandem-Signature: v1=<hex>`, the HMAC-SHA256 of `<timestamp>.` followed by the body's bytes, keyed with the hook's secret (the `hks_...` string TandemApp showed once when you made the hook). Check it on the raw bytes before you parse them, refuse a timestamp more than five minutes away, and refuse a delivery `id` you have already seen. While a rotated secret is in its grace, the header holds two `v1=` values: accept either.

Python:

<!-- example: verify.py -->
```python
import hashlib
import hmac
import json
import time

WINDOW_S = 300
seen = {}   # delivery id -> when; a shared store when the receiver runs more than one process


def verify(secret: str, headers, body: bytes) -> bool:
    """True for a call this TandemApp signed that was not seen before."""
    ts = headers.get("X-Tandem-Timestamp") or ""
    sig = headers.get("X-Tandem-Signature") or ""
    if not ts.isdigit() or abs(time.time() - int(ts)) > WINDOW_S:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(want, part.strip()[3:])
               for part in sig.split(",") if part.strip().startswith("v1=")):
        return False
    delivery = json.loads(body)["id"]
    now = time.time()
    for k in [k for k, t in seen.items() if now - t > WINDOW_S]:
        del seen[k]
    if delivery in seen:
        return False
    seen[delivery] = now
    return True
```

A whole receiver with the standard library, capping the temperature at `request.before_engine`:

```python
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["HOOK_SECRET"]


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not verify(SECRET, self.headers, body):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        call = json.loads(body)
        t = call["event"].get("params", {}).get("temperature")
        answer = {"action": "continue"}
        if isinstance(t, (int, float)) and t > 0.5:
            answer = {"action": "modify", "patch": {"temperature": 0.5}, "reason": "capped"}
        out = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


HTTPServer(("0.0.0.0", 9300), Hook).serve_forever()
```

Node (the raw body, before any JSON parser):

<!-- example: verify.js -->
```js
const crypto = require("crypto");

const WINDOW_S = 300;
const seen = new Map(); // delivery id -> when

function verify(secret, headers, body /* Buffer */) {
  const ts = headers["x-tandem-timestamp"] || "";
  const sig = headers["x-tandem-signature"] || "";
  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > WINDOW_S) return false;
  const want = crypto.createHmac("sha256", secret).update(ts + ".").update(body).digest();
  const ok = sig.split(",").map((s) => s.trim()).filter((s) => s.startsWith("v1="))
    .some((s) => {
      const got = Buffer.from(s.slice(3), "hex");
      return got.length === want.length && crypto.timingSafeEqual(got, want);
    });
  if (!ok) return false;
  const id = JSON.parse(body.toString("utf8")).id;
  const now = Date.now() / 1000;
  for (const [k, t] of seen) if (now - t > WINDOW_S) seen.delete(k);
  if (seen.has(id)) return false;
  seen.set(id, now);
  return true;
}

module.exports = { verify };
```

To try a hook without writing one, run the fake receiver: `python contracts/hooks-v1/fake_server.py --port 9300 --secret hks_... --set temperature=0.3`.

## Adding a hook

```sh
curl -s -X POST http://127.0.0.1:8080/v1/fleet/hooks -H 'Content-Type: application/json' -d '{
  "name": "temperature cap", "kind": "http", "endpoint": "http://hooks.lan:9300/",
  "role": "transform", "stages": ["request.before_engine"], "priority": 10,
  "scope": {"models": ["qwen*"]}}'
```

The answer holds the hook and, once, its `secret`. Then:

- `POST /v1/fleet/hooks/{id}/test {"stage": "request.before_engine"}` calls it once with the stage's sample event and shows what it answered and what it would change. Nothing is applied or counted.
- `PUT /v1/fleet/hooks/{id}/mode {"mode": "shadow"}` lets it see real traffic without changing it; `GET /v1/fleet/hooks/{id}/calls` shows each call's outcome and, for a shadow, the diff (field names and sizes).
- `POST /v1/fleet/hooks/{id}/disable`, `/enable`, `PATCH`, `DELETE`.
- `POST /v1/fleet/hooks/{id}/secret {"grace_s": 3600}` rotates the secret; the old one keeps signing beside it for the grace.

The routes need the admin's rights. Granting `full`, moving a full hook to another endpoint and rotating a secret need pairing's rights (with no admin password set: from the controller's own machine).

A hook's switches (enabled, mode, priority, scope, policy, timeout and payload level) are also keys of the settings interface's `hooks` scope (`hooks.<id>.<field>`, [settings](settings.md#hooks)). There a change is planned, versioned and can be restored with the other settings. The full payload keeps the same rules there: pairing's rights, and a second confirm (`confirm_grant`) for a destination outside the LAN.

A builtin hook names its `target` and `options`, for example a guard that refuses one model for one user:

```json
{"name": "no big model for guests", "kind": "builtin", "target": "deny", "role": "guard",
 "stages": ["request.received"], "scope": {"models": ["big-*"], "users": ["guest*"]},
 "options": {"reason": "ask the admin for this model"}}
```

## Payload levels

| level | the hook sees |
|-|-|
| `meta` (default) | ids, the model, sizes, counts, timings, the scalar parameters, tool names. No prompt or answer text |
| `redacted` | meta and `content`, with keys, tokens and passwords replaced by `[redacted:<kind>]` (the conductor's redaction). If the redaction fails, the hook is not called and its policy applies |
| `full` | meta and `content` as they are |

`full` is granted per hook, and the grant keeps who granted it and when. If the endpoint is outside the LAN (not loopback, a private or tailnet address, or a `.lan`, `.local`, `.home.arpa` or `.internal` name), the request answers 409 `confirm_needed` with a `confirm` token; send the same request again with `"confirm": "<token>"` to grant it. Changing a full hook's endpoint to another place outside the LAN asks again. A hook granted for the LAN checks on every call that the address it connected to is still on the LAN, and sends nothing otherwise.

## Shadow mode

A hook in `shadow` runs on every request in its scope, off the request's path, on a copy of the value it would have seen. Its answer and what it would have changed are journaled; nothing reaches the request. Use it to try a new guard on real traffic before switching it `on`.

## Metrics

`GET /v1/fleet/hooks` carries each hook's calls, failures, timeouts, p50 and p95 and its breaker. The controller's `/metrics` page has `qse_hook_calls_total`, `qse_hook_failures_total`, `qse_hook_timeouts_total`, `qse_hook_latency_ms{quantile}` and `qse_hook_breaker{state}`, labelled by hook id.

## Limits

- Hooks run in the controller. A hook that needs token ids or the decode loop runs inside the engine through its extension interface instead.
- The journal of calls is kept in memory, the last 500 calls per hook.
- A hook's answer is at most 1 MiB. A hook's timeout is at most its stage's budget; an async observer's defaults to 5 s, at most 30 s.
- On a stream, hooks watch; they cannot change what was already sent.
- `request.error` runs for failures that can still go elsewhere (a refused connection, 429, 502, 503); a backend that stays silent past its first-byte timeout answers 504 as before.
