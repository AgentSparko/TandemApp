"""hello-tool: an MCP server over stdio (one JSON-RPC message a line), the standard library only.

The plugin host (F2) starts it with `python3 server.py` in this folder and passes the plugin's
settings as the JSON in TANDEM_PLUGIN_SETTINGS. It answers initialize, tools/list and tools/call.
"""

from __future__ import annotations

import json
import os
import sys

TOOLS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "plugin.json")))["contributes"]["tools"]


def settings() -> dict:
    try:
        return json.loads(os.environ.get("TANDEM_PLUGIN_SETTINGS") or "{}")
    except ValueError:
        return {}


def answer(msg: dict):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "hello-tool", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call" and params.get("name") == "greet":
        args = params.get("arguments") or {}
        name = args.get("name")
        if set(args) != {"name"} or not isinstance(name, str) or not 1 <= len(name) <= 80:
            return {"isError": True, "content": [{"type": "text",
                                                  "text": "greet takes {name}: 1 to 80 characters"}]}
        s = settings()
        text = f"{s.get('greeting', 'Hello')}, {name}{'!' if s.get('excited') else '.'}"
        return {"content": [{"type": "text", "text": text}]}
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
            continue                                   # a notification
        try:
            out = {"jsonrpc": "2.0", "id": msg["id"], "result": answer(msg)}
        except LookupError as exc:
            out = {"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32601, "message": f"no method {exc}"}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
