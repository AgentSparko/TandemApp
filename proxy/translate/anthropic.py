"""OpenAI chat completions to an Anthropic-compatible Messages server, and the answer back
(SRV-877). No I/O: the adapter (proxy/adapters/anthropic.py) and the relay call these. The other
direction, a Messages API client in front of an OpenAI-compatible backend (SRV-879, cell 4), is
the second half of this file: `from_messages`, `to_message`, `ChatToMessages`, `error_in`.

    options(backend)                     the backend's `anthropic` settings over the defaults
    to_messages(body, model, opts, max_output=None) -> the Messages request; TranslateError (a
                                         400 naming the field) for what has no equal
    dropped(body, opts)                  the client's fields the request goes without
    from_message(obj, created)           a whole answer -> (the chat.completion, its usage)
    StreamToChat(created).feed(event)    one SSE event of a Messages stream -> OpenAI SSE bytes
    error_out(status, data)              an error answer -> (status, OpenAI's error shape)
    usage_openai(u)                      the Messages usage in OpenAI's fields
    parse_models(doc), next_page(doc)    one page of GET /v1/models, and the id after it

THE REQUEST. `system` and `developer` messages become the top-level `system`, in order (where a
later one stood is lost). `max_tokens` is required upstream: the client's (`max_completion_tokens`
or `max_tokens`), else `default_max_tokens` (16000, 64000 for a stream) capped by the model's
output limit. `temperature`, `top_p` and `top_k` are dropped unless `sampling` is `pass` (a
version-2026-10-21 request and several current models refuse them); `presence_penalty`,
`frequency_penalty`, `logit_bias`, `seed`, `logprobs`, `top_logprobs` and `user` have no equal and
are dropped too; the answer's `X-Tandem-Dropped` header names each one the client sent. `stop`
becomes `stop_sequences`. Function tools become `{name, description, input_schema}` (`strict`
kept); `tool_choice` `auto`, `none`, `required` (`any`) and a named function (`tool`) map one to
one, `parallel_tool_calls: false` to `disable_parallel_tool_use`. A model that refuses `any` or
`tool` answers its own 400, which the client gets as it is: nothing is downgraded silently. An
assistant's `tool_calls` become `tool_use` blocks (the arguments parsed; a string that is not JSON
is a 400) and `role: tool` messages `tool_result` blocks, every result of one turn in one user
message, first (a tool message's image parts go into its result). Images: a data URL to a
`base64` source, an http(s) URL to a `url` source.
`reasoning_effort` asks for thinking in the backend's `thinking_shape`: `adaptive` (thinking
`adaptive` with `output_config.effort`; `none` and `minimal` read as `low`, since current models
cannot switch thinking off), `budget` (`enabled` with `budget_tokens`, for older models) or `off`
(never asked). `response_format` `json_schema` becomes `output_config.format`. With `auto_cache` on
the request carries one top-level `cache_control` breakpoint.

400 (no request goes out): `n` above 1, `response_format` `json_object`, audio and file parts, a
tool of another type than `function`, a `tool_choice` it cannot read, arguments that are not JSON.

THE ANSWER. Text to `content`, thinking text to `reasoning_content` (its signature has no place
in this shape: the thinking stash is SRV-881), `tool_use` blocks to `tool_calls` (input written as
compact JSON). Stop reasons: `end_turn`, `stop_sequence` and `pause_turn` -> `stop`;
`max_tokens` and `model_context_window_exceeded` -> `length`; `tool_use` -> `tool_calls`;
`refusal` -> `content_filter`. Usage: `prompt_tokens` = input + cache read + cache write,
`prompt_tokens_details.cached_tokens` = cache read, `completion_tokens_details.reasoning_tokens`
= the thinking tokens when the server counts them. The cache writes have no OpenAI field; they are
in the ledger only. `redacted_thinking`, server-tool blocks, citations and unknown stream events
are not passed on (pass-through, SRV-881, keeps them).

THE STREAM. `message_start` -> the role chunk; text, thinking and tool blocks -> `delta.content`,
`delta.reasoning_content` and `tool_calls` fragments (a call's index counts the calls, not the
blocks; a call that streamed no input gets `{}` at its block's end); `message_delta` -> the finish chunk, then the usage chunk (input and cache counts from
`message_start`, merged with the cumulative ones of `message_delta`); `message_stop` -> `[DONE]`;
`ping` dropped; `error` -> an error frame and `[DONE]`.
"""

from __future__ import annotations

import hashlib
import json

DEFAULTS = {"auth": "x-api-key", "api_version": "2023-06-01", "sampling": "drop",
            "default_max_tokens": None, "auto_cache": True, "thinking_display": "summarized",
            "thinking_shape": "adaptive"}
MAX_TOKENS = 16000
MAX_TOKENS_STREAM = 64000
SAMPLING = ("temperature", "top_p", "top_k")
LOST = ("presence_penalty", "frequency_penalty", "logit_bias", "seed", "logprobs", "top_logprobs",
        "user")
EFFORT = {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high",
          "xhigh": "xhigh", "max": "max"}
BUDGET = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768,
          "max": 65536}
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop",
          "max_tokens": "length", "model_context_window_exceeded": "length",
          "tool_use": "tool_calls", "refusal": "content_filter"}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_JSON = {"ensure_ascii": False, "separators": (",", ":")}


class TranslateError(Exception):
    """A request field with no equal upstream: the client gets 400 naming it."""

    def __init__(self, param: str, message: str):
        super().__init__(message)
        self.param, self.message = param, message

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": "invalid_request_error",
                          "param": self.param, "code": "unsupported_parameter"}}


def options(backend: dict | None) -> dict:
    out = dict(DEFAULTS)
    got = (backend or {}).get("anthropic")
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if k in DEFAULTS and v is not None})
    return out


def _present(body: dict, k: str) -> bool:
    return k in body and body[k] is not None


def dropped(body: dict, opts: dict) -> list[str]:
    """The fields the client sent that the request goes without, in a fixed order."""
    out = [k for k in SAMPLING if opts.get("sampling") != "pass" and _present(body, k)]
    if opts.get("thinking_shape") == "off" and _present(body, "reasoning_effort"):
        out.append("reasoning_effort")
    return out + [k for k in LOST if _present(body, k) and body[k] is not False]


