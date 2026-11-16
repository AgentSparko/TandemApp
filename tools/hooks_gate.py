"""The hooks' byte gate (SRV-849): a fixed set of requests through the standalone proxy in front of
the fake backend, each answer and what the backend received reduced to one digest.

    python tools/hooks_gate.py          one line per case: `<case> <sha256>`, then `cases N`

A case's digest covers the client's answer (status, every header but Date, the body) and what
the backend got (the body's bytes, every header but Host, the number of requests). Run on two
builds, equal lines mean the builds answer byte for byte alike; tests/test_hooks_relay.py runs the
same cases in one build with the hook runner absent, empty, and holding hooks that change nothing.
It uses only tests/proxy_util.py and tests/fake_backend.py, so it runs on a build without hooks.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

KEY = "sk-fixture-ABCDEFGH1234"
TOOLS = [{"type": "function", "function": {"name": "search", "parameters": {
    "type": "object", "properties": {"q": {"type": "string"}}}}}]


def _chat(text, stream=False, **kw):
    b = {"model": "fake-a", "messages": [{"role": "user", "content": text}], "stream": stream}
    b.update(kw)
    return b


#: (name, path, body, extra headers)
CASES = [
    ("chat", "/v1/chat/completions", _chat("hello"), {}),
    ("chat-stream-usage", "/v1/chat/completions",
     _chat("hello FAKE_REASONING", True, stream_options={"include_usage": True}), {}),
    ("chat-stream", "/v1/chat/completions", _chat("hello FAKE_REASONING", True), {}),
    ("chat-stream-cost", "/v1/chat/completions", _chat("hello", True), {"X-Tandem-Cost": "1"}),
    ("chat-tools-stream", "/v1/chat/completions", _chat("go FAKE_TOOL", True, tools=TOOLS), {}),
    ("chat-calls", "/v1/chat/completions",
     _chat('go FAKE_CALLS [{"name": "search", "arguments": "{\\"q\\": \\"x\\"}"}]',
           tools=TOOLS), {}),
    ("chat-deepseek", "/v1/chat/completions", _chat("hi FAKE_DEEPSEEK", True), {}),
    ("chat-params", "/v1/chat/completions",
     _chat("hi", temperature=0.7, top_p=0.9, max_tokens=33, seed=4, stop=["\n\n"]), {}),
    ("chat-unicode", "/v1/chat/completions", _chat("grüße, 東京 🙂 FAKE_SAY ok ✓"), {}),
    ("chat-agent", "/v1/chat/completions", _chat("hi"),
     {"User-Agent": "opencode/1.0 ai-sdk/5", "x-session-id": "s-gate"}),
    ("chat-500", "/v1/chat/completions", _chat("FAKE_500"), {}),
    ("completions", "/v1/completions", {"model": "fake-a", "prompt": "once upon"}, {}),
    ("completions-stream", "/v1/completions",
     {"model": "fake-a", "prompt": "once upon", "stream": True}, {}),
    ("embeddings", "/v1/embeddings", {"model": "fake-a", "input": ["a", "b"]}, {}),
    ("responses", "/v1/responses", {"model": "fake-a", "input": "hi"}, {}),
    ("responses-stream", "/v1/responses", {"model": "fake-a", "input": "hi", "stream": True}, {}),
    ("unknown-model", "/v1/chat/completions", dict(_chat("hi"), model="nope"), {}),
    ("gateway", "/v1/fleet/gateway/chat/completions", _chat("hi via the page"), {}),
]


def env_for(port: int) -> dict:
    return {"TANDEM_BACKENDS": "f1", "TANDEM_BACKEND_F1_KIND": "generic",
            "TANDEM_BACKEND_F1_URL": f"http://127.0.0.1:{port}", "TANDEM_BACKEND_F1_KEY": KEY}


def digest(px, srv, case) -> tuple[str, dict]:
    """(the case's sha256, what went into it)."""
    name, path, body, headers = case
    f = srv.fake
    before = f.requests
    f.last = None
    st, hdrs, out = px.post(path, json.dumps(body, ensure_ascii=False).encode(), headers)
    up = f.last or {}
    doc = {"status": st,
           "headers": sorted((k, v) for k, v in hdrs.items() if k.lower() != "date"),
           "body": out.decode("utf-8", "replace"),
           "upstream_raw": up.get("raw"),
           "upstream_headers": sorted((k, v) for k, v in (up.get("headers") or {}).items()
                                      if k.lower() != "host"),
           "upstream_requests": f.requests - before}
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest(), doc


def run(px, srv) -> list[tuple[str, str, dict]]:
    return [(c[0],) + digest(px, srv, c) for c in CASES]


def main() -> int:
    from proxy_util import Proxy, fake
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    try:
        with Proxy(env_for(port)) as px:
            px.sync()
            rows = run(px, srv)
    finally:
        srv.shutdown()
    for name, d, _ in rows:
        print(f"{name} {d}")
    print(f"cases {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
