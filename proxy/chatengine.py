"""The engine chatd sees when the controller runs it (SRV-843): the controller's gateway as a whole
engine API, under one prefix.

    QSE_CHAT_ENGINE = <the controller's own URL>/chat-engine        (proxy/appchat.py sets it)

chatd (chatd/engine.py) calls one engine base for everything: the completions, the model list,
the facts it sizes a chat by, the live rows, the cache routes and the skills. On an engine host
that base was the engine itself. With chatd on the controller's host (the plan's C2) it is this
prefix, so chat traffic passes the controller's routes, keys, hooks and ledger, and the rest still
reaches an engine:

    POST /chat-engine/v1/chat/completions (and the other OpenAI routes)
                                   the gateway, exactly as /v1/chat/completions: the route
                                   table, the deployments, the backends' keys, the ledger row
    GET  /chat-engine/v1/models[/{id}]
                                   this controller's list, as /v1/models
    GET  /chat-engine/v1/library[/...], * /chat-engine/v1/conductor/...
                                   this controller's conductor (its library and its packages
                                   document: a user skill goes there as a body, chatd/skills.py)
    GET  /chat-engine/health       the chat engine's own /health (its `max_len`, `parallel.n`:
                                   chatd's run slots follow its lanes), else {"status": "ok"}
    GET  /chat-engine/v1/dashboard/system
                                   the chat engine's facts (weights, max_len, drafter, memory)
    GET  /chat-engine/v1/dashboard/live[?follow=0]
                                   the chat engine's live rows, its stream passed through
    POST /chat-engine/v1/cache/peek, /prewarm
                                   with a `model`: the member that holds the conversation
                                   (SRV-741, deployments.cache_route); without: the chat engine
    POST /chat-engine/v1/cache/release
                                   the chat engine's, when it runs on this host (a sandbox here
                                   shares its memory); an engine on another host gives nothing
                                   back for a sandbox here: 200 with `released_bytes` 0
    POST /chat-engine/v1/tokenize  the engine the body's model routes to
    GET  /chat-engine/v1/plugins/tools
                                   the running plugins' tools (SRV-859, proxy/pluginhost.py):
                                   chatd lists them with its MCP servers
    POST /chat-engine/v1/plugins/tools/call {plugin, tool, arguments}
                                   one call of a plugin's tool -> {result} (MCP's tools/call
                                   answer); arguments outside the tool's schema are refused

THE CHAT ENGINE is the TandemLLM backend whose facts chatd reads: TANDEM_CHAT_ENGINE_BACKEND when it
names an enabled one, else the default backend when it is a TandemLLM engine, else the first
enabled TandemLLM backend; none: the facts answer as a server without them does (chatd then runs
FALLBACK_RUNS at once and shows fewer figures). Every call to it carries ConfigDB.key_for's bearer:
the backend's key, or the HTTP token of the engine host it runs on (SRV-839, engine API 1.9).

ACCESS. The OpenAI routes, the model list, the cache routes, tokenize and the plugins' tools
follow the completions' rule (Service.authorize: a key of this controller's users once it has
any, else open; this machine's own calls pass). The facts and the live rows pass this machine's own calls, the
dashboard's rule or a user's key; /health without one says only {"status"}. Anything else under
the prefix is 404.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse

from proxy import canary as canary_mod
from proxy import config as config_mod
from tandemapp import http_body

PREFIX = "/chat-engine"
#: what the controller's own routes answer once the prefix is off
PASS_GET = ("/v1/models", "/v1/library")
PASS_PREFIX = ("/v1/models/", "/v1/library/", "/v1/conductor/")
CACHE = ("/v1/cache/peek", "/v1/cache/prewarm", "/v1/cache/release")
#: how long a call to the chat engine may take (connect and answer), per path
TIMEOUTS = {"/health": 3.0, "/v1/dashboard/system": 5.0, "/v1/dashboard/live": 5.0,
            "/v1/cache/peek": 5.0, "/v1/cache/prewarm": 5.0, "/v1/cache/release": 30.0,
            "/v1/tokenize": 60.0}
LIVE_CONNECT_S = 10.0
ENV_BACKEND = "TANDEM_CHAT_ENGINE_BACKEND"


def strip(path: str) -> str | None:
    """`path` without the prefix (`/` for the prefix alone), or None when it is not under it."""
    if path == PREFIX or path.startswith(PREFIX + "/") or path.startswith(PREFIX + "?"):
        rest = path[len(PREFIX):]
        return rest if rest.startswith("/") else "/" + rest
    return None


def passes(method: str, path: str) -> bool:
    """Whether the controller's own route answers `path` (the prefix off) as it is."""
    from proxy.service import PROXY_PATHS
    if method == "GET":
        return path in PASS_GET or path.startswith(PASS_PREFIX)
    if method == "POST":
        return path in PROXY_PATHS or path.startswith("/v1/conductor/")
    return path.startswith("/v1/conductor/")