# ----------------------------------------------------------------------------- the request
def _text_of(content, where: str) -> list[str]:
    """The texts of a system, developer or assistant content: a string or text parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise TranslateError(where, f"{where} must be a string or a list of parts")
    out = []
    for j, p in enumerate(content):
        if isinstance(p, dict) and p.get("type") in ("text", "refusal"):
            t = p.get("text") if p.get("type") == "text" else p.get("refusal")
            if isinstance(t, str):
                out.append(t)
                continue
        raise TranslateError(f"{where}[{j}]", f"{where}[{j}] must be a text part here")
    return out


def _image(part: dict, where: str) -> dict:
    iu = part.get("image_url")
    url = iu.get("url") if isinstance(iu, dict) else iu
    if not isinstance(url, str) or not url:
        raise TranslateError(where, f"{where}: image_url.url is missing")
    if url.startswith("data:"):
        head, sep, data = url[5:].partition(",")
        mt, _, enc = head.partition(";")
        if not sep or enc != "base64" or not mt:
            raise TranslateError(where, f"{where}: a data URL must be base64 with a media type")
        return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise TranslateError(where, f"{where}: an image URL must be a data URL or http(s)")


def _user_blocks(content, where: str) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise TranslateError(where, f"{where} must be a string or a list of parts")
    out = []
    for j, p in enumerate(content):
        w = f"{where}[{j}]"
        t = p.get("type") if isinstance(p, dict) else None
        if t == "text" and isinstance(p.get("text"), str):
            if p["text"]:
                out.append({"type": "text", "text": p["text"]})
        elif t == "image_url":
            out.append(_image(p, w))
        elif t in ("input_audio", "file"):
            raise TranslateError(w, f"{w}: {t} parts have no equal in the Messages API")
        else:
            raise TranslateError(w, f"{w}: the part type {t!r} cannot be translated")
    return out


def _tool_result(m: dict, where: str) -> dict:
    tid = m.get("tool_call_id")
    if not isinstance(tid, str) or not tid:
        raise TranslateError(f"{where}.tool_call_id", f"{where}: a tool message needs "
                                                      f"tool_call_id")
    c = m.get("content")
    if isinstance(c, list):
        # text and image parts (a screenshot a tool returned): blocks of the result
        c = _user_blocks(c, f"{where}.content")
    elif c is None:
        c = ""
    elif not isinstance(c, str):
        raise TranslateError(f"{where}.content", f"{where}.content must be a string or text "
                                                 f"parts")
    return {"type": "tool_result", "tool_use_id": tid, "content": c}


def _tool_uses(calls, where: str) -> list[dict]:
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise TranslateError(where, f"{where} must be a list")
    out = []
    for j, call in enumerate(calls):
        w = f"{where}[{j}]"
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise TranslateError(w, f"{w} must be a function call with a name")
        args = fn.get("arguments")
        if args is None or (isinstance(args, str) and not args.strip()):
            inp = {}
        elif isinstance(args, str):
            try:
                inp = json.loads(args)
            except ValueError:
                raise TranslateError(f"{w}.function.arguments",
                                     f"{w}.function.arguments is not JSON")
        else:
            inp = args
        if not isinstance(inp, dict):
            raise TranslateError(f"{w}.function.arguments",
                                 f"{w}.function.arguments must be a JSON object")
        out.append({"type": "tool_use", "id": str(call.get("id") or f"call_{j}"),
                    "name": fn["name"], "input": inp})
    return out


def _messages(msgs) -> tuple[list[dict], list[dict]]:
    """(system blocks, messages), consecutive messages of one role merged into one, a user
    message's tool results first."""
    if not isinstance(msgs, list) or not msgs:
        raise TranslateError("messages", "messages must be a non-empty list")
    system: list[dict] = []
    turns: list[tuple[str, list[dict]]] = []

    def add(role: str, blocks: list[dict]) -> None:
        if turns and turns[-1][0] == role:
            turns[-1][1].extend(blocks)
        else:
            turns.append((role, list(blocks)))

    for i, m in enumerate(msgs):
        w = f"messages[{i}]"
        if not isinstance(m, dict):
            raise TranslateError(w, f"{w} must be an object")
        role = m.get("role")
        if role in ("system", "developer"):
            system += [{"type": "text", "text": t} for t in _text_of(m.get("content"),
                                                                     f"{w}.content") if t]
        elif role == "user":
            add("user", _user_blocks(m.get("content"), f"{w}.content"))
        elif role == "tool":
            add("user", [_tool_result(m, w)])
        elif role == "assistant":
            blocks = [{"type": "text", "text": t}
                      for t in _text_of(m.get("content"), f"{w}.content") if t]
            add("assistant", blocks + _tool_uses(m.get("tool_calls"), f"{w}.tool_calls"))
        else:
            raise TranslateError(f"{w}.role", f"{w}.role {role!r} cannot be translated")
    out = []
    for role, blocks in turns:
        if role == "user":
            blocks = ([b for b in blocks if b["type"] == "tool_result"]
                      + [b for b in blocks if b["type"] != "tool_result"])
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            out.append({"role": role, "content": blocks[0]["text"]})
        else:
            out.append({"role": role, "content": blocks if blocks else ""})
    return system, out


def _tools(tools) -> list[dict]:
    if not isinstance(tools, list):
        raise TranslateError("tools", "tools must be a list")
    out = []
    for i, t in enumerate(tools):
        w = f"tools[{i}]"
        if not isinstance(t, dict) or t.get("type", "function") != "function" or \
                not isinstance(t.get("function"), dict):
            raise TranslateError(f"{w}.type", f"{w}: only function tools can be translated")
        fn = t["function"]
        if not isinstance(fn.get("name"), str) or not fn["name"]:
            raise TranslateError(f"{w}.function.name", f"{w}.function.name is missing")
        params = fn.get("parameters")
        tool = {"name": fn["name"]}
        if isinstance(fn.get("description"), str):
            tool["description"] = fn["description"]
        tool["input_schema"] = params if isinstance(params, dict) else \
            {"type": "object", "properties": {}}
        if isinstance(fn.get("strict"), bool):
            tool["strict"] = fn["strict"]
        out.append(tool)
    return out


def _tool_choice(tc, parallel) -> dict | None:
    out = None
    if tc is None:
        pass
    elif tc == "auto":
        out = {"type": "auto"}
    elif tc == "none":
        out = {"type": "none"}
    elif tc == "required":
        out = {"type": "any"}
    elif isinstance(tc, dict) and tc.get("type") == "function" and \
            isinstance((tc.get("function") or {}).get("name"), str):
        out = {"type": "tool", "name": tc["function"]["name"]}
    else:
        raise TranslateError("tool_choice", "tool_choice must be auto, none, required or a "
                                            "named function")
    if parallel is False and (out is None or out["type"] != "none"):
        out = dict(out or {"type": "auto"}, disable_parallel_tool_use=True)
    return out


