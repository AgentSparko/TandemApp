"""The Messages API in front of TandemApp (SRV-879): clients that speak it reach every backend, a
local engine included.

    POST /v1/messages              a message, whole or streamed
    POST /v1/messages/count_tokens the prompt's tokens, counted by the backend
    POST /v1/messages/...          404 not_found_error (batches are not served)

WHO MAY CALL: the rule of /v1/chat/completions (`Service.authorize`), with the key in `x-api-key`
or in `Authorization: Bearer`. Neither header goes on to a backend. `anthropic-version`
(accepted, never required) and `anthropic-beta` go on only when the request passes through
(SRV-881); a translated request goes without them. The key's hash names the client in the ledger as it does for the OpenAI routes.

THE PIPELINE sees the OpenAI chat shape (the design's 4.5): the request is translated
(proxy/translate/anthropic.py, cell 4) and goes through `Service.serve` as a chat completion, so
the route table, the deployments, the hooks, the harness, the relay and the ledger work as for
an OpenAI client. The ledger row's endpoint is `messages` (`count_tokens` for a count) and its
`api` column `anthropic` (schema 13, SRV-867; `rec.api` is `messages`). With TANDEM_AUTH=accounts
the gate has checked and removed the key before this handler runs; `Service.authorize` and the
row take the gate's principal (its token, its client id). A request whose route is all backends
of kind `anthropic` passes through instead (SRV-881, proxy/passthrough.py): no translation, the
client's bytes upstream and the backend's back; a route with any other backend in it is
translated, twice where it reaches a backend of kind `anthropic` (cell 4, then that adapter's
cell 3).

THE ANSWER. `Out` is the handler the pipeline writes to: what it writes in the OpenAI shape (a
completion, a stream, an error, a 401, a hook's refusal, the relay's own 503) reaches the client
in the Messages API's shape, with a `request-id` header on every answer. Status codes keep their
meaning; 503 (a drained node, a full backend, a busy server) is 529 `overloaded_error`, its
`Retry-After` kept. `X-Tandem-*` headers pass; `X-Tandem-Dropped` names the request's fields that
did not go on, from both translations.

THE STREAM. While nothing has gone to a streaming client for PING_S seconds (a long prefill, a
queue, a tool's long input), the head and `message_start` go out and then a `ping` every PING_S
seconds, so the client and every proxy between keep the connection. An error after that is an
`error` event. A client that leaves is seen by the relay as for an OpenAI client (the upstream
closed within a quarter second).

COUNT TOKENS. Exact or nothing (no estimate, ever): a backend of kind `anthropic` counts with its
own `count_tokens` (the request translated twice); a local backend counts with our engine's
tokenize route (`POST /v1/tokenize`, the model's chat template over the translated request); any
other backend, or one without the route, answers 404 `not_found_error`.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import uuid

from proxy import passthrough
from proxy import route as route_mod
from proxy.adapters.base import CHAT
from proxy.translate import anthropic as tr
from tandemapp import ledger as ledger_mod

MESSAGES = "/v1/messages"
COUNT = "/v1/messages/count_tokens"
PING_S = 15.0
COUNT_TIMEOUT_S = 60.0
# the client's headers the pipeline never sees: its credentials and the Messages API's own
STRIP = ("authorization", "x-api-key", "anthropic-version", "anthropic-beta")
# the answer headers a Messages API client gets besides ours
KEEP_PREFIXES = ("x-tandem-", "anthropic-ratelimit-")
COUNT_KEYS = ("model", "messages", "system", "tools", "tool_choice", "thinking")
AUTH_MESSAGE = ("an API key of this server is needed, in x-api-key or as Authorization: Bearer; "
                "the admin makes one under Backends, Users")


def is_messages(path: str) -> bool:
    return path == MESSAGES or path.startswith(MESSAGES + "/")


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


# ----------------------------------------------------------------------------- answers of ours
def refuse(h, status: int, message: str, rid: str | None = None, headers=(),
           etype: str | None = None) -> None:
    """One refusal in the Messages API's error shape, with its `request-id`."""
    rid = rid or new_id("req_")
    data = tr.messages_error(status, message, etype, rid)
    try:
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("request-id", rid)
        for k, v in headers:
            if k.lower() not in ("content-type", "content-length"):
                h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    except OSError:
        h.close_connection = True


