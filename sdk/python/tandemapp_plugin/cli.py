"""`tandemapp-plugin`: start, vendor and check a plugin written with tandemapp_plugin (SRV-861).

    tandemapp-plugin new NAME [--dir PARENT] [--kind tool|hook|provider] [--trust sandboxed|host]
        a new plugin folder PARENT/NAME: plugin.json, main.py and a copy of this SDK; refused
        when the folder exists
    tandemapp-plugin vendor FOLDER
        copy this SDK into FOLDER/tandemapp_plugin (a sandboxed plugin sees only its own tree)
    tandemapp-plugin check FOLDER
        the manifest's tools against the four annotations and strict input schemas, and the copy
        of the SDK against this one

Also `python -m tandemapp_plugin ...`. Exit 0 when done or clean, 1 when a check fails, 2 for a
usage error. The controller's own check of a whole plugin is the TandemApp repository's
tools/plugin_check.py (or POST /v1/fleet/plugins/inspect).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

from tandemapp_plugin import (HOOKS_API, PLUGIN_API, SDK_VERSION, validate_tool)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
PKG = os.path.dirname(os.path.abspath(__file__))
#: what a plugin's copy of the SDK holds: the runtime only
VENDORED = ("__init__.py",)

_TOOL = {
    "name": "echo",
    "description": "Answer with the text it was given.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 2000}},
                    "required": ["text"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                    "openWorldHint": False},
}

MAIN = {
    "tool": '''"""{name}: a TandemApp plugin with one tool (tandemapp_plugin, standard library only)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin  # noqa: E402

plugin = Plugin()


@plugin.tool("echo", description="Answer with the text it was given.",
             input_schema={{"type": "object",
                           "properties": {{"text": {{"type": "string", "maxLength": 2000}}}},
                           "required": ["text"], "additionalProperties": False}},
             read_only=True, destructive=False, idempotent=True, open_world=False)
def echo(args, ctx):
    return plugin.settings.get("prefix", "") + args["text"]


if __name__ == "__main__":
    plugin.main()
''',
    "hook": '''"""{name}: a TandemApp plugin with one observer hook (tandemapp_plugin)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin  # noqa: E402

plugin = Plugin()


@plugin.hook("finished")
def finished(call, ctx):
    # an observer: what it answers never changes the request
    with open(os.path.join(plugin.data_dir, "finished.jsonl"), "a") as f:
        f.write(json.dumps({{k: call.event.get(k) for k in ("id", "model", "tokens", "ms")}})
                + "\\n")
    return {{"action": "continue"}}


if __name__ == "__main__":
    plugin.main()
''',
    "provider": '''"""{name}: a TandemApp decision provider for the classify question (tandemapp_plugin)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin  # noqa: E402

plugin = Plugin()


@plugin.decision("answer", question="classify")
def classify(ask, ctx):
    text = (ask.text or "").lower()
    label = "code" if "traceback" in text and "code" in ask.options else None
    return {{"label": label or sorted(ask.options)[0], "confidence": 0.5}}


if __name__ == "__main__":
    plugin.main()
''',
}


def manifest_for(name: str, kind: str, trust: str) -> dict:
    m = {"plugin_api": PLUGIN_API, "name": name, "version": "0.1.0", "title": name,
         "description": f"A {kind} plugin made with tandemapp_plugin {SDK_VERSION}.",
         "tandemapp": ">=0.15", "hooks_api": HOOKS_API,
         "entry": {"command": ["python3", "main.py"]}}
    if trust != "sandboxed":
        m["trust"] = trust
    if kind == "tool":
        m["contributes"] = {"tools": [_TOOL]}
        m["settings"] = {"type": "object", "additionalProperties": False,
                         "properties": {"prefix": {"type": "string", "maxLength": 40,
                                                   "default": "",
                                                   "description": "put before each answer"}}}
    elif kind == "hook":
        m["contributes"] = {"hooks": [{"name": "finished", "stages": ["request.finished"],
                                       "role": "observer", "payload": "meta"}]}
        m["capabilities"] = ["prompts:meta"]
    else:
        m["contributes"] = {"decision_providers": [{"name": "answer",
                                                    "questions": ["classify"]}]}
        m["capabilities"] = ["prompts:redacted"]
    return m


def vendor(folder: str) -> str:
    """Copy the SDK's runtime into FOLDER/tandemapp_plugin; the copy's path."""
    dst = os.path.join(folder, "tandemapp_plugin")
    os.makedirs(dst, exist_ok=True)
    for n in VENDORED:
        shutil.copyfile(os.path.join(PKG, n), os.path.join(dst, n))
    return dst


def stale(folder: str) -> list[str]:
    """The files of FOLDER's copy of the SDK that differ from this one."""
    out = []
    for n in VENDORED:
        mine, theirs = os.path.join(PKG, n), os.path.join(folder, "tandemapp_plugin", n)
        if not os.path.isfile(theirs):
            out.append(f"tandemapp_plugin/{n} is missing: run tandemapp-plugin vendor {folder}")
        elif open(mine, "rb").read() != open(theirs, "rb").read():
            out.append(f"tandemapp_plugin/{n} differs from SDK {SDK_VERSION}: run "
                       f"tandemapp-plugin vendor {folder}")
    return out


def new(name: str, parent: str, kind: str, trust: str) -> int:
    if not NAME_RE.match(name):
        print("NAME: lowercase letters, digits and dashes, at most 48", file=sys.stderr)
        return 2
    d = os.path.join(parent, name)
    if os.path.exists(d):
        print(f"{d} exists: new never writes into a folder that is there", file=sys.stderr)
        return 2
    os.makedirs(d)
    with open(os.path.join(d, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump(manifest_for(name, kind, trust), f, indent=1)
        f.write("\n")
    with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
        f.write(MAIN[kind].format(name=name))
    vendor(d)
    print(f"made {d}: plugin.json, main.py, tandemapp_plugin/")
    return 0


def check(folder: str) -> int:
    try:
        with open(os.path.join(folder, "plugin.json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"plugin.json: {exc}", file=sys.stderr)
        return 1
    probs = []
    for t in (m.get("contributes") or {}).get("tools") or []:
        probs += validate_tool(t)
    probs += stale(folder)
    for p in probs:
        print(p)
    if not probs:
        print(f"ok {m.get('name')} {m.get('version')}")
    return 1 if probs else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tandemapp-plugin", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="start a plugin folder")
    n.add_argument("name")
    n.add_argument("--dir", default=".")
    n.add_argument("--kind", choices=sorted(MAIN), default="tool")
    n.add_argument("--trust", choices=("sandboxed", "host"), default="sandboxed")
    v = sub.add_parser("vendor", help="copy this SDK into a plugin folder")
    v.add_argument("folder")
    c = sub.add_parser("check", help="check a plugin's tools and its copy of the SDK")
    c.add_argument("folder")
    a = ap.parse_args(argv)
    if a.cmd == "new":
        return new(a.name, a.dir, a.kind, a.trust)
    if not os.path.isdir(a.folder):
        print(f"no folder {a.folder}", file=sys.stderr)
        return 2
    if a.cmd == "vendor":
        print(f"vendored into {vendor(a.folder)}")
        return 0
    return check(a.folder)


if __name__ == "__main__":
    sys.exit(main())