def _thinking(out: dict, effort, opts: dict) -> None:
    if effort is None:
        return
    if not isinstance(effort, str) or effort not in EFFORT:
        raise TranslateError("reasoning_effort", "reasoning_effort must be one of "
                                                 + ", ".join(EFFORT))
    shape = opts.get("thinking_shape")
    if shape == "adaptive":
        out["thinking"] = {"type": "adaptive", "display": opts.get("thinking_display")
                           or "summarized"}
        out.setdefault("output_config", {})["effort"] = EFFORT[effort]
    elif shape == "budget" and effort != "none":
        budget = min(BUDGET[effort], out["max_tokens"] - 1)
        if budget >= 1024:
            out["thinking"] = {"type": "enabled", "budget_tokens": budget}


def to_messages(body: dict, model: str, opts: dict, max_output: int | None = None) -> dict:
    """The Messages request for an OpenAI chat completion request. Raises TranslateError."""
    n = body.get("n")
    if n is not None and n != 1:
        raise TranslateError("n", "n above 1 has no equal in the Messages API")
    rf = body.get("response_format")
    fmt = None
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        raise TranslateError("response_format", "response_format json_object has no equal in "
                                                "the Messages API; use json_schema")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise TranslateError("response_format.json_schema.schema",
                                 "response_format.json_schema.schema is missing")
        fmt = {"type": "json_schema", "schema": schema}
    elif rf is not None and not (isinstance(rf, dict) and rf.get("type") == "text"):
        raise TranslateError("response_format", "response_format cannot be translated")
    stream = bool(body.get("stream"))
    mt = body.get("max_completion_tokens", body.get("max_tokens"))
    if mt is None:
        mt = opts.get("default_max_tokens") or (MAX_TOKENS_STREAM if stream else MAX_TOKENS)
        if isinstance(max_output, int) and max_output > 0:
            mt = min(mt, max_output)
    if not isinstance(mt, int) or isinstance(mt, bool) or mt < 1:
        raise TranslateError("max_tokens", "max_tokens must be a positive integer")
    system, msgs = _messages(body.get("messages"))
    out: dict = {"model": model, "max_tokens": mt}
    if system:
        out["system"] = system
    out["messages"] = msgs
    if body.get("tools"):
        out["tools"] = _tools(body["tools"])
    tc = _tool_choice(body.get("tool_choice"), body.get("parallel_tool_calls"))
    if tc is not None:
        out["tool_choice"] = tc
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [s for s in stop if isinstance(s, str) and s]
    if opts.get("sampling") == "pass":
        for k in SAMPLING:
            if _present(body, k):
                out[k] = body[k]
    _thinking(out, body.get("reasoning_effort"), opts)
    if fmt is not None:
        out.setdefault("output_config", {})["format"] = fmt
    if opts.get("auto_cache"):
        out["cache_control"] = {"type": "ephemeral"}
    if stream:
        out["stream"] = True
    return out


# ----------------------------------------------------------------------------- the answer
def _n(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def usage_openai(u) -> dict | None:
    if not isinstance(u, dict):
        return None
    inp = sum(_n(u.get(k)) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                          "cache_creation_input_tokens"))
    outp = _n(u.get("output_tokens")) or 0
    o = {"prompt_tokens": inp, "completion_tokens": outp, "total_tokens": inp + outp,
         "prompt_tokens_details": {"cached_tokens": _n(u.get("cache_read_input_tokens")) or 0}}
    think = _n((u.get("output_tokens_details") or {}).get("thinking_tokens")) \
        if isinstance(u.get("output_tokens_details"), dict) else None
    if think is not None:
        o["completion_tokens_details"] = {"reasoning_tokens": think}
    return o


def _args(inp) -> str:
    return json.dumps(inp if inp is not None else {}, **_JSON)


def from_message(obj: dict, created: int) -> tuple[dict, dict | None]:
    """(the chat.completion, the Messages usage) of one whole answer."""
    text, reasoning, calls = [], [], []
    for b in obj.get("content") or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            text.append(b["text"])
        elif t == "thinking" and isinstance(b.get("thinking"), str) and b["thinking"]:
            reasoning.append(b["thinking"])
        elif t == "tool_use":
            calls.append({"id": b.get("id"), "type": "function",
                          "function": {"name": b.get("name"), "arguments": _args(b.get("input"))}})
    msg: dict = {"role": "assistant",
                 "content": "".join(text) if text or not calls else None}
    if reasoning:
        msg["reasoning_content"] = "\n\n".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    out = {"id": obj.get("id"), "object": "chat.completion", "created": created,
           "model": obj.get("model"),
           "choices": [{"index": 0, "message": msg, "logprobs": None,
                        "finish_reason": FINISH.get(obj.get("stop_reason"), "stop")}]}
    ou = usage_openai(usage)
    if ou is not None:
        out["usage"] = ou
    return out, usage


def error_out(status: int, data: bytes) -> tuple[int, bytes]:
    """An error answer in OpenAI's shape. 529 (overloaded) becomes 503 for an OpenAI client."""
    etype, msg = None, None
    try:
        doc = json.loads(data)
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            etype, msg = err.get("type"), err.get("message")
    except ValueError:
        pass
    if not isinstance(etype, str):
        etype = "upstream_error"
    if not isinstance(msg, str) or not msg:
        msg = f"the backend answered HTTP {status}"
    body = {"error": {"message": msg, "type": etype, "param": None, "code": etype}}
    return (503 if status == 529 else status), json.dumps(body, **_JSON).encode()


def _event(event: bytes) -> tuple[str | None, dict | None]:
    name, data = None, []
    for line in event.replace(b"\r\n", b"\n").split(b"\n"):
        if line.startswith(b"event:"):
            name = line[6:].strip().decode("utf-8", "replace")
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))
    if not data:
        return name, None
    try:
        obj = json.loads(b"\n".join(data))
    except ValueError:
        return name, None
    if not isinstance(obj, dict):
        return name, None
    return obj.get("type") or name, obj


