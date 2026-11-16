"""SRV-859 (QA SRV-860) end to end: the standalone proxy with its plugin host, fake backends, and
plugins installed and enabled over its API. The plugins' sandboxed runtime is a host process here
(tests/test_plugin_sandbox.py runs the real sandboxes).

  1  No plugin enabled, the relay answers byte for byte as before (tools/hooks_gate.py's cases):
     without the runner, with plugins installed but disabled, and with one running that
     contributes no hook                                        test_no_plugin_byte_for_byte
  2  The examples end to end over HTTP: installed and enabled on /v1/fleet/plugins; hello-tool's
     greet through /chat-engine/v1/plugins/tools[/call] and chatd's MCP client (listed with its
     servers, called through the controller); audit-hook's line after a relayed request;
     classify-provider as a decision provider of kind plugin, tested live; the status, logs and
     start/stop/restart routes                                  test_examples_over_http
  3  A plugin's backend: registered in the backends store with the controller's loopback URL, a
     chat completion and a stream through the relay to the plugin, the shim refused to another
     machine; gone with the plugin                              test_backend_through_the_relay
  4  A panel over HTTP: served with the CSP sandbox, no path out of its folder, gone with the
     plugin; the bridge route enforces the capabilities         test_panel_over_http
  5  The health page: a plugin that gave up, and one without a sandbox runtime, are findings of
     the controller                                             test_health_findings

Run: python tests/test_plugin_relay.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_host_util import folder, kitchen, wait  # noqa: E402
from proxy_util import Proxy, fake  # noqa: E402

from proxy import pluginhost, pluginrt  # noqa: E402
from tools import hooks_gate  # noqa: E402

KEY = hooks_gate.KEY
BASE = "/v1/fleet/plugins"


def _env(*backs):
    env = {"TANDEM_BACKENDS": ",".join(b for b, _ in backs)}
    for bid, port in backs:
        up = bid.upper()
        env[f"TANDEM_BACKEND_{up}_KIND"] = "generic"
        env[f"TANDEM_BACKEND_{up}_URL"] = f"http://127.0.0.1:{port}"
        env[f"TANDEM_BACKEND_{up}_KEY"] = KEY
    return env


def _j(px, method, path, body=None, want=200):
    st, hdrs, out = px.request(method, path, body)
    doc = json.loads(out or b"{}")
    assert st == want, (method, path, st, doc)
    return doc


def _host(px):
    h = px.svc.plugin_host
    h.set_runtime("sandboxed", pluginrt.HostRuntime())
    h.backoff_s = (0.05,)
    h.own_url = f"http://127.0.0.1:{px.port}"
    return h


def _install(px, path, enable=True, **write):
    p = _j(px, "POST", BASE, {"source": {"type": "folder", "path": path}}, 201)["plugin"]
    if write:
        px.svc.plugins.write(p["id"], who="admin", **write)
    if enable:
        _j(px, "POST", f"{BASE}/{p['id']}/enable")
    return p["id"]


def _running(px, pid, timeout=20):
    h = px.svc.plugin_host
    h.sync()
    st = wait(lambda: h.running(pid) and h.status(pid), timeout)
    assert st, (pid, h.status(pid), h.logs(pid, 30))
    return st


def _chat(text, model="fake-a", stream=False):
    return {"model": model, "messages": [{"role": "user", "content": text}], "stream": stream}


# ---------------------------------------------------------------------------- 1
def test_no_plugin_byte_for_byte():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    srcs = [folder("hello-tool"), folder("audit-hook"), kitchen()]
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            host = _host(px)
            runner = px.svc.hooks
            px.svc.hooks = None                                    # the build without hooks
            base = {n: (d, doc) for n, d, doc in hooks_gate.run(px, srv)}
            px.svc.hooks = runner
            _install(px, srcs[1], enable=False)
            _install(px, srcs[2], enable=False)
            host.start()
            assert runner.idle() and host.statuses()
            disabled = {n: d for n, d, _ in hooks_gate.run(px, srv)}
            hid = _install(px, srcs[0])                            # running, no hook
            _running(px, hid)
            assert runner.idle()
            running = {n: d for n, d, _ in hooks_gate.run(px, srv)}
            for name, (d, doc) in base.items():
                for label, got in (("disabled", disabled), ("running", running)):
                    assert got[name] == d, (label, name, doc["status"], doc["body"][:200])
            assert len(base) == len(hooks_gate.CASES) >= 18
            return f"{len(base)} cases x 3 builds equal"
    finally:
        srv.shutdown()
        for s in srcs:
            shutil.rmtree(s)


# ---------------------------------------------------------------------------- 2
def test_examples_over_http():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    srcs = [folder("hello-tool"), folder("audit-hook"), folder("classify-provider")]
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            host = _host(px)
            hello = _install(px, srcs[0])
            audit = _install(px, srcs[1], grant=["prompts:meta"])
            clf = _install(px, srcs[2], grant=["prompts:redacted"])
            host.start()
            for pid in (hello, audit, clf):
                _running(px, pid)
            st = _j(px, "GET", f"{BASE}/{hello}/status")["status"]
            assert st["state"] == "running" and st["tools"] == ["greet"], st
            assert _j(px, "GET", f"{BASE}/{audit}/status")["status"]["hooks"] == \
                ["audit-hook/finished"]
            # the tool, as chatd reaches it
            got = _j(px, "GET", "/chat-engine/v1/plugins/tools")["plugins"]
            assert [(p["id"], [t["name"] for t in p["tools"]]) for p in got] == \
                [("hello-tool", ["greet"])], got
            out = _j(px, "POST", "/chat-engine/v1/plugins/tools/call",
                     {"plugin": "hello-tool", "tool": "greet", "arguments": {"name": "Ada"}})
            assert out["result"]["content"][0]["text"] == "Hello, Ada.", out
            err = _j(px, "POST", "/chat-engine/v1/plugins/tools/call",
                     {"plugin": "hello-tool", "tool": "greet", "arguments": {"who": "x"}}, 400)
            assert err["error"]["type"] == "bad_arguments"
            _j(px, "POST", "/chat-engine/v1/plugins/tools/call",
               {"plugin": "audit-hook", "tool": "greet", "arguments": {}}, 404)
            from chatd.engine import Engine
            from chatd.mcp import Mcp

            class Svc:
                store = None
                engine = Engine(f"http://127.0.0.1:{px.port}/chat-engine")

                def log(self, m):
                    pass
            m = Mcp(Svc(), px.dir)
            listed = m.plugins(fresh=True)
            assert [p["id"] for p in listed] == ["hello-tool"]
            pub = m._plugin_public(listed[0])
            assert pub["id"] == "plugin:hello-tool" and pub["transport"] == "plugin" and \
                pub["read_only"] and pub["tools"][0]["name"] == "greet"
            tools = m._plugin_tools()
            assert [t.name for t in tools] == ["mcp__plugin_hello_tool__greet"]
            assert tools[0].source == {"kind": "plugin", "id": "hello-tool"}
            res = tools[0].executor(None, {"name": "Bo"})
            assert res.content == "Hello, Bo." and not res.is_error, res.content
            # the hook: a relayed request, then the audit line
            st, _, _ = px.post("/v1/chat/completions", _chat("hello"))
            assert st == 200
            px.rows()
            px.svc.hooks.bg.drain(10)
            log = os.path.join(px.svc.plugin_host.state_dir, "plugin-data", audit, "audit.jsonl")
            line = wait(lambda: os.path.isfile(log) and open(log).read().strip(), 10)
            assert line and json.loads(line.splitlines()[-1])["model"] in ("fake-a", None), line
            calls = px.svc.hooks.calls("audit-hook/finished")
            assert calls and calls[0]["outcome"] == "ok", calls
            # the provider, as a decision provider of kind plugin
            p = _j(px, "POST", "/v1/fleet/decision-providers",
                   {"name": "keywords", "kind": "plugin", "mode": "on",
                    "target": "classify-provider/keywords", "questions": {"classify": {}}},
                   201)["provider"]
            assert (p["kind"], p["target"]) == ("plugin", "classify-provider/keywords")
            t = _j(px, "POST", f"/v1/fleet/decision-providers/{p['id']}/test",
                   {"question": "classify", "text": "hello and thanks"})
            assert t["ok"] and t["answer"]["label"] == "chat", t
            h = _j(px, "GET", f"/v1/fleet/decision-providers/{p['id']}/health")
            assert h["status"] == "ok", h
            # logs and the admin's start, stop, restart
            lines = _j(px, "GET", f"{BASE}/{hello}/logs?tail=50")["lines"]
            assert any("[host] started 0.1.0" in ln for ln in lines), lines
            st = _j(px, "POST", f"{BASE}/{hello}/stop", {})["status"]
            assert st["state"] == "stopped" and st["held"]
            assert _j(px, "GET", "/chat-engine/v1/plugins/tools")["plugins"] == []
            _j(px, "POST", f"{BASE}/{hello}/start", {})
            _running(px, hello)
            _j(px, "POST", f"{BASE}/{hello}/restart", {})
            _running(px, hello)
            _j(px, "GET", f"{BASE}/{hello}/logs?tail=x", want=400)
            _j(px, "POST", f"{BASE}/nope/start", {}, 404)
    finally:
        srv.shutdown()
        for s in srcs:
            shutil.rmtree(s)
    return "greet over HTTP and chatd, the audit line, the classify answer, the admin routes"


# ---------------------------------------------------------------------------- 3
def test_backend_through_the_relay():
    srv, port = fake(models=["fake-a"], key=KEY, cache=False)
    src = kitchen()
    try:
        with Proxy(_env(("f1", port))) as px:
            px.sync()
            host = _host(px)
            pid = _install(px, src, grant=["prompts:meta"])
            host.start()
            st = _running(px, pid)
            bid = pluginhost.backend_id(pid, "echo")
            assert st["backends"] == [bid]
            b = px.svc.cfg.backend(bid)
            assert b["url"] == f"http://127.0.0.1:{px.port}/plugin-backends/kitchen-sink/echo"
            assert b["models"] == ["plugin-echo"] and "(plugin kitchen-sink)" in b["label"]
            px.sync()
            st_, _, out = px.post("/v1/chat/completions", _chat("hi there", "plugin-echo"))
            doc = json.loads(out)
            assert st_ == 200 and doc["choices"][0]["message"]["content"] == "echo: hi there", \
                (st_, doc)
            st_, _, out = px.post("/v1/chat/completions", _chat("streamed", "plugin-echo", True))
            assert st_ == 200 and b"echo: streamed" in out and b"[DONE]" in out, out[:300]
            # the shim answers this machine only
            from proxy import pluginweb

            class H:
                sent = None

                def _peer(self):
                    return "192.0.2.9"

                def _json(self, code, body, extra_headers=()):
                    H.sent = (code, body)
            pluginweb.backend(H(), px.svc, "POST", "/plugin-backends/kitchen-sink/echo/v1/"
                              "chat/completions", b"{}")
            assert H.sent[0] == 403, H.sent
            # gone with the plugin
            _j(px, "POST", f"{BASE}/{pid}/disable")
            host.sync()
            assert px.svc.cfg.backend(bid) is None
            st_, _, _ = px.post("/v1/chat/completions", _chat("hi", "plugin-echo"))
            assert st_ == 404, st_
    finally:
        srv.shutdown()
        shutil.rmtree(src)
    return "a completion and a stream through the relay to the plugin; gone with it"


# ---------------------------------------------------------------------------- 4
def test_panel_over_http():
    src = kitchen()
    try:
        with Proxy({}) as px:
            host = _host(px)
            pid = _install(px, src, grant=["prompts:meta"])
            host.start()
            _running(px, pid)
            st, hdrs, out = px.request("GET", "/plugin-panels/kitchen-sink/main/")
            assert st == 200 and b"kitchen sink" in out, (st, out[:200])
            csp = hdrs.get("Content-Security-Policy", "")
            assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
            assert "connect-src 'none'" in csp and hdrs.get("X-Content-Type-Options") == "nosniff"
            st, hdrs, out = px.request("GET", "/plugin-panels/kitchen-sink/main/app.js")
            ct = hdrs.get("Content-Type", "").split(";")[0]
            assert st == 200 and b"tandem-bridge" in out and \
                ct in ("text/javascript", "application/javascript"), (st, hdrs)
            for bad in ("/plugin-panels/kitchen-sink/main/%2e%2e/plugin.json",
                        "/plugin-panels/kitchen-sink/main/..%2fserver.py",
                        "/plugin-panels/kitchen-sink/nope/",
                        "/plugin-panels/other/main/"):
                st, _, _ = px.request("GET", bad)
                assert st == 404, (bad, st)
            panels = _j(px, "GET", f"{BASE}/{pid}/panels")["panels"]
            assert panels[0]["bridge"]["methods"] == ["plugin.info", "plugin.status", "tools.call"]
            got = _j(px, "POST", f"{BASE}/{pid}/bridge", {"method": "plugin.info"})
            assert got["result"]["id"] == pid
            err = _j(px, "POST", f"{BASE}/{pid}/bridge", {"method": "settings.read"}, 403)
            assert err["error"]["type"] == "refused"
            err = _j(px, "POST", f"{BASE}/{pid}/bridge",
                     {"method": "tools.call", "params": {"tool": "write"}}, 403)
            assert err["error"]["type"] == "refused"
            _j(px, "POST", f"{BASE}/{pid}/disable")
            host.sync()
            st, _, _ = px.request("GET", "/plugin-panels/kitchen-sink/main/")
            assert st == 404
            assert _j(px, "GET", f"{BASE}/{pid}/panels")["panels"] == []
    finally:
        shutil.rmtree(src)
    return "CSP sandbox, its folder only, the bridge per capability, gone when disabled"


# ---------------------------------------------------------------------------- 5
def test_health_findings():
    src = kitchen()
    plain = kitchen(name="plain-sink")
    try:
        with Proxy({}) as px:
            host = _host(px)
            pid = _install(px, src, settings_set={"crash": True})
            host.start()
            assert wait(lambda: host.status(pid)["state"] == "failed", 30), host.status(pid)
            host._sandbox, host._sandbox_cfg = None, {"backend": "off",
                                                       "why": "no container runtime socket"}
            qid = _install(px, plain)
            host.sync()
            assert wait(lambda: host.status(qid)["state"] == "unavailable", 10)
            time.sleep(0.2)
            doc = _j(px, "GET", "/v1/fleet/health/controller?fresh=1")
            target = doc.get("target", doc)
            codes = {(f["code"], f["facts"].get("plugin")) for f in target["findings"]}
            assert ("plugin_failed", pid) in codes and ("plugin_no_sandbox", qid) in codes, \
                target["findings"]
            chk = {c["check"]: c for c in target["checks"]}
            assert chk["plugins"]["ok"] is False, chk
            assert _j(px, "GET", f"{BASE}/{pid}")["plugin"]["enabled"] is False
    finally:
        shutil.rmtree(src)
        shutil.rmtree(plain)
    return "plugin_failed and plugin_no_sandbox on the controller's health"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
