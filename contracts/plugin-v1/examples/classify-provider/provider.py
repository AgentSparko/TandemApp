"""classify-provider: an MCP server over stdio that answers `decision/ask` for the classify question.

The plugin host (F2) sends `decision/ask` with {provider, question, input} (the input at the
payload level granted: redacted here) and expects {label, confidence?}. The keywords come from the
plugin's settings (TANDEM_PLUGIN_SETTINGS).
"""

from __future__ import annotations

import json
import os
import sys


def settings() -> dict:
    try:
        return json.loads(os.environ.get("TANDEM_PLUGIN_SETTINGS") or "{}")
    except ValueError:
        return {}


def classify(text: str) -> dict:
    s = settings()
    low = (text or "").lower()
    for label, words in (s.get("classes") or {}).items():
        if any(w.lower() in low for w in words):
            return {"label": label, "confidence": 0.6}
    return {"label": s.get("fallback", "unclear"), "confidence": 0.0}


def answer(msg: dict):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "classify-provider", "version": "0.1.0"}}
    if method == "decision/ask" and params.get("question") == "classify":
        return classify((params.get("input") or {}).get("text", ""))
    if method == "ping":
        return {}
    raise LookupError(method)


def main() -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        try:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": answer(msg)}
        except LookupError as exc:
            out = {"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32601, "message": f"no method {exc}"}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