class StreamToChat:
    """One Messages stream as OpenAI chunks. `usage` is the merged Messages usage, `done` true
    once `[DONE]` went out. `keep(content)` (SRV-881, the thinking stash) gets the answer's own
    content blocks, signatures included, when it ends with `message_stop`."""

    def __init__(self, created: int, keep=None):
        self.created = created
        self.keep = keep
        self.content: list = []          # the answer's blocks as the backend sent them
        self._open: dict = {}            # block index -> (its block, the input JSON so far)
        self.id = None
        self.model = None
        self.usage: dict | None = None
        self.blocks: dict = {}           # block index -> ("text" | "thinking" | "tool", n)
        self.ntools = 0
        self.args_sent: set = set()      # the calls whose arguments went out (some or all)
        self.done = False
        self.error = None
        self.rest = bytearray()          # the relay's unfinished upstream event

    def _chunk(self, delta: dict, finish=None) -> bytes:
        obj = {"id": self.id, "object": "chat.completion.chunk", "created": self.created,
               "model": self.model,
               "choices": [{"index": 0, "delta": delta, "logprobs": None,
                            "finish_reason": finish}]}
        return b"data: " + json.dumps(obj, **_JSON).encode() + b"\n\n"

    def _tool(self, n: int, **fn) -> dict:
        return {"tool_calls": [dict({"index": n}, **fn)]}

    def _collect(self, kind, obj) -> None:
        """The answer's own blocks, for the stash (only while it has someone to give them)."""
        if kind == "content_block_start" and isinstance(obj.get("content_block"), dict):
            b = json.loads(json.dumps(obj["content_block"]))
            self._open[obj.get("index")] = (b, [])
            self.content.append(b)
        elif kind == "content_block_delta" and obj.get("index") in self._open:
            b, parts = self._open[obj["index"]]
            d = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = (b.get("text") or "") + (d.get("text") or "")
            elif dt == "thinking_delta":
                b["thinking"] = (b.get("thinking") or "") + (d.get("thinking") or "")
            elif dt == "signature_delta":
                b["signature"] = d.get("signature") or ""
            elif dt == "input_json_delta":
                parts.append(d.get("partial_json") or "")
            elif dt == "citations_delta" and d.get("citation") is not None:
                b.setdefault("citations", []).append(d["citation"])
        elif kind == "content_block_stop" and obj.get("index") in self._open:
            b, parts = self._open.pop(obj["index"])
            if b.get("type") in ("tool_use", "server_tool_use") and "".join(parts).strip():
                try:
                    b["input"] = json.loads("".join(parts))
                except ValueError:
                    self.keep = None             # a call cut short: nothing to keep
        elif kind == "message_stop" and self.keep is not None:
            keep, self.keep = self.keep, None
            keep(self.content)
        elif kind == "error":
            self.keep = None

    def feed(self, event: bytes) -> bytes:
        if self.done:
            return b""
        kind, obj = _event(event)
        if obj is None:
            return b""
        if self.keep is not None:
            self._collect(kind, obj)
        if kind == "message_start":
            m = obj.get("message") or {}
            self.id, self.model = m.get("id"), m.get("model")
            if isinstance(m.get("usage"), dict):
                self.usage = dict(m["usage"])
            return self._chunk({"role": "assistant", "content": ""})
        if kind == "content_block_start":
            b = obj.get("content_block") or {}
            t = b.get("type")
            idx = obj.get("index")
            if t == "text":
                self.blocks[idx] = ("text", None)
                return self._chunk({"content": b["text"]}) if b.get("text") else b""
            if t == "thinking":
                self.blocks[idx] = ("thinking", None)
                return self._chunk({"reasoning_content": b["thinking"]}) \
                    if b.get("thinking") else b""
            if t == "tool_use":
                n = self.ntools
                self.ntools += 1
                self.blocks[idx] = ("tool", n)
                out = self._chunk(self._tool(n, id=b.get("id"), type="function",
                                             function={"name": b.get("name"),
                                                       "arguments": ""}))
                if isinstance(b.get("input"), dict) and b["input"]:
                    out += self._chunk(self._tool(n, function={"arguments": _args(b["input"])}))
                    self.args_sent.add(n)
                return out
            self.blocks[idx] = ("skip", None)
            return b""
        if kind == "content_block_delta":
            kind_n = self.blocks.get(obj.get("index"), ("skip", None))
            d = obj.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and kind_n[0] == "text" and d.get("text"):
                return self._chunk({"content": d["text"]})
            if dt == "thinking_delta" and kind_n[0] == "thinking" and d.get("thinking"):
                return self._chunk({"reasoning_content": d["thinking"]})
            if dt == "input_json_delta" and kind_n[0] == "tool" and d.get("partial_json"):
                self.args_sent.add(kind_n[1])
                return self._chunk(self._tool(kind_n[1],
                                              function={"arguments": d["partial_json"]}))
            return b""                   # signature_delta, citations_delta, an unknown delta
        if kind == "content_block_stop":
            kind_n = self.blocks.get(obj.get("index"), ("skip", None))
            if kind_n[0] == "tool" and kind_n[1] not in self.args_sent:
                # a call that streamed no input: `{}`, as a whole answer writes it
                self.args_sent.add(kind_n[1])
                return self._chunk(self._tool(kind_n[1], function={"arguments": "{}"}))
            return b""
        if kind == "message_delta":
            u = obj.get("usage")
            if isinstance(u, dict):
                self.usage = dict(self.usage or {})
                self.usage.update({k: v for k, v in u.items() if v is not None})
            stop = (obj.get("delta") or {}).get("stop_reason")
            out = self._chunk({}, FINISH.get(stop, "stop"))
            ou = usage_openai(self.usage)
            if ou is not None:
                out += b"data: " + json.dumps(
                    {"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                     "model": self.model, "choices": [], "usage": ou}, **_JSON).encode() + b"\n\n"
            return out
        if kind == "message_stop":
            self.done = True
            return b"data: [DONE]\n\n"
        if kind == "error":
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            etype = err.get("type") if isinstance(err.get("type"), str) else "upstream_error"
            self.error = err or {"type": etype}
            self.done = True
            body = {"error": {"message": err.get("message") or "the backend sent an error",
                              "type": etype, "param": None, "code": etype}}
            return b"data: " + json.dumps(body, **_JSON).encode() + b"\n\ndata: [DONE]\n\n"
        return b""                       # ping, an unknown event


# ----------------------------------------------------------------------------- the model list
def parse_models(doc) -> list[dict]:
    """One page of GET /v1/models in the adapter's fields."""
    out = []
    for m in (doc.get("data") if isinstance(doc, dict) else None) or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}

        def on(*path):
            x = caps
            for p in path:
                x = x.get(p) if isinstance(x, dict) else None
            return isinstance(x, dict) and x.get("supported") is True
        levels = [lv for lv in EFFORT_LEVELS if on("effort", lv)]
        name = m.get("display_name")
        out.append({"id": m["id"], "context_length": _n(m.get("max_input_tokens")),
                    "name": name.strip() if isinstance(name, str) and name.strip() else None,
                    "max_output": _n(m.get("max_tokens")),
                    "input_modalities": (["text", "image"] if on("image_input") else ["text"])
                    if caps else None,
                    "effort": {"levels": levels, "default": None} if levels else None})
    return out


def next_page(doc) -> str | None:
    """The `after_id` of the next page, or None at the last one."""
    if isinstance(doc, dict) and doc.get("has_more") is True and \
            isinstance(doc.get("last_id"), str) and doc["last_id"]:
        return doc["last_id"]
    return None


# ============================================================================= cell 4 (SRV-879)
# A Messages API client in front of an OpenAI-compatible backend: the request in, the answer, the
# stream and the errors out. proxy/messages.py runs it; the pipeline between sees the OpenAI shape.
#
# THE REQUEST. `system` (a string or text blocks) becomes one `system` message, blocks joined by a
# blank line; a `system` message inside `messages` stays where it stands. User text and images
# become content parts (`base64` to a data URL, `url` to `image_url`); `tool_result` blocks become
# `role: tool` messages ahead of the turn's other parts (their text joined; their images and
# `is_error` are lost). Assistant text becomes `content`, `tool_use` blocks `tool_calls` (input
# written as compact JSON). Thinking blocks in the history never go on: our own (signed by
# `signature`) silently, `redacted_thinking` and foreign ones named as dropped. Client tools become
# `function` tools (`strict` kept); a tool with a `type` (server tools, schema-less built-in tools)
# is a 400 naming the type, as are `document` and other block types, `mcp_servers` and
# `container`. `tool_choice`: `auto`, `any` to `required`, `tool` to a named function, `none`;
# `disable_parallel_tool_use` to `parallel_tool_calls: false`. `stop_sequences` to `stop`;
# `temperature` and `top_p` pass, `top_k` to a local engine only; `output_config.format` to
# `response_format` `json_schema`. `thinking` and `output_config.effort` become the backend's own
# switch through `Adapter.thinking` (proxy/messages.py; `thinking_of` reads them here).
# Dropped and named (`X-Tandem-Dropped`): every `cache_control`, `metadata`,
# `thinking.budget_tokens`, `output_config.task_budget`, any top-level field this does not know.
# A stream asks for its usage frame (`stream_options.include_usage`).
#
# THE ANSWER. Reasoning (`reasoning_content` or `reasoning`) becomes a `thinking` block whose
# signature this server makes (`signature`; the text empty with `display: "omitted"`), then the
# text block, then a `tool_use` block per call. Stop reasons: `stop` to `end_turn`, or
# `stop_sequence` when the backend names the string that matched (`choices[].stop_reason`, where a
# server sets it); `length` to `max_tokens`; `tool_calls` to `tool_use`; `content_filter` to `refusal`.
# Usage: `input_tokens` = prompt minus cached, `cache_read_input_tokens` = cached,
# `cache_creation_input_tokens` = 0, `output_tokens` = completion.
#
# THE STREAM (`ChatToMessages`). `message_start` first, its usage 0 (the backend counts at the
# end; `message_delta` carries the real counts). Reasoning deltas open a thinking block and its
# `signature_delta` closes it; content deltas a text block; tool calls are gathered and each goes
# out as one `tool_use` block when the answer ends, its whole input in one `input_json_delta`
# (the Messages API too buffers a value until it is whole), so the joined input is always valid
# JSON. Then `message_delta` and `message_stop`. An error frame is an `error` event, the end.

MESSAGE_FIELDS = ("model", "max_tokens", "messages", "system", "stream", "tools", "tool_choice",
                  "stop_sequences", "temperature", "top_p", "top_k", "thinking", "output_config",
                  "metadata", "cache_control")
# top-level fields that need the provider's own servers: a 400, never a silent drop
NEEDS_SERVER = {"mcp_servers": "MCP servers run on the provider's side",
                "container": "a container runs on the provider's side"}
STOP_IN = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
           "function_call": "tool_use", "content_filter": "refusal", "eos": "end_turn"}
ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 402: "billing_error",
               403: "permission_error", 404: "not_found_error", 409: "conflict_error",
               413: "request_too_large", 429: "rate_limit_error", 500: "api_error",
               504: "timeout_error", 529: "overloaded_error"}
