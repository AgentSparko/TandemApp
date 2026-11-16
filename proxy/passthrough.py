"""A Messages API client in front of a Messages API backend: the bytes pass (SRV-881, cell 2 of
the design's matrix). No I/O here: proxy/messages.py picks the mode, the relay and the backend's
adapter (proxy/adapters/anthropic.py) carry it out.

WHEN. Every candidate of the request's route (the backend and its fallbacks) is a backend of kind
`anthropic`. A route with any other backend in it is translated as before (proxy/messages.py,
cell 4), so a fallback never changes the protocol half way.

THE REQUEST goes upstream as the client sent it, byte for byte. Two things change: the client's
key is replaced by the backend's (`x-api-key`, or a bearer with `auth: bearer`), and the model
value when the public id differs from the upstream one (`set_model`: only that value's bytes;
key order, spacing and escapes stay). Of the client's headers only `content-type`,
`anthropic-version` (the backend's `api_version` when the client sends none) and
`anthropic-beta` go on (`headers`).

THE ANSWER, its stream and an upstream error go back as the backend sent them. The relay reads
the Messages events beside them for the ledger (the adapter's `read_frame` and `read_body`).

THE HOOKS (hooks-v1 1.1, `protocol: {inbound: "messages", upstream: "messages"}`). With no hook
enabled nothing here runs: no translation code at all. With hooks, every stage sees a canonical
copy in the chat shape (`HookView`); guards, deciders and observers work as for any request. A
transform's patch of a parameter is written into the native body (`PARAMS`); a patch of a
content field (`messages`, `tools`, ...), of an answer, or of a parameter that has no field in
the Messages API is refused: journaled `refused` with `protocol_native`, and the hook's failure
policy applies (closed: 503 `hook_unavailable`). A hook that moves the request to a backend of
another kind (a new model, route.select) is refused the same way (409 `protocol_native`); a
request.error fallback of another kind is not tried (proxy/relay.py), and the backend's own
failure stands.
"""

from __future__ import annotations

import copy
import json
import time

from proxy.hooks import ProtocolNative, merge_patch
from proxy.translate import anthropic as tr

# the client's headers that go on, besides the backend's key
FORWARD = ("content-type", "anthropic-version", "anthropic-beta")
# chat-shape fields a hook may not patch on a request that passes through
CONTENT = ("messages", "prompt", "input", "instructions", "tools")
# the parameters of the chat shape with a field of their own in the Messages API
PARAMS = ("model", "max_tokens", "max_completion_tokens", "max_output_tokens", "temperature",
          "top_p", "top_k", "stop", "metadata", "tool_choice", "parallel_tool_calls",
          "reasoning_effort", "response_format")
_CANON_KEEP = ("model", "max_tokens", "stream", "temperature", "top_p", "top_k", "metadata")


def native(adapter, path: str) -> bool:
    return bool(getattr(adapter, "passes_through", None)) and adapter.passes_through(path)


# ----------------------------------------------------------------------------- the bytes
def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def set_model(raw: bytes, model: str) -> bytes:
    """`raw` (a JSON object) with the value of its top-level `model` key set to `model`, every
    other byte as it was. Raises ValueError when `raw` is not a JSON object."""
    s = raw.decode("utf-8")
    dec = json.JSONDecoder()
    i = _skip_ws(s, 0)
    if i >= len(s) or s[i] != "{":
        raise ValueError("not a JSON object")
    i = _skip_ws(s, i + 1)
    spans = []
    if i < len(s) and s[i] == "}":
        return raw
    while True:
        if s[i] != '"':
            raise ValueError("a key is not a string")
        key, i = json.decoder.scanstring(s, i + 1)
        i = _skip_ws(s, i)
        if s[i] != ":":
            raise ValueError("no colon")
        i = _skip_ws(s, i + 1)
        _, end = dec.raw_decode(s, i)
        if key == "model":
            spans.append((i, end))
        i = _skip_ws(s, end)
        if s[i] == "}":
            break
        if s[i] != ",":
            raise ValueError("no comma")
        i = _skip_ws(s, i + 1)
    if not spans:
        return raw
    val = json.dumps(model, ensure_ascii=False)
    for a, b in reversed(spans):
        s = s[:a] + val + s[b:]
    return s.encode("utf-8")


