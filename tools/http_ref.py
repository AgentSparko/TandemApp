"""The HTTP reference of TandemApp's docs (docs/reference/http.md): every route, header and error,
checked against the code.

TandemApp's copy of the engine's tools/http_ref.py (ENG-413): the controller's routes
(`tandem-app`, proxy/app.py), the routes its extension serves inside a TandemLLM engine in chat
mode, the host collector, and the headers and errors TandemApp's code (proxy/, tandemapp/) reads,
writes or answers. The engine's own API is in the engine's docs.

The routes, headers and errors are written here by hand, because a handler's purpose and the
shape of its body are not in the code in a form a script can read. What keeps them true is the
check: `problems()` names

  - a path the controller's handlers test that no route row covers (a route was added),
  - a route row whose path the handlers never test (a route was removed or renamed),
  - a header name the code reads or writes that no header row names, and a row the code never uses,
  - an error type or code the code puts in an error body that no error row names, and a row
    whose types the code never uses.

The extension's routes are not checked against a handler: the engine dispatches them.
`gen_reference.py` writes the tables and fails (`--check` too) while any problem is open.
"""

from __future__ import annotations

import ast
import os
import re

# --------------------------------------------------------------------------- who may call a route
# The controller's routes: the permission proxy/authz.py's route table names for the row's method
# and path, read from that file (`authz_routes`), and the lowest role that holds it. PERMS says
# what each permission is; a permission the table uses and PERMS lacks is a problem.
PERMS = {
    "open": "anyone who reaches the port: the page shell, /health's status, the sign-in and the first-run setup (which checks its own setup code)",
    "session-address": "a /s/<token>/ address: its token is the credential, for the library and Laya only",
    "read": "every read of the page. For the fleet, the settings, the configuration and the conductor a write needs its kind's own permission too: settings:write, fleet:write or trust",
    "metrics:read": "the Prometheus page",
    "fleet:read": "the fleet's reads and the cache peek",
    "inference": "the data plane: the OpenAI and Messages API routes, the models, the library and Laya; an API token needs the protocol's scope (inference:openai, inference:anthropic)",
    "self": "a signed-in person's own sessions, password, API tokens and usage",
    "settings:write": "changes to the settings, the configuration and the conductor's documents",
    "fleet:write": "the fleet's writes: nodes, deployments, the Setup Guide relay, clearing a cache",
    "chat": "the Chat tab",
    "users": "every account but the owners, every session, the audit",
    "trust": "trusting a plugin, a publisher key or a node",
    "owner": "the owners, the identity provider, the exposure switches and the auth and api settings",
    "collect:write": "a service identity only: a backend's collector or plugin (svc:collect:<id>)",
    "chat-engine": "a service identity only: chatd (svc:chatd)",
    "plugin-backend": "a service identity only: the plugin relay, in process",
}

# The routes the extension serves inside an engine follow the engine's own rule (the engine's docs):
WHO = {
    "open": "anyone who reaches the port",
    "admin": "the admin rule: anyone while no admin password is set; with one, the admin token, a dashboard session, or a tool on the machine itself",
    "admin-bearer": "as admin, but the token only as a bearer header, never the session cookie",
    "dash": "the dashboard rule: anyone unless a password is set and the dashboard login is on",
    "metrics": "the metrics token when one is set, else as admin",
    "session": "only under a session address, /s/<token>/...",
    "key": "anyone until the first user key exists; then a user key, except from the machine itself",
    "collect": "the collect token, from every address",
}

# --------------------------------------------------------------------------- the extension's routes
# (method, path, who, what, body, answer): what tandemapp/engine_ext.py serves inside a TandemLLM
# engine in chat mode (engine-ext-v1 `routes`); the engine dispatches them, and a headless engine
# answers them 404 not_on_engine. `path` placeholders are {name}; a query is ?a=&b=.
EXT_ROUTES = [
    ("*", "/v1/conductor/*", "admin",
     "The conductor's documents, journal, sessions and memory (Table tab:ref-conductor-routes). Reads follow the dashboard rule, writes and every memory route the admin rule. On a headless engine, or one without --conductor-config, 404.",
     "", ""),
    ("GET", "/v1/laya", "open",
     "What the Laya route takes and answers, its limits, and whether it is on.", "", "a description of the route"),
    ("POST", "/v1/laya/ask", "open",
     "A typed answer from Laya on one text or many, for the model's own scripts; rate-limited.",
     "{question, choices | scale | neither, text | texts, threshold?, model?, deadline_ms?}",
     "{label, p, sure, probs, model, ms, ...}, or {answers: [...]} for texts"),
    ("POST", "/v1/laya/drive/end", "open",
     "Ends a playtest driver session and writes its journal row.", "the session's summary", "the row"),
    ("GET", "/v1/laya/kit/{file}", "open",
     "A file of the playtest kit the model copies into a project.", "", "the file"),
    ("OPTIONS", "/v1/laya/*", "open",
     "A browser's CORS preflight for the Laya routes. Every other path answers OPTIONS with 501.", "", "204 with the CORS headers"),
    ("GET", "/v1/library", "open",
     "The package library: every enabled skill's name, description and classes.", "", "the list and the two routes below"),
    ("GET", "/v1/library/search?q=&k=&class=", "open",
     "BM25 search over the skills, best first (k from 1 to 20, default 5).", "", "[{name, description, score}]"),
    ("GET", "/v1/library/{name}", "open",
     "One skill's text and its reference.", "", "{name, description, classes, body, reference}"),
    ("GET", "/s/{token}/v1/memory", "session",
     "The memory index this session sees. The /s/{token}/ prefix also serves the library and Laya routes, filed to the session.",
     "", "the notes' index lines"),
    ("GET", "/s/{token}/v1/memory/search?q=&k=", "session",
     "Memory notes that match, best first (k from 1 to 10).", "", "[{id, kind, scope, status, title, score}]"),
    ("GET", "/s/{token}/v1/memory/{id}", "session",
     "One note.", "", "{id, title, kind, scope, status, body, ...}"),
    ("POST", "/s/{token}/v1/memory", "session",
     "Writes, changes or archives a note, with the fields of the hidden memory block.",
     "{op, scope, kind, title, tags, body, id, reason}", "{op, id, title, ...}, or 422 with the reason as the type"),
]