LEVEL = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
SIG = "tandemapp-1."


def signature(text: str) -> str:
    """The signature of a thinking block this server made: a mark, not a secret. A block that
    carries it is ours and is dropped from the history without a word."""
    return SIG + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def ours(sig) -> bool:
    return isinstance(sig, str) and sig.startswith(SIG)


def messages_error(status: int, message: str, etype: str | None = None,
                   request_id: str | None = None) -> bytes:
    """A refusal in the Messages API's error shape."""
    body = {"type": "error", "error": {"type": etype or error_type(status), "message": message}}
    if request_id:
        body["request_id"] = request_id
    return json.dumps(body, **_JSON).encode()


def error_type(status: int) -> str:
    if status in ERROR_TYPES:
        return ERROR_TYPES[status]
    return "api_error" if status >= 500 else "invalid_request_error"


def error_in(status: int, data: bytes) -> tuple[int, str, str]:
    """An error answer of the pipeline (OpenAI's shape, or any bytes) as (status, type, message)
    for a Messages API client: 503 (a drained node, a full backend, an overloaded server) is 529
    `overloaded_error`; the rest keep their status, typed by it."""
    msg = None
    try:
        doc = json.loads(data)
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            msg = err.get("message")
        elif isinstance(err, str):
            msg = err
    except (ValueError, RecursionError):
        pass
    if status == 503:
        status = 529
    if not isinstance(msg, str) or not msg:
        msg = f"the backend answered HTTP {status}"
    return status, error_type(status), msg


