"""classify-provider: an MCP server over stdio that answers `decision/ask` for the classify question.

The plugin host sends `decision/ask` with {provider, question, options, input} (the input at the
payload level granted: redacted here) and expects {label, confidence?}. The keywords come from the
plugin's settings (TANDEM_PLUGIN_SETTINGS), each class's list from its schema default when unset.
"""

from __future__ import annotations

import json
import os
import sys


SCHEMA = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "plugin.json")))["settings"]


def settings() -> dict:
    try:
        s = json.loads(os.environ.get("TANDEM_PLUGIN_SETTINGS") or "{}")
    except ValueError:
        s = {}
    classes = dict(s.get("classes") or {})
    for label, sub in SCHEMA["properties"]["classes"]["properties"].items():
        classes.setdefault(label, sub.get("default") or [])
    s["classes"] = classes
    return s


def classify(text: str, options: dict) -> dict:
    s = settings()
    low = (text or "").lower()
    for label, words in s["classes"].items():
        if (not options or label in options) and any(w.lower() in low for w in words):
            return {"label": label, "confidence": 0.6}
    return {"label": s.get("fallback", "unclear"), "confidence": 0.0}


def answer(msg: dict):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "classify-provider", "version": "0.1.0"}}
    if method == "decision/ask" and params.get("question") == "classify":
        return classify((params.get("input") or {}).get("text", ""), params.get("options") or {})
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