def body_error(h, exc) -> None:
    """A body that could not be read (its framing, its size): the connection closes after."""
    h.close_connection = True
    refuse(h, exc.code, exc.message)


def _copy(headers, auth: bool):
    """The client's headers without STRIP; with `auth`, the key of `x-api-key` as the bearer
    `Service.authorize` and the ledger's client id read."""
    out = http.client.HTTPMessage()
    try:
        items = list(headers.items())
    except Exception:                                              # noqa: BLE001
        items = []
    key = None
    bearer = None
    for k, v in items:
        lk = k.lower()
        if lk == "x-api-key":
            key = v
        elif lk == "authorization":
            bearer = v
        if lk not in STRIP:
            out[k] = v
    if auth:
        if bearer:
            out["Authorization"] = bearer
        elif key:
            out["Authorization"] = f"Bearer {key.strip()}"
    return out


class _Sink:
    """A handler that writes nowhere: `Service.authorize` and `account_refusal` read the request
    through it; the refusal the client gets is ours, in its own shape."""

    def __init__(self, h, headers):
        self.headers = headers
        self.client_address = getattr(h, "client_address", None)
        self.connection = getattr(h, "connection", None)

        class _W:
            def write(self, data):
                return len(data)

            def flush(self):
                pass
        self.wfile = _W()

    def send_response(self, *a, **k):
        pass

    def send_header(self, *a, **k):
        pass

    def end_headers(self):
        pass


# ----------------------------------------------------------------------------- the route
def handle(h, svc, path: str, raw: bytes, t_arrival: float | None = None) -> None:
    """One request to /v1/messages or under it; the body is read (`raw`)."""
    rid = new_id("req_")
    if path not in (MESSAGES, COUNT):
        return refuse(h, 404, f"{path} is not served here", rid)
    view = _Sink(h, _copy(h.headers, auth=True))
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, RecursionError) as exc:
        svc.account_refusal(view, path, None, 400)       # a row, as the OpenAI routes write one
        return refuse(h, 400, f"the body is not JSON: {exc}", rid)
    if not isinstance(body, dict):
        svc.account_refusal(view, path, None, 400)
        return refuse(h, 400, "the body must be a JSON object", rid)
    ok, user = svc.authorize(view)
    if not ok:
        svc.account_refusal(view, path, body, 401, finish="refused", error_type="invalid_api_key")
        return refuse(h, 401, AUTH_MESSAGE, rid,
                      (("WWW-Authenticate", 'Bearer realm="TandemLLM"'),))
    client = ledger_mod.client_of(view.headers, None)
    from proxy import deployments

    def refused(exc):
        svc.account_refusal(view, path, body, exc.status, user=user, finish="refused",
                            error_type=exc.code)
        st, et, msg = tr.error_in(exc.status, json.dumps(exc.body()).encode())
        return refuse(h, st, msg, rid, exc.headers, et)
    try:
        native = _native_route(svc, body, h.headers)
    except deployments.Refused as exc:          # a model the token may not use (SRV-867)
        return refused(exc)
    if native is not None:
        return _pass(h, svc, path, raw, body, native, user, view, client, rid, t_arrival)
    plain = _copy(h.headers, auth=False)
    try:
        chat, dropped, meta = tr.from_messages(body, need_max=path == MESSAGES)
    except tr.TranslateError as exc:
        svc.account_refusal(view, path, body, 400, user=user, error_type="untranslatable")
        return refuse(h, 400, exc.message, rid)
    try:
        route = svc.resolve(chat, plain)
    except route_mod.NotFound as exc:
        svc.account_refusal(view, path, body, 404, user=user, error_type="model_not_found")
        return refuse(h, 404, exc.message, rid)
    except deployments.Refused as exc:
        return refused(exc)
    backend = svc.table.backend_for(route.backend)
    adapter = svc.adapter(backend) if backend is not None else None
    if backend is not None and backend.get("cloud") and "top_k" in chat:
        chat.pop("top_k")                    # an engine's field a cloud API refuses
        dropped = sorted(set(dropped) | {"top_k"})
    on, level = tr.thinking_of(body)
    if on is not None and adapter is not None:
        chat, _ = adapter.thinking(chat, on, level, route.model)
    if path == COUNT:
        return _count(h, svc, body, chat, route, backend, adapter, user, view, rid)
    out = Out(h, plain, bool(chat.get("stream")), new_id("msg_"), meta, rid, dropped)
    try:
        svc.serve(out, CHAT, json.dumps(chat, ensure_ascii=False).encode(), chat, route, user,
                  t_arrival=t_arrival, endpoint="messages", client=client)
    finally:
        out.end()


