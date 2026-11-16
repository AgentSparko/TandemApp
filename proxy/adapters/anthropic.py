"""Any server that speaks the Anthropic Messages API, reached by a base URL and a key (SRV-877).

An OpenAI client's chat completion is translated to a Messages request and the answer back
(proxy/translate/anthropic.py has every rule and what is lost). The relay asks this adapter for
the upstream path (`<base>/v1/messages`, the base with or without its `/v1`), the request body
(`prepare`, which may refuse a field with a 400), the stream's translator, the whole answer's
translation, and whether a status may be tried again (529, overloaded, joins 429, 502 and 503).
The pipeline (hooks, harness, the ledger's reader) sees the OpenAI shape only.

HEADERS. The backend's key as `x-api-key` (or `Authorization: Bearer` with `auth: bearer`) and
`anthropic-version` (`api_version`, 2023-06-01 by default). None of the client's headers go on: the
client's own `Authorization` never reaches the server.

USAGE arrives in every answer unasked (`message_start`, `message_delta`), so no usage frame is
asked for. A client that asked for `stream_options.include_usage` gets the usage chunk; for any
other it is dropped like an injected one. The ledger reads the Messages usage itself:
`prompt_tokens` = input + cache read + cache write, `cached_tokens` = cache read,
`cache_write_tokens` = cache write, `reasoning_tokens` = the thinking tokens when counted; the
1-hour writes (`cache_creation.ephemeral_1h_input_tokens`) are priced at `cache_write_1h`.

PASS-THROUGH (SRV-881): a Messages API client's request (`/v1/messages`, its `count_tokens`) is
not translated: `prepare` sends the client's bytes, the model value set to the upstream id when
it differs (proxy/passthrough.py), and the answer goes back as sent; `read_frame` and `read_body`
read the Messages events and message for the ledger (usage, stop reason, tool calls, thinking,
an `error` event).

MODELS. `GET <base>/v1/models?limit=1000`, then each next page by `after_id` while `has_more`:
the id, `display_name`, `max_input_tokens`, `max_tokens`, image input and the effort levels its
`capabilities` say. A manual list works as for every kind.

SETTINGS (the backend's `anthropic` object; env `TANDEM_BACKEND_<ID>_ANTHROPIC_<FIELD>`): `auth`
(x-api-key | bearer), `api_version`, `sampling` (drop | pass), `default_max_tokens`, `auto_cache`,
`thinking_display` (summarized | omitted), `thinking_shape` (adaptive | budget | off).
"""

from __future__ import annotations

import json

from proxy.adapters.base import CHAT, PASS_HEADERS, PASS_PREFIXES, Adapter, _int, sse_data
from proxy.translate import anthropic as tr

MESSAGES = "/v1/messages"
COUNT = "/v1/messages/count_tokens"
NATIVE = (MESSAGES, COUNT)
RETRYABLE = (429, 502, 503, 529)
PASS = PASS_HEADERS + ("request-id",)
PREFIXES = PASS_PREFIXES + ("anthropic-ratelimit-",)
MODELS_PAGE = 1000


