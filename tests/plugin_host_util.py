"""What the plugin host's tests share (SRV-859): a test plugin that contributes every kind and asks
for every capability (`kitchen-sink`), a rig of the registry, the runner, the providers and the
host over a temporary config database, and a wait.

The kitchen-sink's server speaks MCP over stdio plus hooks/call, decision/ask and backend/request,
the standard library only. Its `probe` tool makes it try things from where it runs: its
environment, a direct TCP connect, a CONNECT through its HTTP proxy, a read of a path, and the
host's own requests (tandem/settings, tandem/secret, tandem/engine), so a test sees what the
plugin can and cannot do from inside its runtime."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_util import Reg, folder, write_tree  # noqa: E402,F401

SERVER = r'''
import json, os, socket, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
M = json.load(open(os.path.join(HERE, "plugin.json")))
pending = []
nid = [0]


def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def read():
    if pending:
        return pending.pop(0)
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            continue


def ask(method, params):
    nid[0] += 1
    rid = "ks-%d" % nid[0]
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            return {"error": "closed"}
        msg = json.loads(line)
        if msg.get("id") == rid and ("result" in msg or "error" in msg):
            return msg
        pending.append(msg)


def settings():
    return json.loads(os.environ.get("TANDEM_PLUGIN_SETTINGS") or "{}")


def text(t, err=False):
    return {"content": [{"type": "text", "text": t}], "isError": err}


def probe(a):
    what = a["what"]
    if what == "env":
        return text(json.dumps(sorted(os.environ)))
    if what == "getenv":
        return text(os.environ.get(a.get("name", ""), "<unset>"))
    if what in ("tandem/settings", "tandem/secret", "tandem/engine"):
        return text(json.dumps(ask(what, {k: v for k, v in a.items() if k in ("name", "plugin")})))
    if what == "connect":
        try:
            socket.create_connection((a["host"], int(a["port"])), 3).close()
            return text("connected")
        except OSError as e:
            return text("refused: " + type(e).__name__)
    if what == "proxy":
        p = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if not p:
            return text("no proxy")
        h, _, pt = p.split("//", 1)[1].rstrip("/").rpartition(":")
        try:
            s = socket.create_connection((h, int(pt)), 5)
            s.sendall(("CONNECT %s:%s HTTP/1.1\r\nHost: %s\r\n\r\n"
                       % (a["host"], a["port"], a["host"])).encode())
            s.settimeout(15)
            got = s.recv(400)
            s.close()
            return text(got.decode("latin-1"))
        except OSError as e:
            return text("refused: " + type(e).__name__)
    if what == "read":
        try:
            open(a["path"], "rb").read(1)
            return text("readable")
        except OSError as e:
            return text("unreadable: " + type(e).__name__)
    if what == "exists":
        return text(str(os.path.exists(a["path"])))
    return text("?", True)


def answer(msg):
    m, p = msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "kitchen-sink", "version": M["version"]}}
    if m == "tools/list":
        return {"tools": M["contributes"]["tools"]}
    if m == "tools/call":
        n, a = p.get("name"), p.get("arguments") or {}
        if n == "echo":
            return text(a.get("text", ""))
        if n == "write":
            return text("written")
        if n == "probe":
            return probe(a)
        raise LookupError(n)
    if m == "hooks/call":
        mode = settings().get("hook", "continue")
        if mode == "hang":
            time.sleep(3600)
        if mode == "deny":
            return {"action": "deny", "reason": "kitchen-sink says no"}
        return {"action": "continue", "labels": {"ks": p.get("hook")}}
    if m == "decision/ask":
        opts = p.get("options") or {}
        return {"label": sorted(opts)[0] if opts else "x", "confidence": 0.9}
    if m == "backend/request":
        body = p.get("body") or {}
        msgs = body.get("messages") or []
        last = (msgs[-1].get("content") if msgs else "") or ""
        if body.get("stream"):
            ch = {"id": "ks-1", "object": "chat.completion.chunk", "created": 0,
                  "model": body.get("model"), "choices": [{"index": 0, "delta": {
                      "role": "assistant", "content": "echo: " + last},
                      "finish_reason": "stop"}]}
            return {"status": 200, "events": [ch]}
        return {"status": 200, "body": {
            "id": "ks-1", "object": "chat.completion", "created": 0, "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "echo: " + last},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}}
    if m == "ping":
        return {}
    raise LookupError(m)


def main():
    tok = os.environ.get("TOKEN")
    if tok:
        sys.stderr.write("started with token %s\n" % tok)
        sys.stderr.flush()
    if settings().get("crash"):
        sys.exit(3)
    while True:
        msg = read()
        if msg is None:
            return
        if "id" not in msg or "method" not in msg:
            continue
        try:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": answer(msg)})
        except LookupError as e:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "error": {"code": -32601, "message": "no method %s" % e}})


main()
'''


def _tool(name: str, ro: bool, props: dict, required=()) -> dict:
    return {"name": name, "description": f"the test plugin's {name}",
            "inputSchema": {"type": "object", "properties": props, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": ro, "destructiveHint": not ro,
                            "idempotentHint": ro, "openWorldHint": False}}


def manifest(name: str = "kitchen-sink", hosts=(), trust: str | None = None,
             version: str = "0.1.0") -> dict:
    s = {"type": "string", "maxLength": 300}
    m = {"plugin_api": "1.0", "name": name, "version": version, "title": "Kitchen sink",
         "tandemapp": ">=0.15", "hooks_api": "1.0",
         "entry": {"command": ["python3", "server.py"]},
         "contributes": {
             "tools": [_tool("echo", True, {"text": s}, ["text"]),
                       _tool("write", False, {"text": s}),
                       _tool("probe", False, {"what": s, "host": s, "port": {"type": "integer"},
                                              "name": s, "plugin": s, "path": s}, ["what"])],
             "hooks": [{"name": "guard", "stages": ["request.received"], "role": "guard",
                        "payload": "meta"},
                       {"name": "peek", "stages": ["request.finished"], "role": "observer",
                        "payload": "full"}],
             "decision_providers": [{"name": "first", "questions": ["classify"]}],
             "backends": [{"name": "echo", "models": ["plugin-echo"]}],
             "panels": [{"name": "main", "title": "Main", "path": "panel/index.html"}],
             "skills": ["skills"]},
         "settings": {"type": "object", "additionalProperties": False, "properties": {
             "hook": {"enum": ["continue", "deny", "hang"], "default": "continue"},
             "crash": {"type": "boolean", "default": False}}},
         "secrets": [{"name": "TOKEN"}, {"name": "OTHER"}],
         "capabilities": ["prompts:meta", "prompts:full", "settings:read", "secrets:TOKEN",
                          "secrets:OTHER", "engine:control"] +
         [f"network:{h}" for h in hosts]}
    if trust:
        m["trust"] = trust
    return m


def kitchen(base: str | None = None, **kw) -> str:
    """The kitchen-sink's folder (its manifest from `manifest(**kw)`)."""
    d = tempfile.mkdtemp(prefix="plugin-ks-", dir=base)
    write_tree(d, {"plugin.json": json.dumps(manifest(**kw), indent=1),
                   "server.py": SERVER,
                   "panel/index.html": "<!doctype html><title>ks</title><script src=app.js>"
                                       "</script><p>kitchen sink</p>\n",
                   "panel/app.js": "parent.postMessage({type: 'tandem-bridge', id: 1, "
                                   "method: 'plugin.info'}, '*');\n",
                   "skills/demo/SKILL.md": "---\ndescription: a demo skill\n---\nSay demo.\n"})
    return d