# ----------------------------------------------------------------------------- pass-through
def _native_route(svc, body: dict, headers):
    """SRV-881: the route when every candidate is a backend a Messages API request passes
    through to, else None (the request is translated, and its route resolved there). A model
    the request's token may not use raises deployments.Refused, as on the translated path."""
    from proxy import deployments
    model = body.get("model")
    if not isinstance(model, str) or not model:
        return None
    try:
        if not svc.all_native(svc.table.resolve(model)):
            return None
        r = svc.resolve(body, _creds_off(headers))
        return r if svc.all_native(r) else None
    except deployments.Refused as exc:
        if exc.code == "permission_denied":
            raise
        return None
    except Exception:                                              # noqa: BLE001
        return None


def _creds_off(headers):
    """The client's headers without its credentials; the Messages API's own stay."""
    out = http.client.HTTPMessage()
    try:
        items = list(headers.items())
    except Exception:                                              # noqa: BLE001
        items = []
    for k, v in items:
        if k.lower() not in ("authorization", "x-api-key"):
            out[k] = v
    return out


def _pass(h, svc, path, raw, body, route, user, view, client, rid, t_arrival) -> None:
    """A request that passes through: its bytes to the backend, the answer's back."""
    hdrs = _creds_off(h.headers)
    if path == COUNT:
        return _count_native(h, svc, raw, body, route, user, view, hdrs, rid)
    out = Native(h, hdrs, rid)
    try:
        svc.serve(out, MESSAGES, raw, body, route, user, t_arrival=t_arrival,
                  endpoint="messages", client=client, native=True)
    finally:
        out.end()


def _count_native(h, svc, raw, body, route, user, view, hdrs, rid) -> None:
    backend = svc.table.backend_for(route.backend)
    adapter = svc.adapter(backend)
    try:
        up = adapter.prepare(COUNT, raw, body, route.model)[0]
        key = svc.cfg.key_for(backend["id"])
    except Exception as exc:                                       # noqa: BLE001
        svc.account_refusal(view, COUNT, body, 400, user=user, error_type="count_tokens",
                            model=route.public)
        return refuse(h, 400, f"the request cannot go on: {type(exc).__name__}", rid)
    st, uh, data = _send_upstream(svc, backend, adapter, COUNT, up,
                                  passthrough.headers(key, hdrs, adapter.opts()))
    svc.account_refusal(view, COUNT, body, st or 502, user=user,
                        finish="stop" if st == 200 else "error",
                        error_type=None if st == 200 else "count_tokens", model=route.public)
    if st == 0:
        return refuse(h, 502, f"backend '{backend['id']}' does not answer", rid)
    got = {k.lower(): v for k, v in uh}
    try:
        h.send_response(st)
        h.send_header("Content-Type", got.get("content-type") or "application/json")
        h.send_header("request-id", got.get("request-id") or rid)
        for k, v in uh:
            if k.lower() == "retry-after" or k.lower().startswith(KEEP_PREFIXES):
                h.send_header(k, v)
        h.send_header("X-Tandem-Backend", route.backend)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    except OSError:
        h.close_connection = True