class Anthropic(Adapter):
    kind = "anthropic"
    cloud = True
    translates = True

    def opts(self) -> dict:
        return tr.options(self.backend)

    def routes(self) -> tuple[str, ...]:
        return (CHAT, MESSAGES, COUNT)

    def passes_through(self, path: str) -> bool:
        """SRV-881: a Messages API request on this path goes on as the client sent it."""
        return path in NATIVE

    def capabilities(self) -> dict:
        return {"continue_final_message": False, "choice": "text",
                "reasoning_field": "reasoning_content",
                "cache_counts": ["cache_read_input_tokens", "cache_creation_input_tokens"],
                "thinking_switch": "reasoning_effort"}

    def thinking(self, body: dict, on: bool, level: str | None = None,
                 model: str | None = None) -> tuple[dict, str]:
        """`reasoning_effort` in the OpenAI shape, which the translation turns into the
        backend's thinking shape. Current models cannot switch thinking off."""
        if body.get("reasoning_effort") is not None:
            return body, "client"
        if not on or self.opts().get("thinking_shape") == "off":
            return body, "unsupported"
        word = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"}.get(
            level or "medium")
        if word is None:
            return body, "unsupported"
        return dict(body, reasoning_effort=word), f"reasoning_effort={word}"

    # ----------------------------------------------------------------- the request
    def headers(self, key: str | None) -> list[tuple[str, str]]:
        o = self.opts()
        out = [("Content-Type", "application/json"),
               ("Accept", "application/json, text/event-stream"),
               ("anthropic-version", str(o.get("api_version") or tr.DEFAULTS["api_version"]))]
        if key:
            out.append(("Authorization", f"Bearer {key}") if o.get("auth") == "bearer"
                       else ("x-api-key", key))
        return out

    def forward(self, client_headers, key: str | None = None) -> list[tuple[str, str]]:
        return []

    def passes(self, name: str) -> bool:
        n = name.lower()
        return n in PASS or n.startswith(PREFIXES)

    def inject_usage(self) -> bool:
        return False

    def upstream_path(self, pool, route: str) -> str:
        if route == CHAT:
            route = MESSAGES
        prefix = pool.prefix or ""
        if prefix.endswith("/v1"):
            prefix = prefix[:-3]
        return prefix + route

    def retryable(self, status: int) -> bool:
        return status in RETRYABLE

    def dropped(self, body: dict) -> list[str]:
        return tr.dropped(body, self.opts())

    def prepare(self, path: str, raw: bytes, body: dict, upstream_model: str,
                max_output: int | None = None, stash=None) -> tuple[bytes, bool]:
        """(the Messages request, whether the usage chunk is the client's to drop). Raises
        tr.TranslateError for a field with no equal. A Messages API request passes through:
        the client's bytes, the model value set when it differs (SRV-881). `stash(messages)`:
        the thinking stash's restore of a translated history (proxy/stash.py)."""
        if path in NATIVE:
            if body.get("model") == upstream_model:
                return raw, False
            from proxy import passthrough
            return passthrough.set_model(raw, upstream_model), False
        out = tr.to_messages(body, upstream_model, self.opts(), max_output)
        if stash is not None:
            out["messages"] = stash(out["messages"])
        opts = body.get("stream_options")
        asked = isinstance(opts, dict) and opts.get("include_usage") is True
        return json.dumps(out, ensure_ascii=False).encode(), bool(out.get("stream")) and not asked

    # ----------------------------------------------------------------- the answer
    def stream_translator(self, created: int, keep=None):
        return tr.StreamToChat(created, keep)

    def answer(self, status: int, data: bytes, created: int, keep=None
               ) -> tuple[int, bytes, dict | None]:
        """A whole answer in the OpenAI shape: (status, bytes, the Messages usage). `keep`:
        the thinking stash's, given the answer's own content blocks (SRV-881)."""
        if status >= 400:
            st, body = tr.error_out(status, data)
            return st, body, None
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
        if not isinstance(obj, dict) or obj.get("type") == "error":
            st, body = tr.error_out(502 if not isinstance(obj, dict) else status, data)
            return st, body, None
        if keep is not None and status == 200 and isinstance(obj.get("content"), list):
            keep(obj["content"])
        out, usage = tr.from_message(obj, created)
        return status, json.dumps(out, ensure_ascii=False).encode(), usage

    # ----------------------------------------------------------------- pass-through (SRV-881)
    def read_frame(self, path: str, event: bytes, st, injected: bool) -> tuple[bool, bool]:
        if path not in NATIVE:
            return super().read_frame(path, event, st, injected)
        data = sse_data(event)
        try:
            obj = json.loads(data) if data else None
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            return False, False
        t = obj.get("type")
        if t == "message_start":
            m = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if st.response_id is None and isinstance(m.get("id"), str):
                st.response_id = m["id"][:80]
            st.model = st.model or m.get("model")
            if isinstance(m.get("usage"), dict):
                st.native = dict(m["usage"])
        elif t == "content_block_start":
            b = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
            if b.get("type") == "tool_use":
                st.tools.add(obj.get("index"))
                st.tool_name = b.get("name") if isinstance(b.get("name"), str) else None
            elif b.get("type") in ("thinking", "redacted_thinking"):
                st.reasoning = True
        elif t == "content_block_delta":
            d = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            kind = {"text_delta": "content", "thinking_delta": "reasoning",
                    "input_json_delta": "tool"}.get(d.get("type"))
            if kind is None:
                return False, False
            if kind == "tool":
                st.tool_chars += len(d.get("partial_json") or "")
            elif kind == "reasoning":
                st.reasoning = True
            st.kind = kind
            st.tokens_seen += 1
            return True, False
        elif t == "message_delta":
            u = obj.get("usage")
            if isinstance(u, dict):
                st.native = dict(st.native or {})
                st.native.update({k: v for k, v in u.items() if v is not None})
            stop = (obj.get("delta") or {}).get("stop_reason") \
                if isinstance(obj.get("delta"), dict) else None
            if stop:
                st.finish_reason = tr.FINISH.get(stop, "stop")
        elif t == "message_stop":
            st.done = True
        elif t == "error":
            st.error = obj.get("error") if isinstance(obj.get("error"), dict) else {"type": t}
            st.finish_reason = "error"
        return False, False

    def read_body(self, path: str, obj, st) -> None:
        if path not in NATIVE:
            return super().read_body(path, obj, st)
        if not isinstance(obj, dict) or obj.get("type") == "error":
            return
        if st.response_id is None and isinstance(obj.get("id"), str):
            st.response_id = obj["id"][:80]
        if isinstance(obj.get("usage"), dict):
            st.native = dict(obj["usage"])
        if obj.get("stop_reason"):
            st.finish_reason = tr.FINISH.get(obj["stop_reason"], "stop")
        for i, b in enumerate(obj.get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                st.tools.add(i)
            elif isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"):
                st.reasoning = True

    def read_usage(self, obj, path: str = CHAT) -> dict:
        if isinstance(obj, dict) and "prompt_tokens" not in obj and \
                ("input_tokens" in obj or "cache_read_input_tokens" in obj):
            parts = [_int(obj.get(k)) for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")]
            details = obj.get("output_tokens_details")
            out = {"prompt_tokens": None if parts[0] is None else sum(p or 0 for p in parts),
                   "completion_tokens": _int(obj.get("output_tokens")),
                   "cached_tokens": parts[1], "cache_write_tokens": parts[2],
                   "reasoning_tokens": _int(details.get("thinking_tokens"))
                   if isinstance(details, dict) else None}
            # SRV-881: the writes kept for 1 hour, priced apart (`cache_write_1h`); a sixth
            # field only the price reads, there when the server counts them
            cc = obj.get("cache_creation")
            w1h = _int(cc.get("ephemeral_1h_input_tokens")) if isinstance(cc, dict) else None
            if w1h is not None:
                out["cache_write_1h_tokens"] = w1h
            return out
        return super().read_usage(obj, path)

    # ----------------------------------------------------------------- discovery
    def models_query(self, after: str | None = None) -> str:
        q = f"?limit={MODELS_PAGE}"
        if after:
            from urllib.parse import quote
            q += "&after_id=" + quote(after, safe="")
        return q

    def next_page(self, doc) -> str | None:
        return tr.next_page(doc)

    def parse_models(self, obj) -> list[dict]:
        return tr.parse_models(obj)
