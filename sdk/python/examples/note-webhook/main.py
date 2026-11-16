"""note-webhook: a TandemApp plugin built on tandemapp_plugin (SRV-861).

An observer hook at request.finished posts a note for each finished request (meta payload: its
id, model, tokens and time), and the tool `send_note` posts any note a chat asks for. The webhook
is the `url` setting; WEBHOOK_TOKEN, when granted and set, goes as the bearer. In a sandbox the
post goes through the egress proxy (urllib honours HTTPS_PROXY), which passes only the hosts of
the granted network: capabilities: change network:hooks.example.com to the webhook's host.
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin, Refused, ToolError  # noqa: E402

plugin = Plugin()


def token():
    try:
        return plugin.secret("WEBHOOK_TOKEN")
    except Refused:
        return None                             # not granted: the note goes without a bearer


def post(note: dict) -> int:
    s = plugin.settings
    body = json.dumps({"plugin": plugin.name, "at": int(time.time()), "note": note}).encode()
    req = urllib.request.Request(s.get("url"), data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t = token()
    if t:
        req.add_header("Authorization", f"Bearer {t}")
    with urllib.request.urlopen(req, timeout=float(s.get("timeout_s", 3))) as r:
        return r.status


@plugin.tool("send_note", description="Send a short note to the team's webhook.",
             input_schema={"type": "object",
                           "properties": {"text": {"type": "string", "minLength": 1,
                                                   "maxLength": 2000}},
                           "required": ["text"], "additionalProperties": False},
             read_only=False, destructive=False, idempotent=False, open_world=True)
def send_note(args, ctx):
    try:
        status = post({"text": args["text"]})
    except (OSError, ValueError) as exc:
        raise ToolError(f"the webhook did not take the note: {exc}")
    return f"sent ({status})"


@plugin.hook("finished")
def finished(call, ctx):
    if plugin.settings.get("on_finished", True):
        ev = call.event
        try:
            post({k: ev.get(k) for k in ("id", "model", "tokens", "ms")})
        except (OSError, ValueError) as exc:
            plugin.log(f"the webhook did not take a note: {exc}")
    return {"action": "continue"}                # an observer never changes the request


if __name__ == "__main__":
    plugin.main()
