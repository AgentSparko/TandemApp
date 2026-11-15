"""One sample event per hooks-v1 stage: the playground's default (`POST /v1/fleet/hooks/{id}/test`)
and the contract's examples (contracts/hooks-v1/examples/, tests/test_hooks_contract.py holds them
to the schemas). Made-up values only.

    sample(stage) -> {"request": the call's request block, "meta": the meta event,
                      "content": what a redacted or full hook gets as `content`, or None}
"""

from __future__ import annotations

import copy

from proxy import hookcall

REQUEST = {"request_id": "chatcmpl-px-0123456789abcdef01234567", "endpoint": "chat",
           "model": "example-model", "upstream_model": "example-model", "backend": "local",
           "profile": None, "user": "owner", "session": "s-1", "client_kind": "unknown",
           "stream": False}

BODY = {"model": "example-model", "temperature": 0.2, "max_tokens": 256,
        "messages": [{"role": "system", "content": "Answer briefly."},
                     {"role": "user", "content": "What is the capital of France?"}],
        "tools": [{"type": "function", "function": {"name": "search", "parameters": {
            "type": "object", "properties": {"q": {"type": "string"}}}}}]}

CALLS = [{"id": "call_1", "type": "function",
          "function": {"name": "search", "arguments": "{\"q\": \"capital of France\"}"}}]

ANSWER = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "example-model",
          "choices": [{"index": 0, "finish_reason": "stop",
                       "message": {"role": "assistant", "content": "Paris."}}],
          "usage": {"prompt_tokens": 21, "completion_tokens": 2, "total_tokens": 23}}

ROUTE = {"backend": "local", "model": "example-model", "fallbacks": []}


def sample(stage: str) -> dict:
    req = dict(REQUEST)
    meta: dict
    content = None
    if stage == "request.received":
        meta, content = hookcall.request_meta(BODY), BODY
    elif stage == "request.before_engine":
        meta = dict(hookcall.request_meta(BODY), route={"backend": "local",
                                                        "model": "example-model", "attempt": 0})
        content = BODY
    elif stage == "route.select":
        meta = {"route": ROUTE, "backends": ["local", "cloud"]}
    elif stage == "decision.ask":
        meta = {"question": {"state": "class", "options": ["chat", "code"], "deadline_ms": 400}}
        content = {"prompt": "What is the capital of France?"}
    elif stage == "stream.chunk":
        req["stream"] = True
        meta = {"index": 3, "kind": "content", "bytes": 120}
        content = {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": "Par"}}]}
    elif stage == "tool_call.detected":
        meta, content = {"calls": hookcall.calls_meta(CALLS), "streamed": False}, CALLS
    elif stage == "tool.before_exec":
        meta = {"tool": "search", "call_id": "call_1", "args_bytes": 26}
        content = {"q": "capital of France"}
    elif stage == "tool.after_exec":
        meta = {"tool": "search", "call_id": "call_1", "ok": True, "result_bytes": 5,
                "duration_ms": 12.5}
        content = "Paris"
    elif stage == "response.after_engine":
        meta, content = hookcall.answer_meta(ANSWER, 200, 230, False), ANSWER
    elif stage == "request.error":
        meta = {"error": {"status": 503, "code": "503"}, "route": {"backend": "local",
                "model": "example-model"}, "attempt": 0,
                "candidates_left": [{"backend": "cloud", "model": "example-model"}]}
    elif stage == "request.finished":
        meta = {"row": {"status": 200, "finish_reason": "stop", "error_type": None,
                        "backend": "local", "backend_model": "example-model",
                        "tokens": {"prompt": 21, "completion": 2, "cached": None,
                                   "reasoning": None},
                        "cost_micros": None, "ttft_ms": None, "retries": 0,
                        "fallback_from": None},
                "labels": {}}
    elif stage == "memory.write":
        meta = {"note": {"kind": "fact", "bytes": 24}}
        content = "The user prefers Python."
    elif stage == "session.start":
        meta = {"session": "s-1", "messages": 0, "profile": None}
    elif stage == "session.compact":
        meta = {"session": "s-1", "messages": 42, "bytes": 81234}
        content = "Summarise the conversation so far."
    elif stage == "engine.lifecycle":
        req = {k: None for k in REQUEST}
        meta = {"event": "start", "host": "engine-1", "detail": {"mode": "engine"}}
    else:
        raise KeyError(stage)
    return {"request": req, "meta": meta, "content": copy.deepcopy(content)}


def envelope(stage: str, level: str = "meta", hook: str = "example-hook") -> dict:
    """A whole call body as a hook of `level` gets it at `stage`."""
    from proxy import hookstages
    s = sample(stage)
    return {"hooks": hookstages.VERSION, "id": "hd-0123456789abcdef0123456789abcdef",
            "hook": hook, "stage": stage, "ts": 1763200000.0, "payload": level, "mode": "on",
            "request": s["request"], "event": hookcall.event_at(level, s["meta"], s["content"])}
