# Sign-in, users and API tokens

People sign in to TandemApp with an account. Programs use an API token. Nothing else opens it:
there is no shared admin password, no open default for other devices, and no trust for a request
because it comes from the same machine, except in local mode (below).

The answers of every route on this page are in [contracts/auth-v1](../contracts/auth-v1/README.md).

## Roles

Each role holds the permissions of the roles above it in the table.

| role | may |
|-|-|
| viewer | read every page, the fleet and the metrics; use models from the page; manage their own sessions and password |
| operator | change settings and the fleet, use the Chat tab, make API tokens |
| admin | trust engine hosts and nodes (pairing, take-over); manage every account but the owners, every session and every token |
| owner | manage owners, the identity provider, the API exposure switches and the `auth` and `api` settings |

`auth.chat_min_role: viewer` opens the Chat tab to viewers. `api.tokens.viewers_may_create: on`
lets viewers make inference tokens. The last enabled owner cannot be disabled, demoted or
removed.

## The first owner

**Local mode.** A TandemApp process bound to loopback only (`127.0.0.1`, `::1`), not in a
container and not on Kubernetes, opens to this machine's browser and tools as the owner `local`
until that owner sets a password. Nothing else on the network reaches it. A tool on this machine
needs no API key: a made-up key counts as none, while a known token stays itself and a revoked or
expired one gets 401. Setting the password ends local mode at once. `TANDEM_LOCAL_MODE=off` or the
setting `auth.local_mode: off` turns it off.

**The setup code.** Everywhere else nobody signs in until the first owner exists: every route but
`/health`, the page and the sign-in answers 401. The page asks for a one-time setup code, a name
and a password. The code is 20 characters (case, spaces and dashes do not count), opens only the
setup, and is spent after one use or five wrong tries. Get one from:

- the installer's summary (`--listen` beyond loopback, or `--behind-proxy`);
- the controller's log: each start without an owner logs a fresh one, valid 24 hours;
- `tandem-app users setup-code` on the controller's machine;
- on Kubernetes, the chart's Secret or `kubectl exec ... tandem-app users setup-code`
  ([install-kubernetes.md](install-kubernetes.md)).

`tandem-app service install --host 0.0.0.0` refuses while no owner has a password, unless
`--setup-code`, which prints a code.

## Sessions

A sign-in (`POST /v1/auth/session`) sets a cookie: `__Host-tandem_session` over https,
`tandem_session` over http, `HttpOnly`, `SameSite=Strict`. A session ends when it is signed out or
revoked, after `auth.session.idle_days` (30) without use, or `auth.session.absolute_days` (180)
after it began. The server keeps only a hash of the id.

Every write of a session sends the `X-Tandem-CSRF` header from the sign-in's answer, comes from
the same origin, and declares a JSON body (a file upload excepted). A WebSocket and an event
stream are refused from another origin.

A changed password ends every other session of the account. Disabling an account, lowering its
role or removing it ends its sessions at once.

**Limits.** Five failed sign-ins a minute per address, then 429 for a minute. After five failures
for one name, each further try waits twice as long as the one before, from 2 s up to 300 s, never
a lock for good. An unknown name costs the same as a wrong password. Twenty wrong API tokens a
minute per address, then every request with a token from that address is 429 for the rest of the
minute.

Behind a reverse proxy, name the proxy in `TANDEM_TRUSTED_PROXY` (addresses and CIDR ranges,
comma separated): only then do its `X-Real-IP` and `X-Forwarded-Proto` count, so the limits key
on the client and the https cookie is set. Loopback is always trusted for these two headers.

## API tokens

A token is `tdm-<8 hex id>-<43 characters>`. It is shown once when it is made and stored as its
sha256. Make one with New API key on API, Endpoint and keys (or Create a token on the Access
page), with `POST /v1/auth/tokens`, or with `tandem-app tokens create NAME --label L --scope S`.
The dialog asks what the key is for (a coding agent, a Messages API client, a chat app, a script,
monitoring) and fills in the scopes, then which backends and models it may run, then its expiry
and limits, and shows the key once with the setup for that client.

| scope | opens |
|-|-|
| `inference:openai` | the OpenAI-compatible routes: chat and text completions, embeddings, responses, the cache routes, the model list |
| `inference:anthropic` | the Messages API (`/v1/messages`, `count_tokens`) and the model list |
| `usage:read` | the ledger's views |
| `metrics:read` | the metrics and the ledger's views |
| `settings:read`, `settings:write` | the settings |
| `fleet:read`, `fleet:write` | the fleet |
| `trust` | pairing and take-over |

`backends` narrows a token to those backends (SRV-889): a request goes only to a backend on the
list, a route's fallbacks keep only those, and a model the token may not reach is a 403 that names
the backend. `models` narrows it to model ids that match its globs (`small-*`), or to
`<backend>/<model>` globs over the backend that serves the model (`vllm1/*`,
`openrouter/anthropic/*`). Both empty: every backend and model. `/v1/models` lists a token only
the models it may run. `days` sets its expiry, or
`never`. `limits` holds `requests_per_minute`, `tokens_per_minute`, `concurrent`, `cost_per_day`
and `cost_per_month`: a rate limit is a 429 with `Retry-After`, a spend limit a 403
`spend_limit_reached`. An admin sets defaults per role (`api.limits.default.<role>.<key>`); a
token never has more than its owner's role allows, checked at each request, so a demoted owner's
token loses the rights at once.