def _in_text(content, where: str) -> str:
    """A system prompt or a system message: a string, or text blocks joined by a blank line."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TranslateError(where, f"{where}: a string or a list of text blocks")
    out = []
    for j, b in enumerate(content):
        if not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str):
            raise TranslateError(f"{where}.{j}", f"{where}.{j}: only text blocks can stand here")
        out.append(b["text"])
    return "\n\n".join(out)


def _in_image(b: dict, where: str) -> dict:
    src = b.get("source") if isinstance(b.get("source"), dict) else {}
    if src.get("type") == "base64" and isinstance(src.get("data"), str) and \
            isinstance(src.get("media_type"), str):
        url = f"data:{src['media_type']};base64,{src['data']}"
    elif src.get("type") == "url" and isinstance(src.get("url"), str) and src["url"]:
        url = src["url"]
    else:
        raise TranslateError(f"{where}.source", f"{where}.source: an image is a base64 or a url "
                                                f"source here")
    return {"type": "image_url", "image_url": {"url": url}}


def _refuse_block(t, where: str):
    raise TranslateError(f"{where}.type", f"{where}: the block type {t!r} has no equal in the "
                                          f"OpenAI chat shape of this backend")


def _in_result(b: dict, where: str, lost: set) -> dict:
    tid = b.get("tool_use_id")
    if not isinstance(tid, str) or not tid:
        raise TranslateError(f"{where}.tool_use_id", f"{where}.tool_use_id is missing")
    c = b.get("content")
    if c is None:
        text = ""
    elif isinstance(c, str):
        text = c
    elif isinstance(c, list):
        parts = []
        for j, p in enumerate(c):
            t = p.get("type") if isinstance(p, dict) else None
            if t == "text" and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif t == "image":
                lost.add("tool_result.image")
            else:
                _refuse_block(t, f"{where}.content.{j}")
        text = "\n".join(parts)
    else:
        raise TranslateError(f"{where}.content", f"{where}.content: a string or blocks")
    if b.get("is_error"):
        lost.add("tool_result.is_error")
    return {"role": "tool", "tool_call_id": tid, "content": text}


def _in_user(content, where: str, lost: set) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        raise TranslateError(f"{where}.content", f"{where}.content: a string or a list of blocks")
    tools, parts = [], []
    for j, b in enumerate(content):
        w = f"{where}.content.{j}"
        t = b.get("type") if isinstance(b, dict) else None
        if isinstance(b, dict) and "cache_control" in b:
            lost.add("cache_control")
        if t == "text" and isinstance(b.get("text"), str):
            parts.append({"type": "text", "text": b["text"]})
        elif t == "image":
            parts.append(_in_image(b, w))
        elif t == "tool_result":
            tools.append(_in_result(b, w, lost))
        else:
            _refuse_block(t, w)
    out = list(tools)
    if parts or not tools:
        if len(parts) == 1 and parts[0]["type"] == "text":
            out.append({"role": "user", "content": parts[0]["text"]})
        else:
            out.append({"role": "user", "content": parts})
    return out


def _in_assistant(content, where: str, lost: set) -> dict:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    if not isinstance(content, list):
        raise TranslateError(f"{where}.content", f"{where}.content: a string or a list of blocks")
    text, calls = [], []
    for j, b in enumerate(content):
        w = f"{where}.content.{j}"
        t = b.get("type") if isinstance(b, dict) else None
        if isinstance(b, dict) and "cache_control" in b:
            lost.add("cache_control")
        if t == "text" and isinstance(b.get("text"), str):
            text.append(b["text"])
        elif t == "tool_use":
            if not isinstance(b.get("name"), str) or not isinstance(b.get("id"), str):
                raise TranslateError(w, f"{w}: a tool_use block needs an id and a name")
            inp = b.get("input") if b.get("input") is not None else {}
            if not isinstance(inp, dict):
                raise TranslateError(f"{w}.input", f"{w}.input must be an object")
            calls.append({"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": _args(inp)}})
        elif t == "thinking":
            if not ours(b.get("signature")):
                lost.add("thinking")
        elif t == "redacted_thinking":
            lost.add("thinking")
        else:
            _refuse_block(t, w)
    msg: dict = {"role": "assistant", "content": "".join(text) if text or not calls else None}
    if calls:
        msg["tool_calls"] = calls
    return msg


def _in_tools(tools, lost: set) -> list[dict]:
    if not isinstance(tools, list):
        raise TranslateError("tools", "tools must be a list")
    out = []
    for i, t in enumerate(tools):
        w = f"tools.{i}"
        if not isinstance(t, dict):
            raise TranslateError(w, f"{w} must be an object")
        typ = t.get("type")
        if typ not in (None, "custom"):
            raise TranslateError(f"{w}.type", f"{w}: the tool type {typ!r} runs on the "
                                              f"provider's servers or has no schema; only client "
                                              f"tools with an input_schema can be used here")
        if not isinstance(t.get("name"), str) or not t["name"]:
            raise TranslateError(f"{w}.name", f"{w}.name is missing")
        schema = t.get("input_schema")
        if not isinstance(schema, dict):
            raise TranslateError(f"{w}.input_schema", f"{w}.input_schema is missing")
        if "cache_control" in t:
            lost.add("cache_control")
        fn = {"name": t["name"]}
        if isinstance(t.get("description"), str):
            fn["description"] = t["description"]
        fn["parameters"] = schema
        if isinstance(t.get("strict"), bool):
            fn["strict"] = t["strict"]
        out.append({"type": "function", "function": fn})
    return out


def _in_choice(tc) -> tuple[object, bool]:
    """(OpenAI's tool_choice, parallel tool calls switched off)."""
    if not isinstance(tc, dict):
        raise TranslateError("tool_choice", "tool_choice must be an object")
    t = tc.get("type")
    off = tc.get("disable_parallel_tool_use") is True
    if t == "auto":
        return "auto", off
    if t == "any":
        return "required", off
    if t == "none":
        return "none", False
    if t == "tool" and isinstance(tc.get("name"), str) and tc["name"]:
        return {"type": "function", "function": {"name": tc["name"]}}, off
    raise TranslateError("tool_choice", "tool_choice.type must be auto, any, tool or none")


def thinking_of(body: dict) -> tuple[bool | None, str | None]:
    """What the client asked of thinking: (on, the effort word for Adapter.thinking), or
    (None, None) when it asked nothing (the backend's default stands)."""
    th = body.get("thinking") if isinstance(body.get("thinking"), dict) else None
    oc = body.get("output_config") if isinstance(body.get("output_config"), dict) else {}
    effort = LEVEL.get(oc.get("effort")) if isinstance(oc.get("effort"), str) else None
    if th is not None and th.get("type") in ("disabled", "between_tools"):
        return False, "off"
    if th is not None or effort is not None:
        return True, effort
    return None, None


def from_messages(body: dict, top_k: bool = True, need_max: bool = True
                  ) -> tuple[dict, list[str], dict]:
    """(the OpenAI chat request, the fields it goes without, meta for the answer: the client's
    model, its stop sequences, the thinking display). Raises TranslateError (a 400)."""
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise TranslateError("model", "model: Field required")
    for k, why in NEEDS_SERVER.items():
        if body.get(k):
            raise TranslateError(k, f"{k}: {why}; this server cannot run it")
    lost: set = set()
    mt = body.get("max_tokens")
    if need_max or mt is not None:
        if not isinstance(mt, int) or isinstance(mt, bool) or mt < 1:
            raise TranslateError("max_tokens", "max_tokens: a positive integer is required")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise TranslateError("messages", "messages: a non-empty list is required")
    out_msgs: list[dict] = []
    if body.get("system") is not None:
        sys_text = _in_text(body["system"], "system")
        if isinstance(body["system"], list) and any(
                isinstance(b, dict) and "cache_control" in b for b in body["system"]):
            lost.add("cache_control")
        if sys_text:
            out_msgs.append({"role": "system", "content": sys_text})
    for i, m in enumerate(msgs):
        w = f"messages.{i}"
        if not isinstance(m, dict):
            raise TranslateError(w, f"{w} must be an object")
        role = m.get("role")
        if role == "user":
            out_msgs += _in_user(m.get("content"), w, lost)
        elif role == "assistant":
            out_msgs.append(_in_assistant(m.get("content"), w, lost))
        elif role == "system":
            c = m.get("content")
            if c in (None, []):                 # an effort-only system message: nothing to say
                continue
            out_msgs.append({"role": "system", "content": _in_text(c, f"{w}.content")})
        else:
            raise TranslateError(f"{w}.role", f"{w}.role must be user or assistant")
    out: dict = {"model": body["model"], "messages": out_msgs}
    if mt is not None:
        out["max_tokens"] = mt
    if body.get("tools"):
        out["tools"] = _in_tools(body["tools"], lost)
    if body.get("tool_choice") is not None:
        tc, off = _in_choice(body["tool_choice"])
        out["tool_choice"] = tc
        if off:
            out["parallel_tool_calls"] = False
    stops = body.get("stop_sequences")
    if stops is not None:
        if not isinstance(stops, list) or not all(isinstance(s, str) for s in stops):
            raise TranslateError("stop_sequences", "stop_sequences must be a list of strings")
        if stops:
            out["stop"] = list(stops)
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            out[k] = body[k]
    if body.get("top_k") is not None:
        if top_k:
            out["top_k"] = body["top_k"]
        else:
            lost.add("top_k")
    th = body.get("thinking")
    if th is not None and not isinstance(th, dict):
        raise TranslateError("thinking", "thinking must be an object")
    if isinstance(th, dict) and th.get("budget_tokens") is not None:
        lost.add("thinking.budget_tokens")
    oc = body.get("output_config")
    if oc is not None:
        if not isinstance(oc, dict):
            raise TranslateError("output_config", "output_config must be an object")
        fmt = oc.get("format")
        if fmt is not None:
            if not isinstance(fmt, dict) or fmt.get("type") != "json_schema" or \
                    not isinstance(fmt.get("schema"), dict):
                raise TranslateError("output_config.format",
                                     "output_config.format: a json_schema with a schema")
            out["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "output", "schema": fmt["schema"], "strict": True}}
        for k in oc:
            if k not in ("format", "effort"):
                lost.add(f"output_config.{k}")
    if body.get("metadata") is not None:
        lost.add("metadata")
    if body.get("cache_control") is not None:
        lost.add("cache_control")
    for k in body:
        if k not in MESSAGE_FIELDS and k not in NEEDS_SERVER and body[k] is not None:
            lost.add(k)
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    display = th.get("display") if isinstance(th, dict) else None
    meta = {"model": body["model"], "stop": list(stops or []),
            "display": "omitted" if display == "omitted" else "summarized"}
    return out, sorted(lost), meta


# ----------------------------------------------------------------------------- the answer out
def usage_in(u) -> dict:
    """OpenAI's usage as the Messages API's: input without the cached part, cache reads apart,
    no cache writes (a local engine caches prefixes by itself)."""
    u = u if isinstance(u, dict) else {}
    prompt = _n(u.get("prompt_tokens")) or 0
    det = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) \
        else {}
    cached = min(prompt, _n(det.get("cached_tokens")) or 0)
    out = {"input_tokens": prompt - cached, "output_tokens": _n(u.get("completion_tokens")) or 0,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached}
    cd = u.get("completion_tokens_details")
    think = _n(cd.get("reasoning_tokens")) if isinstance(cd, dict) else None
    if think is not None:
        out["output_tokens_details"] = {"thinking_tokens": think}
    return out


def _stop_of(finish, matched, meta: dict) -> tuple[str, str | None]:
    if isinstance(matched, str) and matched in (meta.get("stop") or ()):
        return "stop_sequence", matched
    return STOP_IN.get(finish, "end_turn"), None


def _tool_input(args) -> dict:
    """A call's arguments as the object a tool_use block holds; arguments that are not a JSON
    object are kept as text under `_raw_arguments`, so nothing is lost or invented."""
    if isinstance(args, dict):
        return args
    if args is None or (isinstance(args, str) and not args.strip()):
        return {}
    try:
        v = json.loads(args)
    except (ValueError, TypeError, RecursionError):
        v = None
    return v if isinstance(v, dict) else {"_raw_arguments": str(args)}


def _reasoning(m: dict) -> str | None:
    for k in ("reasoning_content", "reasoning"):
        if isinstance(m.get(k), str) and m[k]:
            return m[k]
    return None


def to_message(obj: dict, mid: str, meta: dict) -> dict:
    """A whole chat.completion as a Messages API answer."""
    ch = ((obj.get("choices") or [{}])[0]) or {}
    m = ch.get("message") if isinstance(ch.get("message"), dict) else {}
    content: list[dict] = []
    think = _reasoning(m)
    if think:
        content.append({"type": "thinking",
                        "thinking": "" if meta.get("display") == "omitted" else think,
                        "signature": signature(think)})
    c = m.get("content")
    if isinstance(c, list):
        c = "".join(p.get("text") or "" for p in c if isinstance(p, dict))
    if isinstance(c, str) and c:
        content.append({"type": "text", "text": c})
    calls = [tc for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)]
    for i, tc in enumerate(calls):
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        content.append({"type": "tool_use", "id": str(tc.get("id") or f"toolu_{mid[4:]}_{i}"),
                        "name": fn.get("name") or "", "input": _tool_input(fn.get("arguments"))})
    stop, seq = _stop_of(ch.get("finish_reason"), ch.get("stop_reason"), meta)
    if calls and stop == "end_turn":
        stop = "tool_use"
    if not content:
        content.append({"type": "text", "text": ""})
    return {"id": mid, "type": "message", "role": "assistant", "model": meta.get("model"),
            "content": content, "stop_reason": stop, "stop_sequence": seq,
            "usage": usage_in(obj.get("usage"))}


