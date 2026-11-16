"""audit-hook: an MCP server over stdio that answers the hook calls of its observer hook.

The plugin host (F2) sends `hooks/call` with {hook, stage, event} (the event as hooks-v1 has it, at
the payload level granted: meta here) and expects a hooks-v1 result. An observer answers
{"action": "continue"} and nothing it says changes the request.
"""

from __future__ import annotations

import json
import os
import sys
import time

LOG = os.path.join(os.environ.get("TANDEM_PLUGIN_DATA") or ".", "audit.jsonl")


def settings() -> dict:
    try:
        return json.loads(os.environ.get("TANDEM_PLUGIN_SETTINGS") or "{}")
    except ValueError:
        return {}


def answer(msg: dict):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "audit-hook", "version": "0.1.0"}}
    if method == "hooks/call":
        ev = params.get("event") or {}
        keep = settings().get("fields") or ["id", "model", "tokens", "ms"]
        line = {"at": int(time.time())}
        line.update({k: ev.get(k) for k in keep})
        with open(LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
        return {"action": "continue"}
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