class Native:
    """The handler a request that passes through writes to: what the backend sent goes to the
    client as it is (a `request-id` added when it sent none); an answer of our own (a drained
    node, a timeout, a hook's refusal, the relay's 503) is an OpenAI-shaped error, which reaches
    the client in the Messages API's shape, 503 as 529."""

    def __init__(self, h, headers, rid: str):
        self._h = h
        self.headers = headers
        self.client_address = getattr(h, "client_address", None)
        self.connection = getattr(h, "connection", None)
        self._px_written = False
        self.rid = rid
        self.status = None
        self.hdrs: list = []
        self.mode = None                    # None | "raw" | "error" | "done"
        self.buf = bytearray()
        self.want = None
        self.wfile = _Writer(self)

    @property
    def close_connection(self):
        return getattr(self._h, "close_connection", False)

    @close_connection.setter
    def close_connection(self, v):
        self._h.close_connection = v

    def send_response(self, code, message=None) -> None:
        self.status, self.hdrs, self.mode = int(code), [], None

    def send_header(self, k, v) -> None:
        self.hdrs.append((str(k), str(v)))

    def end_headers(self) -> None:
        names = {k.lower() for k, _ in self.hdrs}
        if self.status < 400:
            h = self._h
            h.send_response(self.status)
            for k, v in self.hdrs:
                h.send_header(k, v)
            if "request-id" not in names:
                h.send_header("request-id", self.rid)
            h.end_headers()
            self.mode = "raw"
            return
        self.mode, self.buf = "error", bytearray()
        try:
            self.want = int({k.lower(): v for k, v in self.hdrs}.get("content-length"))
        except (TypeError, ValueError):
            self.want = None
        if self.want == 0:
            self._error()

    def _write(self, data: bytes) -> None:
        if self.mode == "raw":
            self._h.wfile.write(data)
            self._h.wfile.flush()
        elif self.mode == "error":
            self.buf += data
            if self.want is not None and len(self.buf) >= self.want:
                self._error()

    def _error(self) -> None:
        self.mode = "done"
        data, hdrs = bytes(self.buf), self.hdrs
        if _json(data).get("type") == "error":
            # the backend's own error: as it sent it
            h = self._h
            try:
                h.send_response(self.status)
                for k, v in hdrs:
                    if k.lower() != "content-length":
                        h.send_header(k, v)
                if "request-id" not in {k.lower() for k, _ in hdrs}:
                    h.send_header("request-id", self.rid)
                h.send_header("Content-Length", str(len(data)))
                h.end_headers()
                h.wfile.write(data)
            except OSError:
                h.close_connection = True
            return
        st, et, m = tr.error_in(self.status, data)
        keep = [(k, v) for k, v in hdrs if k.lower() == "retry-after"
                or k.lower().startswith(KEEP_PREFIXES)]
        refuse(self._h, st, m, self.rid, keep, et)

    def end(self) -> None:
        if self.mode == "error":
            self._error()


# ----------------------------------------------------------------------------- /v1/models
# SRV-881: one path for two protocols. A request with `anthropic-version`, or with `x-api-key` and
# no `Authorization`, gets the Messages API's shape: {data: [{type: "model", id, display_name,
# created_at, ...}], has_more, first_id, last_id}, paged by `limit` (20, 1 to 1000), `after_id`
# and `before_id`. The models are the OpenAI list's, under the same ids. `capabilities` has every
# leaf the API names; only what a card states is true (image input, thinking, effort levels).
MODEL_LIMIT = (20, 1, 1000)
EPOCH = "1970-01-01T00:00:00Z"


def wants_models(headers) -> bool:
    try:
        if headers.get("anthropic-version"):
            return True
        return bool(headers.get("x-api-key")) and not headers.get("Authorization")
    except Exception:                                              # noqa: BLE001
        return False