# --------------------------------------------------------------------------- the standalone proxy's routes
PROXY_ROUTES = [
    ("POST", "/v1/chat/completions", "key",
     "Relayed to the backend the model id routes to (Section sec:routing); the answer's bytes go back unchanged. The same for /v1/completions, /v1/embeddings and /v1/responses.",
     "the client's body", "the backend's answer, with X-Tandem-Backend"),
    ("POST", "/v1/completions", "key", "As /v1/chat/completions.", "", ""),
    ("POST", "/v1/embeddings", "key", "As /v1/chat/completions; not for deepseek backends.", "", ""),
    ("POST", "/v1/responses", "key", "As /v1/chat/completions; openai backends only.", "", ""),
    ("GET", "/v1/models", "key",
     "Every backend's models under their real ids, owned_by the backend.", "", "{object: list, data: [... with a tandem block]}"),
    ("GET", "/v1/models/{id}", "key", "One model's card.", "", "the card, or 404 model_not_found"),
    ("GET", "/", "open", "Redirects to the dashboard.", "", "302 to /dashboard/"),
    ("GET", "/dashboard/", "open", "The dashboard's static files.", "", "the page"),
    ("GET", "/v1/dashboard/{view}", "dash",
     "summary, usage, requests, system, live, logs, metrics, parallel and session, as on the engine; the engine-only panels answer 404.",
     "", "as on the engine"),
    ("POST", "/v1/dashboard/session", "open", "As on the engine.", "{token}", "the session"),
    ("*", "/v1/config/*", "admin", "The Backends and Setup screens (Table tab:ref-config-routes).", "", ""),
    ("GET", "/v1/fleet/*", "dash",
     "TandemApp's fleet (Section sec:fleet): nodes, links, topology, deployments, per-node metrics, memory, logs, events, alerts, rules and a page's window layout, as app/contracts/fleet-v1 says.",
     "", "the contract's JSON"),
    ("*", "/v1/fleet/*", "admin",
     "The fleet's writes: PATCH /v1/fleet/nodes/{id} (alias, colour, icon, tags), PUT /v1/fleet/layouts/{page}, POST /v1/fleet/alerts/{id}/ack.",
     "the contract's body", "the node, the layout as saved, or the alert"),
    ("POST", "/v1/collect/{backend}", "collect", "A plugin's batch (as on the engine).", "", ""),
    ("GET", "/metrics", "metrics", "The proxy's own Prometheus families.", "", "Prometheus text format"),
    ("GET", "/health", "open",
     "The proxy's health, for a container's check; the full body for the admin or the machine. Also /healthz and /v1/health.",
     "", "{status}"),
    ("*", "/v1/conductor/*", "admin",
     "The conductor in front of the backends, with --conductor-config; the engine-only screens answer 404.", "", ""),
    ("GET", "/v1/library", "open", "The package library, as on the engine, also under /v1/library/*.", "", ""),
    ("GET", "/v1/laya", "open", "Laya, when the decider role names an endpoint.", "", ""),
    ("POST", "/v1/laya/ask", "open", "As on the engine, relayed to the decider role's endpoint.", "", ""),
    ("POST", "/v1/messages", "key",
     "The Messages API: translated to a chat completion and run through the pipeline, or passed through untouched when every backend of the model's route is of kind anthropic (docs/proxy.md).",
     "the Messages API's body, the key as x-api-key or a bearer", "the Messages API's answer or event stream, with request-id"),
    ("POST", "/v1/messages/count_tokens", "key",
     "The prompt's exact token count from a backend that can count it; any other answers 404 not_found_error.", "", "{input_tokens}"),
    ("POST", "/v1/cache/peek", "key",
     "With a deployment's model: relayed to the member that holds the conversation; the same for /v1/cache/prewarm and /v1/cache/release. Without the model: 400 model_required.",
     "{model, session?, ...}", "the member's answer, with X-Tandem-Node and X-Tandem-Deployment"),
    ("POST", "/v1/cache/prewarm", "key", "As /v1/cache/peek.", "", ""),
    ("POST", "/v1/cache/release", "key", "As /v1/cache/peek.", "", ""),
    ("GET|POST|DELETE", "/v1/auth/session", "open",
     "Who this request is (account, role, permissions, CSRF token, provider, setup_required, local_mode); POST signs in, DELETE signs out (docs/auth.md).",
     "POST: {username, password}", "the session; the cookie on a sign-in"),
    ("POST", "/v1/auth/setup", "open", "The first owner, with the one-time setup code; 404 once an owner exists.",
     "{code, username, password}", "the session"),
    ("PUT", "/v1/auth/password", "self", "A signed-in person's own password.", "{current, password}", ""),
    ("GET|DELETE", "/v1/auth/sessions", "self",
     "The live sessions (?user= another's, with the users permission); DELETE ends them, DELETE .../{id} one.", "", ""),
    ("GET|POST|PATCH|DELETE", "/v1/auth/tokens", "self",
     "API tokens (?all=1 every account's): POST makes one, PATCH /{id} changes one, DELETE /{id} revokes, POST /{id}/rotate, GET /{id}/usage.",
     "", "a new token's secret once"),
    ("GET|POST|PATCH|DELETE", "/v1/auth/users", "users",
     "The accounts: POST makes one, PATCH /{id} changes one, DELETE /{id} removes one, POST /{id}/reset a one-time code.", "", ""),
    ("GET", "/v1/auth/audit", "users", "Sign-ins, sessions, accounts, tokens and the auth settings, as audit rows.", "", ""),
    ("GET", "/v1/auth/usage", "self", "What an account used (?user= another's, with the users permission).", "", ""),
    ("GET", "/v1/chat-app/capabilities", "dash", "What the Chat tab can do here: whether chatd answers, the sandbox, the session.", "", ""),
    ("*", "/v1/chat-app/*", "admin", "The Chat tab: relayed to chatd behind the controller (off, own or external; docs/chat.md).", "", "chatd's answer"),
    ("GET|POST", "/s/{token}/v1/library/*", "session",
     "The library and Laya under a session address, the address the notes give the model, journaled to that session.", "", ""),
    ("*", "/v1/settings/*", "admin",
     "The settings interface over every store: scopes, change sets, versions and restore, the audit (docs/settings.md). Reads need the read permission, writes the scope's own.",
     "the contract's body", "the contract's JSON"),
    ("GET", "/plugin-panels/{id}/{panel}/", "dash",
     "A running plugin's panel: its HTML and the files beside it, served sandboxed for the page's frame (docs/plugins.md).",
     "", "the panel's files"),
    ("*", "/plugin-backends/{id}/{name}", "open",
     "A plugin's backend, the URL of its row in the backends store; the gateway routes a model there. From this machine only (403 forbidden otherwise).",
     "the relayed request", "the plugin's answer"),
]