def facts_backend(svc, env=None) -> dict | None:
    """The chat engine (THE CHAT ENGINE above), or None."""
    env = os.environ if env is None else env
    cfg = svc.cfg
    want = (env.get(ENV_BACKEND) or "").strip()
    cands = [want] if want else []
    if cfg.default_backend:
        cands.append(cfg.default_backend)
    for bid in cands:
        b = cfg.backend(bid)
        if b is not None and b["kind"] == "tandemllm" and b["enabled"]:
            return b
    for b in cfg.backends():
        if b["kind"] == "tandemllm" and b["enabled"]:
            return b
    return None


def engine_here(svc, env=None) -> bool | None:
    """True when the chat engine runs on this host (a loopback URL), False when on another, None
    when there is none."""
    b = facts_backend(svc, env)
    if b is None:
        return None
    return config_mod.is_loopback_host(config_mod.url_host(b["url"]) or "")


def _conn(url: str, timeout: float):
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    target = u.path + (f"?{u.query}" if u.query else "")
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout), \
        target or "/"


def _headers(svc, b: dict, extra: dict | None = None) -> dict:
    h = {"User-Agent": "tandem-app/chat-engine", "Accept": "application/json"}
    try:
        key = svc.cfg.key_for(b["id"])
    except Exception:                                              # noqa: BLE001
        key = None
    if key:
        h["Authorization"] = f"Bearer {key}"
    h.update(extra or {})
    return h


def call(svc, b: dict, method: str, path: str, body: bytes | None = None,
         timeout: float | None = None) -> tuple[int, bytes]:
    """One call to an engine route of backend `b`: (status, body); (0, b"") when it does not
    answer."""
    conn, target = _conn(canary_mod.engine_url(b["url"], path), timeout or TIMEOUTS.get(
        path.split("?")[0], 10.0))
    try:
        h = _headers(svc, b, {"Content-Type": "application/json"} if body is not None else None)
        conn.request(method, target, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read(16 * 1024 * 1024)
    except (OSError, http.client.HTTPException):
        return 0, b""
    finally:
        conn.close()


def _facts_ok(handler, state) -> bool:
    """The dashboard's rule (`read`), chatd's identity (svc:chatd), or an API token that may use
    the API (SEC-28: the gate's principal, never the peer's address)."""
    a = state.get("auth")
    if a is None:
        return False
    if a.decide("dashboard", handler._peer(), handler.headers) == "ok":
        return True
    from proxy import authz
    p = getattr(handler, "_principal", None)
    return any(authz.allow(p, x, a.policy) for x in ("chat-engine", "inference"))


def _pass(handler, st: int, raw: bytes) -> None:
    if st == 0:
        return handler._json(502, {"error": {"type": "upstream_error",
                                             "message": "the chat engine does not answer"}})
    try:
        json.loads(raw or b"null")
    except ValueError:
        return handler._json(502, {"error": {"type": "upstream_error", "message":
                                             f"the chat engine answered HTTP {st} without JSON"}})
    return handler._raw_json(st, raw, (("Cache-Control", "no-store"),))


def handle(handler, state: dict, method: str, path: str, query: str) -> None:
    """A route under the prefix that the controller's own routes do not answer as they are."""
    svc = state["svc"]
    b = facts_backend(svc)
    if method == "GET" and path in ("/health", "/healthz", "/v1/health"):
        if b is not None and _facts_ok(handler, state):
            st, raw = call(svc, b, "GET", "/health")
            if st == 200:
                return _pass(handler, st, raw)
        return handler._json(200, {"status": "ok"})
    if method == "GET" and path in ("/v1/dashboard/system", "/v1/dashboard/live"):
        if not _facts_ok(handler, state):
            return handler._json(401, {"error": {"type": "unauthorized", "message":
                                                 "the engine's facts need a sign-in or an "
                                                 "API token"}})
        if b is None:
            return handler._json(404, {"error": {"type": "not_found", "message":
                                                 "no TandemLLM engine behind this controller"}})
        q = urllib.parse.parse_qs(query)
        if path == "/v1/dashboard/live" and (q.get("follow") or ["1"])[-1] not in ("0", "false",
                                                                                   "no"):
            return _live(handler, svc, b)
        st, raw = call(svc, b, "GET", path + (f"?{query}" if query else ""))
        return _pass(handler, st, raw)
    if method == "POST" and (path in CACHE or path == "/v1/tokenize"):
        try:
            raw = handler._read_raw()            # read whole first: the connection stays in step
        except http_body.BodyError as exc:
            return handler._body_error(exc)
        ok, _user = svc.authorize(handler)
        if not ok:
            return None
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, RecursionError):
            return handler._json(400, {"error": {"type": "invalid_request_error",
                                                 "message": "the body is not JSON"}})
        if not isinstance(body, dict):
            return handler._json(400, {"error": {"type": "invalid_request_error",
                                                 "message": "the body is a JSON object"}})
        return _post(handler, svc, b, path, body)
    if path in ("/v1/plugins/tools", "/v1/plugins/tools/call"):
        return _plugin_tools(handler, svc, method, path)
    if method != "GET":
        handler.close_connection = True         # a body this route never read ends the connection
    return handler._json(404, {"error": {"type": "not_found",
                                         "message": f"no route {method} {PREFIX}{path}"}})