def sse(kind: str, obj: dict) -> bytes:
    return f"event: {kind}\ndata: ".encode() + json.dumps(obj, **_JSON).encode() + b"\n\n"


def message_start(mid: str, model) -> bytes:
    return sse("message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0}}})


PING = sse("ping", {"type": "ping"})


def error_event(etype: str, message: str) -> bytes:
    return sse("error", {"type": "error", "error": {"type": etype, "message": message}})


def message_events(msg: dict, started: bool = False) -> bytes:
    """A whole Messages answer as the stream that would have carried it (a backend that answered
    a stream request whole)."""
    out = b"" if started else message_start(msg["id"], msg.get("model"))
    for i, b in enumerate(msg["content"]):
        t = b["type"]
        if t == "text":
            out += sse("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "text", "text": ""}})
            if b["text"]:
                out += _delta(i, {"type": "text_delta", "text": b["text"]})
        elif t == "thinking":
            out += sse("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "thinking",
                                                                 "thinking": "",
                                                                 "signature": ""}})
            out += _delta(i, {"type": "thinking_delta", "thinking": b["thinking"]})
            out += _delta(i, {"type": "signature_delta", "signature": b["signature"]})
        else:
            out += sse("content_block_start", {"type": "content_block_start", "index": i,
                                               "content_block": {"type": "tool_use",
                                                                 "id": b["id"], "name": b["name"],
                                                                 "input": {}}})
            out += _delta(i, {"type": "input_json_delta", "partial_json": _args(b["input"])})
        out += sse("content_block_stop", {"type": "content_block_stop", "index": i})
    out += sse("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": msg["stop_reason"],
                                           "stop_sequence": msg["stop_sequence"]},
                                 "usage": msg["usage"]})
    return out + sse("message_stop", {"type": "message_stop"})