# The host collector beside the controller (the engine's status responder is in the engine's docs).
SIDE_ROUTES = [
    ("tandem-app hostcollect", "GET", "/host?watch=", "the machine's sample as JSON (the bearer token when --token is set)"),
    ("tandem-app hostcollect", "GET", "/metrics", "the same sample in the Prometheus text format"),
    ("tandem-app hostcollect", "GET", "/health", "{status: ok}, no token"),
]

# --------------------------------------------------------------------------- headers
# (name, direction, where, meaning). direction: in (TandemApp reads it), out (it writes it), both.
# where: engine (the extension, inside an engine), proxy (the controller and TandemNode), both.
HEADERS = [
    ("x-api-key", "in", "controller",
     "a Messages API client's credential, as an API token or a backend key would be sent as a bearer; removed from the request once checked"),
    ("X-Tandem-Auth", "out", "controller", "accounts on /health: the controller runs the accounts sign-in, so a front proxy asks it"),
    ("X-Tandem-CSRF", "in", "controller",
     "a cookie request that writes carries the session's token (GET /v1/auth/session returns it); without it 403"),
    ("X-Tandem-Dropped", "out", "controller",
     "the client's fields a translated request went without (a Messages API field the chat shape has no place for, thinking the stash evicted)"),
    ("X-Tandem-Hook", "both", "controller",
     "to a webhook: the hook's id; on an answer a guard refused: the hook that refused it"),
    ("X-Tandem-Delivery", "out", "controller", "to a webhook: the call's id, also the body's id, which the signature covers"),
    ("X-Tandem-Timestamp", "out", "controller", "to a webhook: unix seconds, inside the signed text"),
    ("X-Tandem-Signature", "out", "controller",
     "to a webhook: v1=<hex HMAC-SHA256(secret, timestamp.body)>, one v1= per secret while a secret rotates (docs/hooks.md)"),
    ("X-Tandem-*", "out", "controller",
     "every X-Tandem- answer header, and the backend's anthropic-ratelimit-* headers, also reach a Messages API client"),
    ("X-Tandem", "both", "both",
     "raw, shadow or on: lowers the conductor's mode for one request; the response says which mode ran"),
    ("X-Tandem-Profile", "both", "both", "picks a conductor profile; the response names the profile that ran; an unknown name is a 400"),
    ("X-Tandem-Memory", "in", "engine", "off: no memory note is read or written; read: reads only, a block the model writes is hidden and dropped"),
    ("X-Tandem-User", "in", "engine", "a proxy's user name for the memory notes, honoured from loopback or QWEN38_MEMORY_PROXIES"),
    ("X-Tandem-Relay-Id", "in", "engine", "a proxy's id for a relayed request, for the live counts; a relayed request without X-Tandem-User touches no memory"),
    ("X-Tandem-Parent", "in", "engine", "the request a call of TandemLLM to itself serves; the row becomes client kind internal"),
    ("X-Tandem-Forward", "out", "engine", "to chatd on a /v1/chat-app request: the forward key, the only proof chatd accepts"),
    ("X-Tandem-Principal", "out", "engine", "to chatd: who the request is for (owner)"),
    ("X-Tandem-Password-Set", "out", "engine", "to chatd: 1 when an admin password is set"),
    ("X-Tandem-Signed-In", "out", "engine", "to chatd: 1 when the request passed the dashboard's rule"),
    ("X-Tandem-Backend", "out", "proxy", "the id of the backend that answered a relayed request"),
    ("X-Tandem-Deployment", "out", "proxy", "the deployment that answered a request the gateway routed to one of its members"),
    ("X-Tandem-Node", "out", "proxy", "the alias of the node whose engine answered a request the gateway routed"),
    ("X-Tandem-Pin", "in", "proxy", "a node's id or alias: the request goes to that member of the deployment and to no other (the Playground's A/B of a deployment's members)"),
    ("X-Tandem-Gateway", "out", "proxy", "tandem-app on the controller's /v1/models answers, so a page behind a reverse proxy knows whether its /v1 reaches the controller or an engine"),
    ("X-Tandem-Bundle", "out", "both", "on the page's files: the bundle's tag, or none while no bundle is built and the placeholder answers"),
    ("X-Tandem-Copy-Token", "in", "proxy", "TandemNode: the token of one weights copy between two nodes; a file sent without it is refused"),
    ("X-Tandem-Cost", "in", "proxy", "1 on a streamed relayed request: one SSE comment before data: [DONE] carries the request's cost (: tandem-cost {\"cost_micros\": N, \"backend\": id}); without it the stream is the backend's"),
    ("x-session-id", "in", "both", "opencode's session id: the router's session and the caches' session key"),
    ("X-Conversation-Id", "in", "both", "a conversation id for clients that send no session field; read after the body's own ids"),
    ("X-OpenWebUI-Chat-Id", "in", "both", "Open WebUI's chat id (a connection header with {{CHAT_ID}}): the router's session for that chat"),
    ("X-Request-Id", "both", "proxy", "sent to a vllm backend as px-<hex>, which the plugin reports back for the join; a client's own id is kept"),
    ("X-QSE-Client", "in", "engine", "dashboard marks the dashboard's own requests (client kind dashboard)"),
    ("X-Real-IP", "in", "both", "set by a reverse proxy; a request that carries it is never trusted as local; the sign-in's rate limit counts per address"),
    ("X-Forwarded-For", "in", "both", "as X-Real-IP: a request that carries it is never trusted as local"),
    ("X-Forwarded-Proto", "in", "both", "https makes the session cookie Secure, and the notes' {tandem_url} https"),
    ("X-Forwarded-Host", "in", "engine", "the host the notes' {tandem_url} names, unless QWEN38_TANDEM_URL sets it"),
    ("X-Accel-Buffering", "out", "both", "no, on every stream, so nginx sends each event at once"),
    ("X-Content-Type-Options", "out", "both", "nosniff on the dashboard's static files"),
    ("x-ratelimit-*", "out", "proxy", "a cloud backend's rate-limit headers, passed on to the client"),
    ("OpenAI-Beta", "both", "proxy", "a client's OpenAI-Beta header, passed on to an openai backend"),
    ("Authorization", "in", "both", "Bearer: an API token (tdm-...), a service identity's token (a collector, chatd) or a management token for scripts, by route (docs/auth.md); the engine's OpenAI routes ignore it and file the request under a hash of it"),
    ("User-Agent", "in", "both", "the client kind in the ledger: open-webui, opencode, openai-sdk, curl, other (a Hermes-shaped body: hermes)"),
    ("Last-Event-ID", "in", "both", "resumes the log stream after that line"),
    ("Retry-After", "out", "both", "with a 503 (queue full, memory gate, draining, a full backend), a 429, or a decision model that is loading"),
    ("WWW-Authenticate", "out", "both", "Bearer, with a 401"),
    ("Set-Cookie", "out", "both", "qse_dash, the dashboard session: HttpOnly, SameSite=Strict, Secure behind https"),
    ("Access-Control-Allow-Origin", "out", "engine", "* on the library and Laya routes, so a page the model builds can call them; with Access-Control-Allow-Methods, -Headers and -Max-Age on the preflight"),
    ("Access-Control-Allow-Methods", "out", "engine", "on the Laya preflight"),
    ("Access-Control-Allow-Headers", "out", "engine", "on the Laya preflight"),
    ("Access-Control-Max-Age", "out", "engine", "on the Laya preflight"),
]