def _plugin_tools(handler, svc, method: str, path: str) -> None:
    """SRV-859: the running plugins' tools for chatd, and one call of one."""
    from proxy.pluginstore import PluginError
    host = getattr(svc, "plugin_host", None)
    raw = b""
    if method == "POST":
        try:
            raw = handler._read_raw()
        except http_body.BodyError as exc:
            return handler._body_error(exc)
    ok, _user = svc.authorize(handler)
    if not ok:
        return None
    if path == "/v1/plugins/tools":
        if method != "GET":
            return handler._json(405, {"error": {"type": "method_not_allowed",
                                                 "message": "GET only"}})
        return handler._json(200, {"plugins": host.tools_view() if host is not None else []})
    if method != "POST":
        return handler._json(405, {"error": {"type": "method_not_allowed",
                                             "message": "POST only"}})
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict) or set(body) - {"plugin", "tool", "arguments"} or \
            not isinstance(body.get("plugin"), str) or not isinstance(body.get("tool"), str):
        return handler._json(400, {"error": {"type": "bad_request", "message":
                                             "{plugin, tool, arguments}"}})
    if host is None:
        return handler._json(503, {"error": {"type": "plugin_down",
                                             "message": "no plugin host here"}})
    try:
        res = host.tool_call(body["plugin"], body["tool"], body.get("arguments") or {})
    except PluginError as exc:
        return handler._json(exc.status, exc.body())
    return handler._json(200, {"result": res})


def _post(handler, svc, b: dict | None, path: str, body: dict) -> None:
    from proxy import deployments
    from proxy import route as route_mod
    if path == "/v1/cache/release":
        if b is None or not config_mod.is_loopback_host(config_mod.url_host(b["url"]) or ""):
            # a sandbox on this host gains nothing from an engine on another host
            return handler._json(200, {"released_bytes": 0, "levels": {},
                                       "skipped": "no_engine_here"})
        st, raw = call(svc, b, "POST", path, json.dumps(body).encode())
        return _pass(handler, st, raw)
    target = b
    if body.get("model"):
        if path in CACHE and svc.cache_route is not None:
            try:
                st, doc, hdrs = svc.cache_route(path, body, handler.headers)
            except route_mod.NotFound as exc:
                return handler._json(404, exc.body())
            except deployments.Refused as exc:
                return handler._json(exc.status, exc.body(), extra_headers=exc.headers)
            return handler._json(st, doc, extra_headers=hdrs)
        try:
            r = svc.resolve(body, handler.headers)
            target = svc.cfg.backend(r.backend) or b
            body = dict(body, model=r.model)
        except (route_mod.NotFound, deployments.Refused):
            pass
    if target is None:
        return handler._json(404, {"error": {"type": "not_found",
                                             "message": "no TandemLLM engine behind this "
                                                        "controller"}})
    st, raw = call(svc, target, "POST", path, json.dumps(body).encode())
    return _pass(handler, st, raw)


def _live(handler, svc, b: dict) -> None:
    """The chat engine's live stream, passed through as it arrives; its refusal (a 429 when it
    has as many live streams as it takes) as it is, so chatd polls instead."""
    conn, target = _conn(canary_mod.engine_url(b["url"], "/v1/dashboard/live"), LIVE_CONNECT_S)
    try:
        try:
            conn.request("GET", target, headers=_headers(svc, b, {"Accept": "text/event-stream"}))
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            return handler._json(502, {"error": {"type": "upstream_error",
                                                 "message": "the chat engine does not answer"}})
        if resp.status != 200:
            return _pass(handler, resp.status, resp.read(1024 * 1024))
        if conn.sock is not None:
            conn.sock.settimeout(60.0)               # the engine pings every 15 s
        handler._sse_head()
        while True:
            try:
                chunk = resp.read1(65536)
            except (OSError, http.client.HTTPException):
                break
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                handler.close_connection = True
                break
    finally:
        conn.close()
