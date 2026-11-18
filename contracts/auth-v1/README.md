# auth-v1: sign-in, sessions and API tokens

The answers and bodies of `/v1/auth/*` and the gate's refusals. [docs/auth.md](../../docs/auth.md)
says what they mean. `tests/test_auth_contract.py` validates every example against its schema and
the running server's answers against the same schemas.

| file | what |
|-|-|
| `common.schema.json` | the shared parts: a time, a role, a scope, an account, the identity provider |
| `session.schema.json` | `GET /v1/auth/session`: who the request is, its permissions, the CSRF token |
| `sign-in.schema.json` | `POST /v1/auth/session`: the body |
| `signed-in.schema.json` | `POST /v1/auth/session`: 200, with the session cookie |
| `setup.schema.json` | `POST /v1/auth/setup`: the body that makes the first owner |
| `token-new.schema.json` | `POST /v1/auth/tokens`: the body |
| `token-created.schema.json` | `POST /v1/auth/tokens`: 201, the token shown once |
| `token.schema.json` | one token, never its secret |
| `tokens.schema.json` | `GET /v1/auth/tokens` |
| `error.schema.json` | a refusal of the sign-in routes and of the gate for a session |
| `openai-error.schema.json` | a token's refusal on an OpenAI route |
| `messages-error.schema.json` | a token's refusal on a Messages API route |
| `examples/` | one example per schema (`session-anonymous.json` is `session.schema.json` without a sign-in) |

Rules every answer keeps:

- No answer holds a password, a hash, a salt, a session id or a token's secret, but the one
  answer that makes a token (`token-created`), which is `Cache-Control: no-store`.
- Every write of a session sends `X-Tandem-CSRF` with the session's `csrf`.
- `contract_version` is `1.0`. A field is only ever added; a changed meaning is a new contract.
- SRV-889 added `backends` to a token and to a new token's body: the backend ids its requests may reach, none for every backend; `models` may then name `<backend>/<model>` globs.