# --------------------------------------------------------------------------- errors
# (status, types, when, retry_after). `types` are the error.type (and error.code) values.
ERRORS = [
    ("400", ["invalid_request_error"],
     "a request TandemApp will not relay as asked: error.param names the field", False),
    ("400", ["bad_request"], "a dashboard, fleet or conductor query or body that does not parse (a level, a range, a number, a body that is not an object)", False),
    ("400", ["model_required"], "a cache route through the gateway without the model that names the deployment", False),
    ("400", ["context_length_exceeded"], "a prompt and max_tokens that no member of the deployment has the window for", False),
    ("401", ["unauthorized"], "a route that needs a credential, without a session or an API token, or with one that is unknown, revoked or expired; a wrong collect token; wrong_credentials: a sign-in whose name or password is wrong", False),
    ("401", ["invalid_api_key"], "the data plane (OpenAI shape): no API token, a token it does not know, or two credentials that name different tokens (code invalid_api_key)", False),
    ("403", ["forbidden"], "a memory route outside a known session address; a cross-site write; a weights copy with another token", False),
    ("403", ["wrong_password"], "PUT /v1/auth/password whose current password is not right", False),
    ("404", ["not_found"], "no such route, or a route whose part is off on this server (the conductor, the backends, the fleet)", False),
    ("404", ["unsupported_route"], "the proxy: a route the backend's kind does not serve, such as embeddings on deepseek (code unsupported_route)", False),
    ("404", ["model_not_found"], "a model id no backend serves, or one two backends serve (the message names the prefixed forms) (code model_not_found)", False),
    ("405", ["method_not_allowed"], "the sign-in routes, the accounts or the Setup Guide's relay with a method they do not take", False),
    ("409", ["busy"], "an Add Spark session or an install that already runs; a chatd job that runs on TandemNode", False),
    ("409", ["closed", "stale"], "a package proposal already closed, or whose base text changed since", False),
    ("409", ["conflict"], "an Add Spark session in another state, a device picked twice, more devices than one add takes", False),
    ("409", ["not_a_member"], "X-Tandem-Pin names a node that is not a member of the deployment", False),
    ("413", ["too_large"], "a request body over the limit (a fleet request's body over 4 MiB)", False),
    ("422", ["invalid_request_error", "invalid_report"],
     "a value the schema refuses: a config field (error.field), a field an environment variable locks (error.variable), a Laya, library or memory call's fields, an eval report", False),
    ("422", ["schema", "secret", "cap", "user", "unknown_id", "unknown_package", "rule_from_tool", "no_session", "no_user",
             "refused_root", "no_proposals", "propose"],
     "a memory note or a package proposal refused, with the reason as the type: a field (schema), a secret, a cap, an unknown note or package", False),
    ("429", ["too_many"], "a fifth log or live stream at once; a sixth failed sign-in in a minute; too many wrong passwords or pairing attempts", True),
    ("429", ["rate_limited"], "the library, Laya or memory routes past their calls a minute", True),
    ("500", ["internal_error", "error"], "a bug, or a fleet job that failed on an exception: the message is the exception's", False),
    ("502", ["connect_error", "upstream_error", "key_unreadable"], "the proxy could not reach the backend or read its stored key, after its retries", False),
    ("502", ["unreachable"], "Add Spark: a paired device whose agent does not answer, or answers with an error", False),
    ("200", ["upstream_closed", "upstream_error"], "a stream that already began: the backend failed or closed early; the client gets an error event, then [DONE]", False),
    ("503", ["server_busy"], "a backend or a deployment that cannot take the request now; error.code says why (the codes below)", True),
    ("503", ["max_inflight"], "every backend for the model is at the max_inflight you set (Retry-After: 1)", True),
    ("503", ["deployment_stopped"], "a request for a deployment that is stopped, or a Split without a plan", True),
    ("503", ["no_member", "member_not_serving"], "a deployment with no member that takes requests now, or the pinned member does not", True),
    ("503", ["node_draining"], "a member whose node is drained for maintenance", True),
    ("503", ["not_loaded"], "a Laya call while the engine has handed over no decision model", True),
    ("504", ["upstream_timeout", "timeout"], "a backend that sent no first byte in time; a Laya call past its deadline", False),
]