def wait(fn, timeout: float = 15.0, step: float = 0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class Rig:
    """The registry (plugin_util.Reg), the providers and a host whose sandboxed runtime is a host
    process (for the wiring; tests/test_plugin_sandbox.py runs the real sandboxes)."""

    def __init__(self, label: str, sandbox=None, env: dict | None = None):
        from proxy import pluginhost, pluginrt
        from proxy import providers as providers_mod
        self.reg = Reg(label)
        self.store, self.runner, self.cfg = self.reg.store, self.reg.runner, self.reg.cfg
        self.providers = providers_mod.Providers(self.cfg, runner=self.runner)
        self.lines: list[str] = []
        self.host = pluginhost.PluginHost(self.store, runner=self.runner,
                                          providers=self.providers, cfg=self.cfg,
                                          log=self.lines.append,
                                          env=env if env is not None else
                                          {"PATH": os.environ.get("PATH", "")})
        self.host.set_runtime("sandboxed", sandbox or pluginrt.HostRuntime())
        self.host.backoff_s = (0.05,)
        self.host.unavailable_s = 0.2

    def add(self, path: str, enable: bool = True, grant=(), confirmed=(), **write) -> str:
        p, _ = self.reg.install(path)
        pid = p["id"]
        if grant or write:
            self.store.write(pid, grant=list(grant), confirmed=set(confirmed), who="admin",
                             **write)
        if enable:
            self.store.set_enabled(pid, True, "admin")
        return pid

    def running(self, pid: str, timeout: float = 20.0) -> dict:
        self.host.sync()
        st = wait(lambda: self.host.running(pid) and self.host.status(pid), timeout)
        assert st, (pid, self.host.status(pid), self.host.logs(pid, 30))
        return st

    def probe(self, pid: str, **args) -> str:
        res = self.host.tool_call(pid, "probe", args, timeout_s=30)
        return res["content"][0]["text"]

    def audit(self) -> list[dict]:
        return self.reg.audit()

    def close(self) -> None:
        self.host.close()
        self.providers.stop()
        self.reg.close()