def headers(key: str | None, client_headers, opts: dict) -> list[tuple[str, str]]:
    """The upstream headers of a request that passes through: the client's FORWARD ones, the
    version the backend's when the client sent none, and the backend's key in place of the
    client's."""
    out, seen = [], set()
    try:
        items = list(client_headers.items())
    except Exception:                                              # noqa: BLE001
        items = []
    for k, v in items:
        lk = k.lower()
        if lk in FORWARD:
            out.append((k, v))
            seen.add(lk)
    if "content-type" not in seen:
        out.insert(0, ("Content-Type", "application/json"))
    if "anthropic-version" not in seen:
        out.append(("anthropic-version",
                    str(opts.get("api_version") or tr.DEFAULTS["api_version"])))
    if key:
        out.append(("Authorization", f"Bearer {key}") if opts.get("auth") == "bearer"
                   else ("x-api-key", key))
    return out


# ----------------------------------------------------------------------------- the hooks
def canonical(body: dict) -> dict:
    """The request in the chat shape, for the hooks. A request the chat shape cannot hold (a
    server tool, a document) is shown with its parameters and no messages."""
    try:
        chat, _, _ = tr.from_messages(copy.deepcopy(body), need_max=False)
    except tr.TranslateError:
        out = {k: copy.deepcopy(body[k]) for k in _CANON_KEEP if k in body}
        out["messages"] = []
        return out
    oc = body.get("output_config")
    if isinstance(oc, dict) and isinstance(oc.get("effort"), str):
        chat["reasoning_effort"] = oc["effort"]
    return chat


def check(patch: dict) -> None:
    """ProtocolNative unless every field of `patch` has a native field."""
    for k in patch:
        if k in CONTENT:
            raise ProtocolNative(f"{k} is content; a request that passes through keeps the "
                                 f"client's")
        if k not in PARAMS:
            raise ProtocolNative(f"{k} has no field of its own in the Messages API")
        if k == "response_format" and patch[k] is not None and not (
                isinstance(patch[k], dict) and patch[k].get("type") == "json_schema"):
            raise ProtocolNative("response_format: only json_schema has a Messages API field")
        if k == "reasoning_effort" and patch[k] is not None and patch[k] not in tr.EFFORT:
            raise ProtocolNative("reasoning_effort: not a level")
        if k == "tool_choice" and patch[k] is not None:
            try:
                tr._tool_choice(patch[k], None)
            except tr.TranslateError as exc:
                raise ProtocolNative(exc.message)


def _native_fields(p: dict, body: dict) -> dict:
    """One chat-shape patch (checked) as a merge patch of the native body. `tool_choice` and
    `parallel_tool_calls` make one `tool_choice`, `reasoning_effort` and `response_format` one
    `output_config`, whatever their order in the patch."""
    out: dict = {}
    for k, v in p.items():
        if k in ("model", "temperature", "top_p", "top_k", "metadata"):
            out[k] = v
        elif k in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            out["max_tokens"] = v
        elif k == "stop":
            out["stop_sequences"] = None if v is None else ([v] if isinstance(v, str) else v)
    if "tool_choice" in p or "parallel_tool_calls" in p:
        tc = tr._tool_choice(p["tool_choice"], None) if p.get("tool_choice") is not None \
            else (None if "tool_choice" in p else body.get("tool_choice"))
        if "parallel_tool_calls" in p:
            tc = dict(tc or {"type": "auto"})
            if p["parallel_tool_calls"] is False:
                tc["disable_parallel_tool_use"] = True
            else:
                tc.pop("disable_parallel_tool_use", None)
        out["tool_choice"] = tc
    if "reasoning_effort" in p or "response_format" in p:
        oc = dict(body.get("output_config") or {})
        if "reasoning_effort" in p:
            if p["reasoning_effort"] is None:
                oc.pop("effort", None)
            else:
                oc["effort"] = tr.EFFORT[p["reasoning_effort"]]
        if "response_format" in p:
            v = p["response_format"]
            if v is None:
                oc.pop("format", None)
            else:
                oc["format"] = {"type": "json_schema",
                                "schema": (v.get("json_schema") or {}).get("schema")}
        out["output_config"] = oc or None
    return out


def patch(body: dict, patches: list) -> dict:
    """The native body with the hooks' parameter patches, in order."""
    for p in patches:
        try:
            body = merge_patch(body, _native_fields(p, body))
        except tr.TranslateError as exc:
            raise ProtocolNative(exc.message)
    return body


class HookView:
    """What a hook turn needs of a request that passes through (proxy/hooks.Turn.native)."""

    request = staticmethod(canonical)
    check = staticmethod(check)
    patch = staticmethod(patch)

    @staticmethod
    def answer(obj: dict) -> dict:
        return tr.from_message(obj, int(time.time()))[0]

    @staticmethod
    def stream():
        return tr.StreamToChat(int(time.time()))