# --------------------------------------------------------------------------- the scan
HANDLERS = {
    "proxy/app.py": ("do_GET", "do_POST", "do_PUT", "do_PATCH", "do_DELETE", "do_OPTIONS",
                     "_dashboard_get", "_strip_session"),
}
# TandemApp's controller and the extension (the engine's server/ is checked by the engine's copy)
HEADER_DIRS = ("proxy", "tandemapp")
_HEADER = re.compile(r"^(?:[Xx]-[A-Za-z0-9-]+|Retry-After|WWW-Authenticate|Set-Cookie|Last-Event-ID"
                     r"|OpenAI-[A-Za-z-]+|Access-Control-[A-Za-z-]+)$")
# code that is not part of the served API: test doubles, and the kit the model copies into a project
_SKIP = ("fake", "laya_kit")


def _parse(root: str, rel: str):
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return ast.parse(fh.read())
    except (OSError, SyntaxError):
        return None


def handler_paths(root: str) -> dict:
    """{rel: set of the path literals the request handlers test}."""
    out = {}
    for rel, names in HANDLERS.items():
        tree = _parse(root, rel)
        found = set()
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    # what tests a path: a comparison (== or in), a startswith(), a dispatch dict's key
                    tested = []
                    for c in ast.walk(node):
                        if isinstance(c, ast.Compare):
                            tested += [c.left, *c.comparators]
                        elif isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "startswith":
                            tested += c.args
                        elif isinstance(c, ast.Dict):
                            tested += [k for k in c.keys if k is not None]
                    for t in tested:
                        for c in ast.walk(t):
                            if (isinstance(c, ast.Constant) and isinstance(c.value, str)
                                    and c.value.startswith("/") and " " not in c.value):
                                found.add(c.value.rstrip("/") or "/")
        out[rel] = found
    # the proxy's completion routes are a constant of proxy/service.py
    tree = _parse(root, "proxy/service.py")
    if tree is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PROXY_PATHS" for t in node.targets)
                    and isinstance(node.value, ast.Tuple)):
                out["proxy/app.py"] |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return out