def _iso(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return EPOCH
    if ts > 1e11:                                        # milliseconds
        ts = ts / 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def _no(*names) -> dict:
    return {n: {"supported": False} for n in names}


def model_info(card: dict, first_seen=None) -> dict:
    """One OpenAI-shaped card of /v1/models as the Messages API's ModelInfo."""
    t = card.get("tandem") if isinstance(card.get("tandem"), dict) else {}
    caps = t.get("capabilities") if isinstance(t.get("capabilities"), dict) else {}
    levels = set(caps.get("effort_levels") or [])
    thinking = bool(caps.get("thinking"))
    ctx = t.get("context_length") or card.get("context_length") or card.get("max_model_len")
    mo = t.get("max_output")
    return {
        "type": "model", "id": card["id"],
        "display_name": t.get("name") or card.get("name") or card["id"],
        "created_at": _iso(first_seen if first_seen is not None else card.get("created")),
        "max_input_tokens": ctx if isinstance(ctx, int) and ctx > 0 else None,
        "max_tokens": mo if isinstance(mo, int) and mo > 0 else None,
        "lifecycle": "active", "deprecated_at": None, "retires_at": None, "line": None,
        "capabilities": {
            **_no("batch", "citations", "code_execution", "pdf_input", "structured_outputs"),
            "context_management": dict(_no("clear_thinking_20251015",
                                           "clear_tool_uses_20250919", "compact_20260112"),
                                       supported=False),
            "effort": dict({lv: {"supported": lv in levels}
                            for lv in ("low", "medium", "high", "xhigh", "max")},
                           supported=bool(levels)),
            "image_input": {"supported": bool(caps.get("vision"))},
            "server_tools": dict(_no("code_execution", "web_search"), supported=False),
            "thinking": {"supported": thinking,
                         "types": _no("adaptive", "between_tools", "disabled", "enabled")}}}


def _infos(svc) -> list[dict]:
    snap = svc.table.snapshot()["by_backend"]
    out = []
    for card in svc.models(int(svc.started)):
        t = card.get("tandem") if isinstance(card.get("tandem"), dict) else {}
        row = (snap.get(t.get("backend")) or {}).get(t.get("upstream_id")) or {}
        out.append(model_info(card, row.get("discovered_at")))
    return out


def models(h, svc, path: str) -> None:
    """GET /v1/models or /v1/models/{id} in the Messages API's shape."""
    from urllib.parse import parse_qs, unquote, urlsplit
    rid = new_id("req_")
    view = _Sink(h, _copy(h.headers, auth=True))
    ok, _ = svc.authorize(view)
    if not ok:
        return refuse(h, 401, AUTH_MESSAGE, rid,
                      (("WWW-Authenticate", 'Bearer realm="TandemLLM"'),))
    infos = _infos(svc)
    if path.startswith("/v1/models/"):
        name = unquote(path[len("/v1/models/"):])
        got = next((m for m in infos if m["id"] == name), None)
        if got is None:
            return refuse(h, 404, f"model: {name}", rid)
        return _send_json(h, got, rid)
    q = {k: v[-1] for k, v in parse_qs(urlsplit(h.path).query).items()}
    limit = MODEL_LIMIT[0]
    if "limit" in q:
        try:
            limit = int(q["limit"])
        except ValueError:
            limit = 0
        if not MODEL_LIMIT[1] <= limit <= MODEL_LIMIT[2]:
            return refuse(h, 400, "limit: a number from 1 to 1000", rid)
    ids = [m["id"] for m in infos]
    if q.get("after_id") is not None or q.get("before_id") is not None:
        which = "after_id" if q.get("after_id") is not None else "before_id"
        if q[which] not in ids:
            return refuse(h, 400, f"{which}: no model {q[which]!r} in this list", rid)
        i = ids.index(q[which])
        if which == "after_id":
            page, more = infos[i + 1:i + 1 + limit], i + 1 + limit < len(infos)
        else:
            page, more = infos[max(0, i - limit):i], i - limit > 0
    else:
        page, more = infos[:limit], limit < len(infos)
    return _send_json(h, {"data": page, "has_more": more,
                          "first_id": page[0]["id"] if page else None,
                          "last_id": page[-1]["id"] if page else None}, rid)


def _send_json(h, obj, rid: str) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode()
    try:
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("request-id", rid)
        h.send_header("X-Tandem-Gateway", "tandem-app")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    except OSError:
        h.close_connection = True


# ----------------------------------------------------------------------------- count_tokens
def _count(h, svc, body, chat, route, backend, adapter, user, view, rid) -> None:
    status, n, msg, etype = _counted(svc, chat, route, backend, adapter)
    svc.account_refusal(view, COUNT, body, status, user=user,
                        finish="stop" if status == 200 else "error",
                        error_type=None if status == 200 else "count_tokens", model=route.public)
    if status != 200:
        return refuse(h, status, msg, rid, etype=etype)
    data = json.dumps({"input_tokens": n}).encode()
    try:
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("request-id", rid)
        h.send_header("X-Tandem-Backend", route.backend)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
    except OSError:
        h.close_connection = True


def _counted(svc, chat, route, backend, adapter) -> tuple[int, int | None, str, str | None]:
    """(status, the count, a refusal's message, its type)."""
    if backend is None or adapter is None:
        return 404, None, f"no backend serves {route.public}", None
    bid = backend["id"]
    if adapter.translates:
        try:
            req = tr.to_messages(dict(chat, max_tokens=1, stream=False), route.model,
                                 adapter.opts())
        except tr.TranslateError as exc:
            return 400, None, exc.message, None
        req = {k: v for k, v in req.items() if k in COUNT_KEYS}
        try:
            hdrs = adapter.headers(svc.cfg.key_for(bid))
        except Exception:                                          # noqa: BLE001
            return 502, None, f"backend '{bid}': its key cannot be read", None
        st, _, raw = _send_upstream(svc, backend, adapter, COUNT,
                                    json.dumps(req, ensure_ascii=False).encode(), hdrs)
        if st == 200:
            n = _json(raw).get("input_tokens")
            if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
                return 200, n, "", None
            return 502, None, f"backend '{bid}' answered count_tokens without input_tokens", None
        if st == 0:
            return 502, None, f"backend '{bid}' does not answer", None
        err = _json(raw).get("error")
        err = err if isinstance(err, dict) else {}
        return st, None, str(err.get("message") or f"backend '{bid}' answered HTTP {st}"), \
            err.get("type") if isinstance(err.get("type"), str) else None
    if backend.get("cloud"):
        return 404, None, (f"backend '{bid}' ({backend['kind']}) cannot count tokens, and this "
                           f"server gives no estimate"), None
    from proxy import chatengine
    q = {"model": route.model, "messages": chat["messages"]}
    for k in ("tools", "chat_template_kwargs"):
        if chat.get(k):
            q[k] = chat[k]
    st, raw = chatengine.call(svc, backend, "POST", "/v1/tokenize",
                              json.dumps(q, ensure_ascii=False).encode(), COUNT_TIMEOUT_S)
    n = _json(raw).get("tokens") if st == 200 else None
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        return 200, n, "", None
    if st == 0:
        return 502, None, f"backend '{bid}' does not answer", None
    return 404, None, (f"backend '{bid}' cannot count tokens (it has no tokenize route), and "
                       f"this server gives no estimate"), None


def _send_upstream(svc, backend, adapter, path: str, data: bytes, headers
                   ) -> tuple[int, list, bytes]:
    """One request to a backend through its pool: (status, headers, body); (0, [], b"") when
    it does not answer."""
    to = float((backend.get("timeouts") or {}).get("first_byte") or COUNT_TIMEOUT_S)
    try:
        pool = svc.relay.pool(backend)
        conn = pool.get()
        try:
            conn.send("POST", adapter.upstream_path(pool, path), headers, data)
            st, _, uh = conn.read_head(to)
            raw = conn.read_all(to)
            pool.put(conn)
        except Exception:
            conn.close()
            raise
        return st, list(uh), raw
    except Exception:                                              # noqa: BLE001
        return 0, [], b""


def _json(raw) -> dict:
    try:
        v = json.loads(raw or b"null")
    except (ValueError, RecursionError):
        return {}
    return v if isinstance(v, dict) else {}


# ----------------------------------------------------------------------------- the answer
class _Writer:
    def __init__(self, out: "Out"):
        self.out = out

    def write(self, data) -> int:
        self.out._write(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass


class Out:
    """The handler the pipeline writes to (see THE ANSWER). One lock orders the pipeline's thread
    and the ping thread: once the head went out, whatever comes later is an event."""

    def __init__(self, h, headers, stream: bool, mid: str, meta: dict, rid: str, dropped):
        self._h = h
        self.headers = headers
        self.client_address = getattr(h, "client_address", None)
        self.connection = getattr(h, "connection", None)
        self._px_written = False
        self.stream = stream
        self.rid = rid
        self.dropped = list(dropped)
        self.conv = tr.ChatToMessages(mid, meta)
        self.mid, self.meta = mid, meta
        self.lock = threading.RLock()
        self.status = None
        self.hdrs: list = []
        self.mode = None                    # None | "stream" | "whole" | "done"
        self.buf = bytearray()
        self.want = None
        self.head = False                   # the client's head went out
        self.ended = False
        self.gone = False
        self.last = time.monotonic()
        self.wfile = _Writer(self)
        self.pinger = None
        if stream:
            self.pinger = threading.Thread(target=self._ping, daemon=True,
                                           name="messages-ping")
            self.pinger.start()

    @property
    def close_connection(self):
        return getattr(self._h, "close_connection", False)

    @close_connection.setter
    def close_connection(self, v):
        self._h.close_connection = v

    # the handler's interface, as the pipeline calls it
    def send_response(self, code, message=None) -> None:
        with self.lock:
            self.status = int(code)
            self.hdrs = []
            self.mode = None

    def send_header(self, k, v) -> None:
        with self.lock:
            self.hdrs.append((str(k), str(v)))

    def end_headers(self) -> None:
        with self.lock:
            hmap = {k.lower(): v for k, v in self.hdrs}
            if self.status == 200 and "event-stream" in hmap.get("content-type", ""):
                self.mode = "stream"
                self.sbuf = bytearray()
                self._head(self.hdrs)
                self._send(self.conv.start())
                return
            self.mode = "whole"
            self.buf = bytearray()
            try:
                self.want = int(hmap.get("content-length"))
            except (TypeError, ValueError):
                self.want = None
            if self.want == 0:
                self._whole()

    def _write(self, data: bytes) -> None:
        with self.lock:
            if self.mode == "stream":
                self.sbuf += data
                out = b""
                while True:
                    a, b = self.sbuf.find(b"\n\n"), self.sbuf.find(b"\r\n\r\n")
                    if a < 0 and b < 0:
                        break
                    end, sep = (b, 4) if b >= 0 and (a < 0 or b < a) else (a, 2)
                    out += self.conv.feed(bytes(self.sbuf[:end]))
                    del self.sbuf[:end + sep]
                if out:
                    self._send(out)
            elif self.mode == "whole":
                self.buf += data
                if self.want is not None and len(self.buf) >= self.want:
                    self._whole()

    # what goes to the client
    def _kept(self, hdrs) -> list:
        out, names = [], list(self.dropped)
        for k, v in hdrs:
            lk = k.lower()
            if lk == "x-tandem-dropped":
                names += [n.strip() for n in v.split(",") if n.strip()]
            elif lk == "retry-after" or lk.startswith(KEEP_PREFIXES):
                out.append((k, v))
        if names:
            out.append(("X-Tandem-Dropped", ",".join(dict.fromkeys(names))))
        return out

    def _head(self, hdrs) -> None:
        if self.head:
            return
        self.head = True
        h = self._h
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        h.send_header("request-id", self.rid)
        for k, v in self._kept(hdrs):
            h.send_header(k, v)
        h.end_headers()
        h.close_connection = True

    def _plain(self, status: int, hdrs, data: bytes) -> None:
        self.head = True
        h = self._h
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("request-id", self.rid)
        for k, v in self._kept(hdrs):
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        self._send(data)

    def _send(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._h.wfile.write(data)
            self._h.wfile.flush()
        except OSError:
            self.gone = True
            raise
        self.last = time.monotonic()

    def _whole(self) -> None:
        """A whole answer of the pipeline (a completion or an error) for the client."""
        status, data, hdrs = self.status, bytes(self.buf), self.hdrs
        self.mode = "done"
        msg = None
        if status == 200:
            obj = _json(data)
            if isinstance(obj.get("choices"), list):
                msg = tr.to_message(obj, self.mid, self.meta)
            else:
                status = 502
                data = json.dumps({"error": {"message": "the backend answered without a chat "
                                                        "completion"}}).encode()
        if msg is not None:
            if self.stream:
                started = self.conv.started
                self._head(hdrs)
                self.conv.started = self.conv.done = True
                self._send(tr.message_events(msg, started=started))
            else:
                self._plain(200, hdrs, json.dumps(msg, ensure_ascii=False).encode())
            return
        st, et, m = tr.error_in(status or 502, data)
        if self.head:
            self._send(self.conv.fail(et, m))
        else:
            self._plain(st, hdrs, tr.messages_error(st, m, et, self.rid))

    def end(self) -> None:
        """After the pipeline returned: what it left open is closed for the client."""
        with self.lock:
            self.ended = True
            try:
                if self.mode == "whole":
                    self._whole()            # a body shorter than its length, or without one
                elif self.mode == "stream" and not self.gone:
                    if self.sbuf.strip():
                        self._send(self.conv.feed(bytes(self.sbuf)))
                    if self.conv.finish_reason is not None:
                        # a finish without `[DONE]`: whole, as the relay reads it too
                        self._send(self.conv.finish())
                    else:
                        self._send(self.conv.fail("api_error", "the stream ended early"))
                elif self.mode is None and self.head and not self.gone:
                    self._send(self.conv.fail("api_error", "the request ended without an answer"))
            except OSError:
                pass

    def _ping(self) -> None:
        while True:
            time.sleep(max(0.01, min(0.5, PING_S / 4)))
            with self.lock:
                if self.ended or self.gone or self.conv.done or self.mode in ("whole", "done"):
                    return
                if time.monotonic() - self.last < PING_S:
                    continue
                try:
                    if not self.head:
                        self._head([])
                        self._send(self.conv.start())
                    self._send(tr.PING)
                except OSError:
                    return
