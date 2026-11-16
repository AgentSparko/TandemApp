"""SRV-859 (QA SRV-860): the plugin host (proxy/pluginhost.py) over a temporary config database,
its sandboxed runtime a host process here (the real sandboxes: tests/test_plugin_sandbox.py; the
relay end to end: tests/test_plugin_relay.py).

  Gherkin and the acceptance criteria:
  1  Enable registers contributions: an installed plugin with a hook and a tool, enabled, runs as
     one process speaking MCP over stdio; its hook is in the runner and its tool is offered to
     chatd; every kind (tools, hooks, providers, backends, skills, panels) registers on enable
     and is gone on disable                                     test_enable_registers_every_kind
  2  The three examples run: hello-tool's tool, audit-hook's hooks/call through the runner,
     classify-provider's decision/ask through the providers     test_the_examples_run
  3  Ungranted capability: a plugin that asks for a secret, its settings or engine control it
     was not granted is refused and journaled (status, audit); its environment holds only its
     grants, never the controller's; another plugin's secret is never given
                                                                test_ungranted_capability_refused
  4  Payload levels: a hook whose payload is above the granted prompts: level is not registered;
     a registry hook of kind plugin above it is refused at call time and journaled; a registry
     hook of kind plugin takes the contributed one's place     test_payload_level_enforced
  5  Crash loop: a plugin that exits at start five times is restarted with back-off, then
     disabled (audited) and a finding names it; enabled again it runs
                                                                test_crash_loop_disables
  6  A hand stop holds; start and restart; a changed setting restarts it; uninstall while
     running ends the process first                             test_supervision
  7  Trust: host only with its grant (second confirm), engine refused 'not available yet', no
     sandbox runtime: unavailable with a finding, not a failure
                                                                test_trust_levels
  8  A plugin that hangs on hooks/call: the runner's timeout and breaker  test_hang_opens_breaker
  9  Panels and the bridge: a panel only of a running plugin, no path out of its folder; the
     bridge gives only what the capabilities allow, the rest refused and journaled
                                                                test_panels_and_bridge
 10  Logs: stderr captured, size-capped, every secret replaced  test_logs_redacted_and_capped

Run: python tests/test_plugin_host.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_host_util import Rig, folder, kitchen, wait  # noqa: E402
from plugin_util import example  # noqa: E402

from proxy import hooks as hooks_mod  # noqa: E402
from proxy import pluginhost, pluginrt  # noqa: E402
from proxy.pluginstore import PluginError  # noqa: E402

CONFIRM = ("prompts:full", "engine:control")


def _ctx(**kw):
    c = {k: None for k in hooks_mod.CTX_KEYS}
    c.update(request_id="r-1", model="m", endpoint="/v1/chat/completions")
    c.update(kw)
    return c


# ---------------------------------------------------------------------------- 1
def test_enable_registers_every_kind():
    r = Rig("enable")
    src = kitchen()
    try:
        from tandemapp.conductor.packages import Library
        lib = Library()
        r.host.library = lambda: lib
        r.host.own_url = "http://127.0.0.1:9"
        pid = r.add(src, enable=False, grant=["prompts:meta", "secrets:TOKEN"])
        assert r.runner.idle() and r.host.tools_view() == []
        r.store.set_enabled(pid, True, "admin")
        st = r.running(pid)
        assert st["runtime"] == "host" and st["where"].startswith("pid ")
        assert st["tools"] == ["echo", "write", "probe"], st
        assert st["hooks"] == ["kitchen-sink/guard"], st                 # peek needs full
        assert st["providers"] == ["kitchen-sink/first"]
        bid = pluginhost.backend_id(pid, "echo")
        assert st["backends"] == [bid] and r.cfg.backend(bid)["url"] == \
            "http://127.0.0.1:9/plugin-backends/kitchen-sink/echo"
        assert "(plugin kitchen-sink)" in r.cfg.backend(bid)["label"]
        assert st["skills"] == ["skills"] and st["panels"] == ["main"]
        # the hook is in the runner, the tool is offered to chatd, the provider to the conductor
        assert [h["id"] for h in r.runner.hooks()] == ["kitchen-sink/guard"]
        assert not r.runner.idle()
        assert [t["name"] for t in r.host.tools_view()[0]["tools"]] == ["echo", "write", "probe"]
        assert [p["id"] for p in r.providers.rows()] == ["kitchen-sink/first"]
        assert r.providers.rows()[0]["mode"] == "shadow"
        r.host.sync()                                                    # the Library's seam
        assert any(d.endswith(os.sep + "skills") for d in lib._dirs({"skill_dirs": []}))
        assert [s["name"] for s in lib.scan({"skill_dirs": []})] == ["demo"]
        assert r.host.tool_call(pid, "echo", {"text": "hi"})["content"][0]["text"] == "hi"
        # disable: every kind gone, the process ended
        proc = r.host.plugins[pid].pipe.proc
        r.store.set_enabled(pid, False, "admin")
        r.host.sync()
        assert not proc.alive
        assert r.runner.idle() and r.host.tools_view() == [] and r.providers.rows() == []
        assert r.cfg.backend(bid) is None and lib._dirs({"skill_dirs": []}) == []
        assert r.host.status(pid)["state"] == "disabled"
        try:
            r.host.tool_call(pid, "echo", {"text": "x"})
            raise AssertionError("a stopped plugin's tool answered")
        except PluginError as exc:
            assert exc.code == "plugin_down"
    finally:
        r.close()
        shutil.rmtree(src)
    return "tools, hooks, providers, backends, skills, panels on enable; all gone on disable"


# ---------------------------------------------------------------------------- 2
def test_the_examples_run():
    r = Rig("examples")
    srcs = [folder(n) for n in ("hello-tool", "audit-hook", "classify-provider")]
    try:
        hello = r.add(srcs[0], settings_set={"excited": True})
        audit = r.add(srcs[1], grant=["prompts:meta"])
        clf = r.add(srcs[2], grant=["prompts:redacted"])
        for pid in (hello, audit, clf):
            r.running(pid)
        got = r.host.tool_call(hello, "greet", {"name": "Ada"})
        assert got["content"][0]["text"] == "Hello, Ada!", got
        try:
            r.host.tool_call(hello, "greet", {"name": "Ada", "x": 1})
            raise AssertionError("an argument outside the schema went through")
        except PluginError as exc:
            assert exc.code == "bad_arguments"
        # audit-hook: an observer at request.finished, called through the runner
        r.runner.notify("request.finished", _ctx(), {"id": "r-1", "model": "m", "tokens": 7,
                                                     "ms": 12})
        r.runner.bg.drain(10)
        log = os.path.join(r.host.state_dir, "plugin-data", audit, "audit.jsonl")
        line = wait(lambda: os.path.isfile(log) and open(log).read().strip(), 10)
        row = json.loads(line.splitlines()[-1])
        assert (row["id"], row["model"], row["tokens"], row["ms"]) == ("r-1", "m", 7, 12), row
        calls = r.runner.calls("audit-hook/finished")
        assert calls and calls[0]["outcome"] == "ok", calls
        # classify-provider: decision/ask, as a registry provider of kind plugin, mode on
        p = r.providers.store.create({"name": "kw", "kind": "plugin",
                                      "target": "classify-provider/keywords", "mode": "on",
                                      "questions": {"classify": {}}}, "admin")
        assert p["target"] == "classify-provider/keywords"
        opts = {"code": "write code", "chat": "talk", "unclear": "?"}
        rep = r.providers.ask(r.providers.row(p["id"]), "classify", "there is a traceback",
                              "which class?", opts)
        assert (rep.error, rep.label, rep.model) == (None, "code",
                                                     "plugin:classify-provider/keywords"), \
            rep.view()
        assert r.providers.rows()[0]["id"] == p["id"]          # it took the contributed one's
        assert [x["id"] for x in r.providers.rows()] == [p["id"]]
        h = r.providers.health(p["id"])
        assert h["status"] == "ok", h
        for bad, field in (({"target": "classify-provider/nope"}, "target"),
                           ({"target": "classify-provider/keywords",
                             "questions": {"judge": {}}}, "questions")):
            doc = dict({"name": "x", "kind": "plugin", "questions": {"classify": {}}}, **bad)
            try:
                r.providers.store.create(doc, "admin")
                raise AssertionError(bad)
            except Exception as exc:                               # noqa: BLE001
                assert getattr(exc, "field", None) == field, (bad, exc)
    finally:
        r.close()
        for s in srcs:
            shutil.rmtree(s)
    return "greet, the audit line, the classify answer"


# ---------------------------------------------------------------------------- 3
def test_ungranted_capability_refused():
    env = {"PATH": os.environ.get("PATH", ""), "TANDEM_ADMIN_TOKEN": "adm-canary-1234567890",
           "QSE_MASTER_KEY": "mk-canary"}
    r = Rig("caps", env=env)
    src = kitchen()
    other = kitchen(name="other-sink")
    try:
        pid = r.add(src, grant=["prompts:meta", "secrets:TOKEN"],
                    secrets_set={"TOKEN": "tok-secret-1234", "OTHER": "other-secret-5678"})
        oid = r.add(other, grant=["prompts:meta", "secrets:TOKEN"],
                    secrets_set={"TOKEN": "theirs-9999"})
        r.running(pid)
        r.running(oid)
        names = json.loads(r.probe(pid, what="env"))
        assert "TOKEN" in names and "OTHER" not in names, names
        for k in ("TANDEM_ADMIN_TOKEN", "QSE_MASTER_KEY"):
            assert k not in names, k
        assert {"TANDEM_PLUGIN", "TANDEM_PLUGIN_SETTINGS", "TANDEM_PLUGIN_DATA",
                "TANDEM_PLUGIN_CAPABILITIES"} <= set(names)
        assert r.probe(pid, what="getenv", name="TOKEN") == "tok-secret-1234"
        before = len(r.host.status(pid)["refusals"])
        got = json.loads(r.probe(pid, what="tandem/secret", name="TOKEN"))
        assert got["result"]["value"] == "tok-secret-1234", got
        for args, cap in ((dict(name="OTHER"), "secrets:OTHER"),
                          (dict(name="TOKEN", plugin="other-sink"), "secrets:other-sink/TOKEN"),
                          (dict(), "settings:read"),
                          (dict(), "engine:control")):
            what = "tandem/settings" if cap == "settings:read" else \
                "tandem/engine" if cap == "engine:control" else "tandem/secret"
            got = json.loads(r.probe(pid, what=what, **args))
            assert got["error"]["code"] == -32001 and got["error"]["data"]["capability"] == cap, \
                (args, got)
            assert "secret-" not in json.dumps(got) and "theirs" not in json.dumps(got)
        st = r.host.status(pid)
        assert [x["capability"] for x in st["refusals"][before:]] == \
            ["secrets:OTHER", "secrets:other-sink/TOKEN", "settings:read", "engine:control"]
        audit = [a for a in r.audit() if a["action"] == "refused"]
        mine = [a for a in audit if a["who"] == "plugin:kitchen-sink"]
        assert len(mine) >= 4 and {a["who"] for a in audit} <= {"plugin:kitchen-sink",
                                                                 "plugin:other-sink"}, audit
        assert all(a["object_id"] == a["who"].split(":", 1)[1] for a in audit), audit
        assert not any("tok-secret" in json.dumps(a) for a in r.audit())
        # granted: settings:read gives its own settings only
        r.store.write(pid, grant=["settings:read"], who="admin")
        r.running(pid)
        wait(lambda: r.host.status(pid)["state"] == "running" and
             "settings:read" in r.host.plugins[pid].granted, 10)
        got = json.loads(r.probe(pid, what="tandem/settings"))
        assert got["result"]["settings"] == {"hook": "continue", "crash": False}, got
    finally:
        r.close()
        shutil.rmtree(src)
        shutil.rmtree(other)
    return "secrets, settings, engine control and another plugin's secret refused, journaled"


# ---------------------------------------------------------------------------- 4
def test_payload_level_enforced():
    r = Rig("payload")
    src = kitchen()
    try:
        pid = r.add(src, grant=["prompts:meta"])
        st = r.running(pid)
        assert st["hooks"] == ["kitchen-sink/guard"]
        assert any("peek: payload full needs prompts:full" in n for n in st["notes"]), st
        assert any(x["capability"] == "prompts:full" for x in st["refusals"])
        # a registry hook of kind plugin, its own priority and scope, in place of the contributed
        h, _ = r.runner.store.create({"name": "guard here", "kind": "plugin",
                                      "target": "kitchen-sink/guard", "role": "guard",
                                      "stages": ["request.received"], "priority": 5,
                                      "scope": {"models": ["m*"]}}, "admin")
        hs = r.runner.hooks()
        assert [x["id"] for x in hs] == [h["id"]], hs
        out = r.runner.run("request.received", _ctx(model="m1"), {"model": "m1"},
                           lambda v: {"model": v.get("model")})
        assert out.deny is None and out.labels == {"ks": "guard"}, (out.deny, out.labels)
        # the plugin says deny when its setting says so (a restart with the new setting)
        r.store.write(pid, settings_set={"hook": "deny"}, who="admin")
        r.running(pid)
        wait(lambda: r.host.plugins[pid].info.get("settings", {}).get("hook") == "deny", 10)
        out = r.runner.run("request.received", _ctx(model="m1"), {"model": "m1"},
                           lambda v: {"model": v.get("model")})
        assert out.deny and out.deny["message"] == "kitchen-sink says no", out.deny
        # a registry hook above the grant: refused at call time, journaled
        h2, _ = r.runner.store.create({"name": "peek full", "kind": "plugin",
                                       "target": "kitchen-sink/peek", "role": "observer",
                                       "stages": ["request.finished"], "payload": "full"},
                                      "admin")
        c = r.runner.call(r.runner.store.get(h2["id"]), "request.finished", _ctx(), {}, None,
                          timeout_ms=2000, mode="on")
        assert c.outcome == "error" and "prompts:full" in c.error, (c.outcome, c.error)
        assert r.host.status(pid)["refusals"][-1]["capability"] == "prompts:full"
        for doc, field in (({"target": "kitchen-sink/guard", "role": "observer",
                             "stages": ["request.received"]}, "role"),
                           ({"target": "kitchen-sink/guard", "role": "guard",
                             "stages": ["request.before_engine"]}, "stages"),
                           ({"target": "kitchen-sink/guard", "role": "guard",
                             "stages": ["request.received"], "payload": "redacted"},
                            "payload")):
            try:
                r.runner.store.create(dict({"name": "x", "kind": "plugin"}, **doc), "admin")
                raise AssertionError(doc)
            except Exception as exc:                               # noqa: BLE001
                assert getattr(exc, "field", None) == field, (doc, exc)
    finally:
        r.close()
        shutil.rmtree(src)
    return "the payload above the grant: not registered, refused at call, journaled"


# ---------------------------------------------------------------------------- 5
def test_crash_loop_disables():
    r = Rig("crash")
    src = kitchen()
    try:
        pid = r.add(src, enable=False, settings_set={"crash": True})
        r.store.set_enabled(pid, True, "admin")
        r.host.sync()
        assert wait(lambda: r.host.status(pid)["state"] == "failed", 30), \
            (r.host.status(pid), r.host.logs(pid, 40))
        r.host.sync()
        st = r.host.status(pid)
        assert st["gave_up"]["failures"] == 5, st
        assert "exited (code 3)" in st["gave_up"]["error"], st
        assert r.store.get(pid)["enabled"] is False
        lines = r.host.logs(pid, 200)
        assert sum("starting again in" in ln for ln in lines) == 4, lines
        assert any("gave up" in ln for ln in lines)
        disabled = [a for a in r.audit() if a["action"] == "disable"]
        assert disabled and disabled[-1]["who"] == "plugin-host"
        probs = r.host.problems()
        assert probs == [{"code": "plugin_failed", "plugin": pid,
                          "detail": st["gave_up"]["error"]}], probs
        from proxy import fleethealth
        f = fleethealth.finding("plugin_failed", probs[0]["detail"], plugin=pid)
        assert f["sentence"].startswith("The plugin kitchen-sink does not run"), f
        # fixed and enabled again: it runs, the finding goes
        r.store.write(pid, settings_set={"crash": False}, who="admin")
        r.store.set_enabled(pid, True, "admin")
        r.running(pid)
        assert r.host.problems() == [] and r.host.status(pid)["gave_up"] is None
    finally:
        r.close()
        shutil.rmtree(src)
    return "5 failures with back-off, disabled by plugin-host, a finding; enabled: runs"


# ---------------------------------------------------------------------------- 6
def test_supervision():
    r = Rig("super")
    src = kitchen()
    try:
        pid = r.add(src)
        r.running(pid)
        first = r.host.plugins[pid].pipe.proc
        r.host.stop_plugin(pid)
        assert not first.alive and r.host.status(pid)["state"] == "stopped"
        assert r.host.status(pid)["held"] is True
        r.host.sync()
        r.host.sync()
        assert r.host.status(pid)["state"] == "stopped", "a hand stop holds"
        r.host.start_plugin(pid)
        r.running(pid)
        second = r.host.plugins[pid].pipe.proc
        r.host.restart_plugin(pid)
        r.running(pid)
        assert not second.alive and r.host.plugins[pid].pipe.proc.alive
        # the process dies: started again by itself (one failure counted)
        third = r.host.plugins[pid].pipe.proc
        third.close()
        assert wait(lambda: r.host.running(pid) and r.host.plugins[pid].pipe.proc is not third,
                    20), r.host.status(pid)
        assert r.host.status(pid)["restarts"] == 1
        # a changed setting restarts it with the new value
        r.store.write(pid, settings_set={"hook": "deny"}, who="admin")
        r.host.sync()
        assert wait(lambda: r.host.running(pid) and
                    r.host.plugins[pid].info["settings"]["hook"] == "deny", 20)
        # uninstall while running: the process ends before the folder goes
        proc = r.host.plugins[pid].pipe.proc
        r.store.remove(pid, "admin")
        assert not proc.alive and pid not in r.host.plugins
        assert r.host.status(pid)["state"] == "disabled"
    finally:
        r.close()
        shutil.rmtree(src)
    return "hand stop holds, start, restart, a crash restarts, a setting restarts, uninstall"


# ---------------------------------------------------------------------------- 7
def test_trust_levels():
    r = Rig("trust")
    host_src = kitchen(name="host-sink", trust="host")
    eng = folder(manifest=dict(example("hello-tool"), name="engine-tool", trust="engine"))
    plain = kitchen(name="plain-sink")
    try:
        r.host.set_runtime("host", pluginrt.HostRuntime())
        hid = r.add(host_src)
        r.host.sync()
        assert wait(lambda: r.host.status(hid)["state"] == "refused", 10)
        assert "trust host" in r.host.status(hid)["last_error"]
        assert r.host.problems()[0]["code"] == "plugin_failed"
        try:
            r.store.write(hid, trust="host", who="admin")
            raise AssertionError("trust host without the second confirm")
        except PluginError as exc:
            assert exc.code == "confirm_needed"
        r.store.write(hid, trust="host", confirmed={"trust"}, who="admin")
        st = r.running(hid)
        assert st["trust"] == {"requested": "host", "granted": "host"} and st["runtime"] == "host"
        a = [x for x in r.audit() if x["object_id"] == hid and x["action"] == "settings"][-1]
        assert "trust" in a["fields"] and "trust:host" in a["detail"], a
        try:
            r.store.write(r.add(plain, enable=False), trust="host", confirmed={"trust"},
                          who="admin")
            raise AssertionError("trust host for a sandboxed plugin")
        except PluginError as exc:
            assert exc.code == "not_requested"
        eid = r.add(eng)
        r.host.sync()
        assert wait(lambda: r.host.status(eid)["state"] == "refused", 10)
        assert "not available yet" in r.host.status(eid)["last_error"]
        # no sandbox runtime: unavailable, a finding, no failure counted
        r.host._sandbox, r.host._sandbox_cfg = None, {"backend": "off",
                                                       "why": "no container runtime socket"}
        pid = "plain-sink"
        r.store.set_enabled(pid, True, "admin")
        r.host.sync()
        assert wait(lambda: r.host.status(pid)["state"] == "unavailable", 10)
        import time
        time.sleep(0.6)
        st = r.host.status(pid)
        assert st["state"] == "unavailable" and st["restarts"] == 0, st
        assert {"code": "plugin_no_sandbox", "plugin": pid,
                "detail": "no container runtime socket"} in r.host.problems()
    finally:
        r.close()
        for d in (host_src, eng, plain):
            shutil.rmtree(d, ignore_errors=True)
    return "host only once granted (confirm), engine refused, no sandbox: unavailable"


# ---------------------------------------------------------------------------- 8
def test_hang_opens_breaker():
    r = Rig("hang")
    src = kitchen()
    try:
        pid = r.add(src, grant=["prompts:meta"], settings_set={"hook": "hang"})
        r.running(pid)
        outs = []
        for i in range(6):
            out = r.runner.run("request.received", _ctx(request_id=f"r{i}"), {"model": "m"},
                               lambda v: {"model": "m"})
            outs.append(out)
        calls = r.runner.calls("kitchen-sink/guard", 10)
        kinds = [c["outcome"] for c in reversed(calls)]
        assert kinds[:5] == ["timeout"] * 5 and kinds[5] == "breaker_open", kinds
        assert all(o.deny is not None and o.deny["code"] == "hook_unavailable" for o in outs), \
            "a guard defaults to closed"
        assert r.runner.metrics("kitchen-sink/guard")["breaker"]["state"] == "open"
    finally:
        r.close()
        shutil.rmtree(src)
    return "5 timeouts at 150 ms, then the breaker"


# ---------------------------------------------------------------------------- 9
def test_panels_and_bridge():
    r = Rig("panels")
    src = kitchen()
    try:
        pid = r.add(src, enable=False, grant=["prompts:meta"])
        assert r.host.panel_file(pid, "main", "") is None and r.host.panels(pid) == []
        r.store.set_enabled(pid, True, "admin")
        r.running(pid)
        page = r.host.panel_file(pid, "main", "")
        assert page.endswith(os.path.join("panel", "index.html"))
        assert r.host.panel_file(pid, "main", "app.js").endswith("app.js")
        for bad in ("../plugin.json", "../server.py", "..", ".hidden", "../../../etc/passwd",
                    "nope.js"):
            assert r.host.panel_file(pid, "main", bad) is None, bad
        assert r.host.panel_file(pid, "other", "") is None
        p = r.host.panels(pid)[0]
        assert p["url"] == "/plugin-panels/kitchen-sink/main/" and "sandbox" in p["csp"]
        assert "allow-same-origin" not in p["csp"] + p["sandbox"]
        assert p["bridge"]["methods"] == ["plugin.info", "plugin.status", "tools.call"]
        assert r.host.bridge(pid, "plugin.info", {})["id"] == pid
        assert r.host.bridge(pid, "tools.call", {"tool": "echo", "arguments": {"text": "x"}}
                             )["content"][0]["text"] == "x"
        for method, params, cap in (("settings.read", {}, "settings:read"),
                                    ("engine.control", {}, "engine:control"),
                                    ("tools.call", {"tool": "write", "arguments": {}},
                                     "tools:write")):
            try:
                r.host.bridge(pid, method, params)
                raise AssertionError(method)
            except PluginError as exc:
                assert (exc.status, exc.code) == (403, "refused"), (method, exc.code)
            assert r.host.status(pid)["refusals"][-1]["capability"] == cap
        try:
            r.host.bridge(pid, "fetch", {"url": "http://example.invalid"})
            raise AssertionError("an unknown bridge method")
        except PluginError as exc:
            assert exc.status == 403
        r.store.write(pid, grant=["settings:read"], who="admin")
        r.running(pid)
        wait(lambda: "settings:read" in r.host.plugins[pid].granted, 10)
        assert "settings.read" in r.host.bridge_methods(pid)
        assert r.host.bridge(pid, "settings.read", {})["settings"]["hook"] == "continue"
    finally:
        r.close()
        shutil.rmtree(src)
    return "panel files in its folder only; the bridge per capability, refusals journaled"


# ---------------------------------------------------------------------------- 10
def test_logs_redacted_and_capped():
    r = Rig("logs")
    src = kitchen()
    try:
        pid = r.add(src, grant=["secrets:TOKEN"], secrets_set={"TOKEN": "tok-very-secret-42"})
        r.running(pid)
        lines = wait(lambda: [ln for ln in r.host.logs(pid) if "started with token" in ln], 10)
        assert lines and "[secret TOKEN]" in lines[0] and "tok-very" not in lines[0], lines
        path = os.path.join(r.host.state_dir, "logs", "plugins", f"{pid}.log")
        body = open(path).read()
        assert "tok-very-secret-42" not in body and "[secret TOKEN]" in body
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        log = r.host.plugins[pid].log
        old = pluginhost.LOG_MAX
        pluginhost.LOG_MAX = 2000
        try:
            for i in range(200):
                log.line(f"line {i} " + "x" * 50)
        finally:
            pluginhost.LOG_MAX = old
        assert os.path.getsize(path) < 4000 and os.path.isfile(path + ".1")
        assert len(log.last(5000)) <= pluginhost.TAIL_LINES
    finally:
        r.close()
        shutil.rmtree(src)
    return "stderr in the log, the secret replaced, the file capped with one older file"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