def _py_files(root: str, dirs: tuple):
    for d in dirs:
        for dp, _, fs in os.walk(os.path.join(root, d)):
            if any(s in os.path.relpath(dp, root) for s in _SKIP):
                continue
            for f in sorted(fs):
                if f.endswith(".py") and not f.startswith(_SKIP):
                    yield os.path.relpath(os.path.join(dp, f), root)


def header_names(root: str) -> set:
    """Lower-cased header names the server and the proxy read or write."""
    out = set()
    for rel in _py_files(root, HEADER_DIRS):
        tree = _parse(root, rel)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and _HEADER.match(n.value):
                out.add(n.value.lower())
    return out


def _text(node) -> str:
    """A message argument as text: a string, or an f-string with {...} for its values."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{...}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    return ""


_TYPE = re.compile(r"[a-z][a-z_]+")
# the calls that raise or answer an error: an ...Error class, a Refused or Refusal, a refuse helper,
# _err and _json (a 200 call with a status-like number, such as a token budget, is no error)
_ERRCALL = re.compile(r"(?:Error|Refused|Refusal|_refuse|^_err|^_json)$")


def error_sites(root: str) -> list:
    """(status or None, type, message, rel) for every error.type and error.code the code puts in
    an error body or raises with a status."""
    out = []
    for rel in _py_files(root, HEADER_DIRS):
        tree = _parse(root, rel)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == "error" and isinstance(v, ast.Dict):
                        msg = next((_text(v2) for k2, v2 in zip(v.keys, v.values)
                                    if isinstance(k2, ast.Constant) and k2.value == "message"), "")
                        for k2, v2 in zip(v.keys, v.values):
                            if (isinstance(k2, ast.Constant) and k2.value in ("type", "code")
                                    and isinstance(v2, ast.Constant) and isinstance(v2.value, str)
                                    and _TYPE.fullmatch(v2.value)):
                                out.append((None, v2.value, msg, rel))
            elif isinstance(n, ast.Call):
                fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                args = n.args
                if fn == "_error_body":
                    msg = _text(args[0]) if args else ""
                    out += [(None, a.value, msg, rel) for a in args[1:3]
                            if isinstance(a, ast.Constant) and isinstance(a.value, str) and _TYPE.fullmatch(a.value)]
                elif (fn == "Refused" and len(args) >= 2 and isinstance(args[0], ast.Constant)
                      and isinstance(args[0].value, str) and _TYPE.fullmatch(args[0].value)):
                    out.append((422, args[0].value, _text(args[1]), rel))   # a memory note's or a proposal's reason
                elif fn == "_busy":
                    out.append((503, "server_busy", "", rel))
                elif _ERRCALL.search(fn):
                    # any error a call raises or answers as (..., status, "type", message?): Refused,
                    # ApiError, AddError, UpdateError, PluginError, _err(handler, 403, "type", ...)
                    for x, (a, b) in enumerate(zip(args, args[1:])):
                        if (isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool)
                                and 400 <= a.value <= 599 and isinstance(b, ast.Constant)
                                and isinstance(b.value, str) and _TYPE.fullmatch(b.value)):
                            msg = _text(args[x + 2]) if len(args) > x + 2 else ""
                            out.append((a.value, b.value, msg, rel))
    return out


def error_types(root: str) -> set:
    """The error.type and error.code values the code puts in an error body."""
    return {t for _, t, _, _ in error_sites(root)}


class _Eval:
    """A small evaluator for proxy/authz.py's module constants: literals, frozenset(), |, names,
    and the route table's starred generators."""

    def __init__(self, env: dict):
        self.env = env

    def __call__(self, n):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.Tuple, ast.List)):
            out = []
            for e in n.elts:
                if isinstance(e, ast.Starred):
                    out.extend(self(e.value))
                else:
                    out.append(self(e))
            return tuple(out)
        if isinstance(n, ast.Set):
            return frozenset(self(e) for e in n.elts)
        if isinstance(n, ast.Name):
            return self.env[n.id]
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "frozenset":
            return frozenset(self(n.args[0])) if n.args else frozenset()
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
            return frozenset(self(n.left)) | frozenset(self(n.right))
        if isinstance(n, ast.GeneratorExp) and len(n.generators) == 1:
            g = n.generators[0]
            out = []
            for v in self(g.iter):
                self.env[g.target.id] = v
                out.append(self(n.elt))
            return tuple(out)
        raise ValueError(ast.dump(n)[:80])