A token never manages tokens and never signs in to the page. Rotate it with
`POST /v1/auth/tokens/{id}/rotate {grace_s}`: the new secret works at once, the old one for
`grace_s` more seconds. A revoked token's open streams are closed.

A client's `Authorization` or `x-api-key` never reaches a backend: the gate checks it and removes
it.

**Service identities.** chatd, a backend's collector and the plugin relay have identities of their
own, which no role holds. Being on the same machine opens nothing for them.

## Who may use the API from other devices

One switch per protocol, on Settings, API: `api.openai.exposure` and `api.anthropic.exposure`.

| value | who |
|-|-|
| `this-machine` (the default) | service identities, the page's own calls, and callers on this machine (with a token, outside local mode) |
| `network` | the above, plus any device with a token that holds the protocol's scope |

No value lets a device in without a token. A call from another device while the switch is
`this-machine` is 403 `api_not_exposed`. A controller bound to loopback is not reachable from other
devices whatever the switch says: change `api.listen` too (applied at the next restart). On
Kubernetes the chart's ingress decides which paths arrive at all. The health page names an exposed
protocol no token can use, one the listener or the ingress cannot reach, and tokens sent over plain
http from another device.

## Clients

An OpenAI-compatible client takes the API's address and a token:

    export OPENAI_BASE_URL=https://tandemapp.example.lan/v1
    export OPENAI_API_KEY=tdm-...

    curl $OPENAI_BASE_URL/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" \
      -H 'Content-Type: application/json' \
      -d '{"model": "small-1", "messages": [{"role": "user", "content": "hi"}]}'

A Messages API client takes the server's address (without `/v1`) and a token with
`inference:anthropic`, sent as `x-api-key` or as a bearer:

    export ANTHROPIC_BASE_URL=https://tandemapp.example.lan
    export ANTHROPIC_API_KEY=tdm-...

    curl $ANTHROPIC_BASE_URL/v1/messages -H "x-api-key: $ANTHROPIC_API_KEY" \
      -H 'anthropic-version: 2023-06-01' -H 'Content-Type: application/json' \
      -d '{"model": "small-1", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}'

Refusals come in the protocol's own shape: `{"error": {type, code, message}}` on the OpenAI
routes, `{"type": "error", "error": {type, message}}` with a `request-id` header on the Messages
API.

## Recovery

| what | do |
|-|-|
| a person lost their password | an admin on the Access page (Accounts); or on the controller's machine `tandem-app users reset-password NAME`. The answer is a one-time code, valid 24 hours: sign in with it as the password, then set one. The old password and every session of the account stop at once |
| the only owner lost the password | `tandem-app users reset-password NAME` on the controller's machine (on Kubernetes through `kubectl exec`) |
| no owner yet, the code is lost | restart the controller (its log holds a fresh code) or `tandem-app users setup-code` |
| a token leaked | revoke it (the Access page, or `tandem-app tokens revoke ID`); its open streams close |
| an account must stop now | disable it: its sessions end and its tokens stop at the next request |

The `users` and `tokens` commands work on the config database in the state directory and ask for
no password: whoever can read that directory is the owner already.

## Routes

    GET    /v1/auth/session            who this request is: account, role, permissions, CSRF token,
                                       provider, setup_required, local_mode
    POST   /v1/auth/session            {username, password}: the session cookie
    DELETE /v1/auth/session            sign out
    POST   /v1/auth/setup              {code, username, password}: the first owner; 404 once one exists
    PUT    /v1/auth/password           {current, password}
    GET    /v1/auth/sessions[?user=]   the live sessions; DELETE ends them, DELETE .../{id} one
    GET    /v1/auth/users              the accounts; POST makes one, PATCH /{id} changes one,
                                       DELETE /{id} removes one, POST /{id}/reset a one-time code
    GET    /v1/auth/audit              sign-ins, sessions, accounts, tokens and the auth settings
    GET    /v1/auth/tokens[?all=1]     tokens; POST makes one, PATCH /{id}, DELETE /{id} revokes,
                                       POST /{id}/rotate, GET /{id}/usage
    GET    /v1/auth/usage[?user=]      what an account used

## Moving from the old sign-in

An older install's secrets are imported once, at the first start of this release:

| before | after |
|-|-|
| the admin password (`admin.json`) | the account `owner` with that password |
| `TANDEM_ADMIN_TOKEN` (or `QSE_ADMIN_TOKEN`), `TANDEM_ADMIN_TOKEN_FILE` | a management token for scripts that ends after 30 days, under review, never a sign-in. With no admin password it works once as the setup code first |
| `TANDEM_API_KEYS`, the keys of `TANDEM_API_USERS_FROM`'s database, the users table | an API token each with the same hash, under review until an admin confirms it; the OpenAI exposure starts at `network` so their clients keep working |
| `TANDEM_DASHBOARD_LOGIN`, `--dashboard-login`, `--trust-loopback`, `TANDEM_AUTH` | nothing: read by nothing |
| the `qse_dash` cookie | nothing: sign in again |

A variable left in `app.env` is named on the health page (`legacy_env_set`) and never stops the
start; remove it once you will not go back. Going back to the release before works on the same
state: its tables are untouched, a key revoked here is disabled there too, and once a token exists
the release before refuses requests without a key instead of opening them.

The engine's own sign-in (`QSE_ADMIN_TOKEN` on the engine's port) is the engine's and is not part
of this.
