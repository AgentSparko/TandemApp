"""OpenAI chat completions to an Anthropic-compatible Messages server, and the answer back
(SRV-877). No I/O: the adapter (proxy/adapters/anthropic.py) and the relay call these.

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
    once `[DONE]` went out."""

    def __init__(self, created: int):
        self.created = created
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

    def feed(self, event: bytes) -> bytes:
        if self.done:
            return b""
        kind, obj = _event(event)
        if obj is None:
            return b""
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