def authz_routes(root: str):
    """(ROUTES, {role: permissions}) of proxy/authz.py, read without importing it."""
    env = {}
    svc = _parse(root, "proxy/service.py")
    for n in svc.body if svc else ():
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PROXY_PATHS":
            env["PROXY_PATHS"] = ast.literal_eval(n.value)
    tree = _parse(root, "proxy/authz.py")
    ev = _Eval(env)
    for n in tree.body:
        target = n.targets[0] if isinstance(n, ast.Assign) else (n.target if isinstance(n, ast.AnnAssign) else None)
        if target is None or not isinstance(target, ast.Name) or n.value is None:
            continue
        try:
            env[target.id] = ev(n.value)
        except (ValueError, KeyError):
            pass
    roles = {r: env[r.upper()] for r in env["ROLES"]}
    return list(env["ROUTES"]), roles


def _lowest(roles: dict, perm: str) -> str:
    if perm == "open":
        return "anyone"
    if perm == "session-address":
        return "the session address"
    for r, perms in roles.items():
        if perm in perms:
            return r + (" and up" if r != "owner" else "")
    return "a service identity"


def _route_method(method: str) -> str:
    return "GET" if method == "*" else method.split("|")[0]


def who_for(routes: list, method: str, path: str) -> str:
    """The permission the controller's route table names for a row (its first method)."""
    p = _static(path)
    m = _route_method(method)
    for methods, how, route, perm in routes:
        if m not in methods:
            continue
        if (how == "exact" and p == route) or (how == "prefix" and (p == route or p.startswith(route.rstrip("/") + "/"))):
            return perm
    return "none (refused)"


def _static(path: str) -> str:
    """A documented path up to its first placeholder or query: what the code must test."""
    p = re.split(r"[{<*?]", path, maxsplit=1)[0]
    return p.rstrip("/") or "/"


def _unsession(path: str) -> str:
    """/s/{token}/v1/memory... is the handler's /v1/memory... after the session prefix is cut."""
    return re.sub(r"^/s/\{token\}", "", path)


def _covers(doc_paths: list, lit: str) -> bool:
    for d in map(_unsession, doc_paths):
        base, st = d.split("?")[0], _static(d)
        if base == lit or base.startswith(lit + "/") or st == lit:
            return True
        if re.search(r"[{*]", base) and lit.startswith(st + "/"):
            return True                   # a placeholder or a wildcard row covers what is under it
    return False


ALIASES = {"/healthz": "/health", "/v1/health": "/health", "/dashboard": "/dashboard/"}


def problems(root: str) -> list:
    out = []
    lits = handler_paths(root)
    for rel, rows in (("proxy/app.py", PROXY_ROUTES),):
        docs = [r[1] for r in rows]
        found = lits.get(rel, set())
        for lit in sorted(found):
            if lit.startswith("/s/"):
                continue
            target = ALIASES.get(lit, lit)
            if not _covers(docs, target) and not _covers(docs, lit):
                out.append(f"undocumented route in {rel}: {lit}")
        for d in docs:
            s = _static(_unsession(d))
            if s == "/":
                ok = "/" in found
            else:
                ok = any(l == s or l.startswith(s + "/") or s.startswith(l + "/") or s.startswith(l)
                         for l in found if l != "/")
            if not ok and rel == "proxy/app.py":
                # a route a helper serves (the Messages API, the sign-in, the Chat tab's relay):
                # the controller's route table names it
                ok = not who_for(authz_routes(root)[0], "GET" if d.endswith("*") else "POST", d).startswith("none") \
                    or any(not who_for(authz_routes(root)[0], m, d).startswith("none") for m in ("GET", "PUT", "DELETE"))
            if not ok:
                out.append(f"documented route not in {rel}: {d}")
    # every route family of the controller's route table has a row (a route served through a helper
    # the literal scan above cannot see: the Messages API, the sign-in, the Chat tab)
    routes, roles = authz_routes(root)
    docs = [r[1] for r in PROXY_ROUTES]
    for methods, how, route, perm in routes:
        if route.startswith(("/chat-engine", "/v1/dashboard/session", "/v1/fleet/session", "/v1/chat-app/session")):
            continue                        # chatd's own relay, and the old sign-in's routes
        if route in ALIASES:
            continue                        # /healthz and /v1/health: the /health row names them
        if not (_covers(docs, route) or any(_static(d) == route or _static(d).startswith(route.rstrip("/") + "/") for d in docs)):
            out.append(f"no route row for the route table's {how} {route} (proxy/authz.py)")
        if perm not in PERMS:
            out.append(f"no description of the permission {perm} (PERMS)")
    for method, path, *_ in PROXY_ROUTES:
        if who_for(routes, method, path).startswith("none"):
            out.append(f"a route row the route table refuses: {method} {path}")
    code_h = header_names(root)
    doc_h = {h[0].lower() for h in HEADERS}
    for h in sorted(code_h):
        if h not in doc_h and not any(d.endswith("*") and h.startswith(d[:-1]) for d in doc_h):
            out.append(f"undocumented header: {h}")
    for d in sorted(doc_h):
        if d in ("authorization", "user-agent"):
            continue                      # read through helpers, not as bare literals everywhere
        if d.endswith("*"):
            if not any(h.startswith(d[:-1]) for h in code_h):
                out.append(f"documented header not in the code: {d}")
        elif d not in code_h:
            out.append(f"documented header not in the code: {d}")
    code_e = error_types(root)
    doc_e = {t for row in ERRORS for t in row[1]}
    # every type the code has and no row explains is listed by gen_errors' second table
    for t in sorted(doc_e - code_e):
        if t in ("invalid_api_key",):
            continue                      # built from a variable (the runtime's state, an f-string)
        out.append(f"documented error type not in the code: {t}")
    return out