def _delta(i: int, d: dict) -> bytes:
    return sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d})


class ChatToMessages:
    """One OpenAI chat stream as a Messages API stream, fed event by event (each without its
    blank line). `start()` is the `message_start`; `feed` the rest; `finish()` closes an answer
    the backend ended without `[DONE]`; `done` once `message_stop` or an `error` went out."""

    def __init__(self, mid: str, meta: dict):
        self.mid, self.meta = mid, meta
        self.started = False
        self.done = False
        self.index = -1                      # the newest block's index
        self.open: str | None = None         # "text" | "thinking" while a block is open
        self.think: list[str] = []
        self.tools: dict = {}                # OpenAI call index -> {id, name, args: [...]}
        self.order: list = []
        self.finish_reason = None
        self.matched = None
        self.usage = None
        self.error = None

    def start(self) -> bytes:
        if self.started:
            return b""
        self.started = True
        return message_start(self.mid, self.meta.get("model"))

    def _close(self) -> bytes:
        if self.open is None:
            return b""
        out = b""
        if self.open == "thinking":
            text = "".join(self.think)
            if self.meta.get("display") == "omitted":
                out += _delta(self.index, {"type": "thinking_delta", "thinking": ""})
            out += _delta(self.index, {"type": "signature_delta", "signature": signature(text)})
            self.think = []
        self.open = None
        return out + sse("content_block_stop", {"type": "content_block_stop",
                                                "index": self.index})

    def _open(self, kind: str) -> bytes:
        if self.open == kind:
            return b""
        out = self._close()
        self.index += 1
        self.open = kind
        block = {"type": "text", "text": ""} if kind == "text" else \
            {"type": "thinking", "thinking": "", "signature": ""}
        return out + sse("content_block_start", {"type": "content_block_start",
                                                 "index": self.index, "content_block": block})

    def feed(self, event: bytes) -> bytes:
        if self.done:
            return b""
        data = sse_data(event)
        if data is None:
            return b""                        # a comment (the cost line), a named event
        out = self.start()
        if data.strip() == b"[DONE]":
            return out + self.finish()
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError):
            return out
        if not isinstance(obj, dict):
            return out
        if obj.get("error") is not None:
            err = obj["error"] if isinstance(obj["error"], dict) else {"message": str(obj["error"])}
            etype = err.get("type") if err.get("type") in ERROR_TYPES.values() else "api_error"
            if err.get("type") in ("server_busy", "overloaded_error") or \
                    err.get("code") in ("node_draining", "max_inflight"):
                etype = "overloaded_error"
            self.error = err
            self.done = True
            return out + error_event(etype, str(err.get("message") or "the backend failed"))
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            d = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
            r = _reasoning(d)
            if r:
                out += self._open("thinking")
                self.think.append(r)
                if self.meta.get("display") != "omitted":
                    out += _delta(self.index, {"type": "thinking_delta", "thinking": r})
            if isinstance(d.get("content"), str) and d["content"]:
                out += self._open("text")
                out += _delta(self.index, {"type": "text_delta", "text": d["content"]})
            for tc in d.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                k = tc.get("index", 0)
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                got = self.tools.get(k)
                if got is None:
                    got = self.tools[k] = {"id": tc.get("id"), "name": fn.get("name"),
                                           "args": []}
                    self.order.append(k)
                got["id"] = got["id"] or tc.get("id")
                got["name"] = got["name"] or fn.get("name")
                if isinstance(fn.get("arguments"), str):
                    got["args"].append(fn["arguments"])
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]
                self.matched = ch.get("stop_reason")
        return out

    def finish(self) -> bytes:
        """The tool blocks, `message_delta` and `message_stop` (once)."""
        if self.done:
            return b""
        out = self.start() + self._close()
        for n, k in enumerate(self.order):
            t = self.tools[k]
            self.index += 1
            tid = str(t["id"] or f"toolu_{self.mid[4:]}_{n}")
            out += sse("content_block_start", {"type": "content_block_start",
                                               "index": self.index, "content_block": {
                                                   "type": "tool_use", "id": tid,
                                                   "name": t["name"] or "", "input": {}}})
            out += _delta(self.index, {"type": "input_json_delta",
                                       "partial_json": _args(_tool_input("".join(t["args"])))})
            out += sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        stop, seq = _stop_of(self.finish_reason, self.matched, self.meta)
        if self.order and stop == "end_turn":
            stop = "tool_use"
        out += sse("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop, "stop_sequence": seq},
                                     "usage": usage_in(self.usage)})
        self.done = True
        return out + sse("message_stop", {"type": "message_stop"})

    def fail(self, etype: str, message: str) -> bytes:
        """An `error` event in place of the rest (once)."""
        if self.done:
            return b""
        self.done = True
        return self.start() + error_event(etype, message)


def sse_data(event: bytes) -> bytes | None:
    parts = [line[5:].lstrip(b" ") for line in event.replace(b"\r\n", b"\n").split(b"\n")
             if line.startswith(b"data:")]
    return b"\n".join(parts) if parts else None