# --------------------------------------------------------------------------- rendering
# "Section x" and "Table x" in a row's text become links to the page that has it
LINKS = {
    "sec:fleet": ("the fleet", "../proxy.md#the-fleet-v1fleet"),
    "sec:routing": ("routing by model id", "../backends.md#routing-by-model-id"),
    "tab:ref-conductor-routes": ("the conductor's routes", "#the-conductors-routes-v1conductor"),
    "tab:ref-config-routes": ("the configuration routes", "#the-configuration-routes-v1config"),
}
_REF = re.compile(r"\b(?:Section|Chapter|Table) ((?:sec:|ch:|tab:)[a-z][a-z0-9:-]*[a-z0-9])")
_CODEISH = re.compile(r"(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}|\[DONE\]|(?<![\w])--[a-z][a-z-]*|"
                      r"(?<![\w/])/[\w{}.<>/*?=&|-]*|\b[A-Za-z][\w.-]*\.(?:schema\.)?json\b|"
                      r"\b[a-z]+(?:_[a-z0-9]+)+(?:\.[a-z_]+)*\b|\b[A-Z]+(?:_[A-Z0-9]+)+\b|\bqse_\w+)")


def _ref(m: re.Match) -> str:
    text, href = LINKS[m.group(1)]
    return "\x00[" + text + "](" + href + ")\x00"


def fmt(text: str, code, esc) -> str:
    """Prose with identifiers, paths, flags and {json} set as code, and Section x as a link."""
    if not text:
        return ""
    parts = re.split("\x00", _REF.sub(_ref, text))
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(part)              # a link, already Markdown
            continue
        for j, piece in enumerate(_CODEISH.split(part)):
            out.append(code(piece) if j % 2 else esc(piece))
    return "".join(out)


def _route_rows(rows, code, esc, root: str = None) -> list:
    out = []
    authz = authz_routes(root) if root else None
    for method, path, who, what, body, answer in rows:
        if authz:
            who = who_for(authz[0], method, path)
        cell = fmt(what, code, esc)
        if body:
            cell += " *Body:* " + fmt(body, code, esc) + "."
        if answer:
            cell += " *Answer:* " + fmt(answer, code, esc) + "."
        out.append([code(method) + " " + code(path), esc(who), cell])
    return out


def gen_who(table, code, esc, root: str) -> str:
    routes, roles = authz_routes(root)
    used = []
    for r in routes:
        if r[3] not in used:
            used.append(r[3])
    rows = [[code(k), esc(_lowest(roles, k)), fmt(PERMS.get(k, ""), code, esc)] for k in used]
    return (table("Who may call a controller route: the permission proxy/authz.py names, and the lowest role that holds it",
                  ["permission", "held by", "what it covers"], rows)
            + "\n" + table("Who may call a route the extension serves inside an engine: the engine's own rule",
                           ["label", "who may call"],
                           [[code(k), fmt(v, code, esc)] for k, v in WHO.items() if k in {r[2] for r in EXT_ROUTES}]))


def gen_ext_routes(table, code, esc, root: str = None) -> str:
    return table("The routes the extension serves inside an engine", ["route", "who", "what"],
                 _route_rows(EXT_ROUTES, code, esc))


def gen_proxy_routes(table, code, esc, root: str) -> str:
    return (table("The controller's routes (`tandem-app`)", ["route", "permission", "what"], _route_rows(PROXY_ROUTES, code, esc, root))
            + "\n" + table("The host collector beside the controller", ["server", "route", "answer"],
                           [[code(server), code(method) + " " + code(path), fmt(what, code, esc)]
                            for server, method, path, what in SIDE_ROUTES]))


def gen_headers(table, code, esc) -> str:
    return table("Every header TandemApp reads or writes", ["header", "direction, where", "meaning"],
                 [[code(name), esc(direction + ", " + where), fmt(meaning, code, esc)]
                  for name, direction, where, meaning in HEADERS])


def gen_errors(table, code, esc, root: str) -> str:
    first = table("The errors explained", ["status", "error.type or code", "when"],
                  [[code(status) + (" with `Retry-After`" if retry else ""),
                    ", ".join(code(t) for t in types) if types else "none", fmt(when, code, esc)]
                   for status, types, when, retry in ERRORS])
    explained = {t for row in ERRORS for t in row[1]}
    by_type: dict = {}
    for status, t, msg, rel in error_sites(root):
        if t in explained:
            continue
        e = by_type.setdefault(t, {"status": set(), "msg": "", "files": set()})
        if status:
            e["status"].add(status)
        if msg and not e["msg"]:
            e["msg"] = msg
        e["files"].add(rel)
    rows = []
    for t in sorted(by_type):
        e = by_type[t]
        st = ", ".join(str(x) for x in sorted(e["status"])) or "in the body"
        files = sorted(e["files"])
        where = ", ".join(code(f) for f in files[:2]) + (f" and {len(files) - 2} more" if len(files) > 2 else "")
        rows.append([code(t), esc(st), esc(e["msg"]) or " ", where])
    second = table("Every other error type the code answers, with its status and the code's own message ({...} is a value)",
                   ["error.type or code", "status", "message", "where"], rows)
    return first + "\n" + second
