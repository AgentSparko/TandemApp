"""SRV-861 (QA SRV-862): the plugin SDK (sdk/python/tandemapp_plugin), its two example plugins and
the engine-stats manifests, through the real plugin registry and host. The webhook the examples
post to is a server on this machine: no example needs the network.

  Gherkin and the acceptance criteria:
  1  SDK refuses a bad tool: a tool defined without an annotation (or with one not true or false,
     or a schema not of type object, or one that allows extra properties at the top, nested, in
     array items or in a $def) is refused by the SDK, naming the annotation or the place; the
     SDK's validator agrees with the install's checks on every tool refusal fixture; the code and
     the manifest compared, a drift refuses the start                test_sdk_refuses_bad_tools
  2  Examples run (sandboxed, the tests' stand-in runtime): both install, run and answer:
     note-webhook's tool posts to the webhook with the granted secret as the bearer and its
     observer hook posts at request.finished through the runner; keyword-router answers classify
     and judge through the providers, its rules read over tandem/settings
                                                                    test_examples_run_sandboxed
  3  The same examples at trust host, granted with the typed id, run as processes of the app's
     user and answer the same                                       test_examples_run_at_host
  4  The SDK's requests through the host: tandem/settings with and without settings:read,
     tandem/secret granted, not granted and another plugin's, tandem/engine refused, each refusal
     a Refused naming the capability; a handler waiting on the host holds no other call
                                                                    test_sdk_requests
  5  `tandemapp-plugin new` for each kind (tool, hook, provider) writes a plugin that the install
     takes and the host runs and calls; refused: a folder that exists, a bad name; `check` finds
     a stale copy of the SDK                                         test_scaffold
  6  Stats plugin manifests: the vLLM and SGLang engine-stats plugins install through the
     installer as kind engine-stats, and the host never starts them  test_engine_stats_install
  7  The docs and the copies: docs/plugins-sdk.md names the SDK's API and both examples, is linked
     from docs/README.md and docs/plugins.md, and every link of the plugin pages resolves (file and
     anchor); each example's copy of the SDK is the SDK; the examples' manifests name no real
     host                                                           test_docs_and_copies

Run: python tests/test_plugin_sdk.py
"""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plugin_host_util import Rig, wait, write_tree  # noqa: E402
from plugin_util import EX  # noqa: E402

from proxy import hooks as hooks_mod  # noqa: E402
from proxy import pluginmanifest as PM  # noqa: E402
from proxy.pluginstore import PluginError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.path.join(ROOT, "sdk", "python")
EXAMPLES = os.path.join(SDK, "examples")
if SDK not in sys.path:
    sys.path.insert(0, SDK)
import tandemapp_plugin as T  # noqa: E402
from tandemapp_plugin import cli  # noqa: E402

ANN = {"read_only": True, "destructive": False, "idempotent": True, "open_world": False}
STRICT = {"type": "object", "properties": {"x": {"type": "string"}},
          "additionalProperties": False}


def _ctx(**kw):
    c = {k: None for k in hooks_mod.CTX_KEYS}
    c.update(request_id="r-1", model="m", endpoint="/v1/chat/completions")
    c.update(kw)
    return c


def copy_example(name: str, base: str | None = None, **change) -> str:
    """A copy of an SDK example (its manifest changed by `change`)."""
    d = tempfile.mkdtemp(prefix=f"sdk-{name}-", dir=base)
    dst = os.path.join(d, name)
    shutil.copytree(os.path.join(EXAMPLES, name), dst,
                    ignore=shutil.ignore_patterns("__pycache__"))
    if change:
        p = os.path.join(dst, "plugin.json")
        m = json.load(open(p))
        m.update(change)
        with open(p, "w") as f:
            json.dump(m, f, indent=1)
    return dst


class Webhook:
    """A webhook on this machine that keeps what it was sent."""

    def __init__(self):
        got = self.got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                                      # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                got.append({"path": self.path, "auth": self.headers.get("Authorization"),
                            "body": json.loads(self.rfile.read(n) or b"{}")})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/notes"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _refused(fn, contains: str):
    try:
        fn()
    except T.ToolDefinitionError as exc:
        assert contains in str(exc), (contains, str(exc))
        return str(exc)
    raise AssertionError(f"not refused: wanted {contains!r}")


# ---------------------------------------------------------------------------- 1
def test_sdk_refuses_bad_tools():
    p = T.Plugin(here=tempfile.gettempdir(), manifest={"name": "t", "version": "0.1.0"})
    for kw, ann in (("read_only", "readOnlyHint"), ("destructive", "destructiveHint"),
                    ("idempotent", "idempotentHint"), ("open_world", "openWorldHint")):
        given = {k: v for k, v in ANN.items() if k != kw}
        msg = _refused(lambda: p.tool("greet", description="d", input_schema=STRICT, **given),
                       f"the annotation {ann} is missing")
        assert "greet" in msg
    _refused(lambda: p.tool("greet", description="d", input_schema=STRICT, **dict(ANN,
                                                                                  read_only=1)),
             "readOnlyHint is true or false")
    _refused(lambda: p.tool("greet", description="d", input_schema={"type": "string"}, **ANN),
             "of type object")
    for schema, where in (
            ({"type": "object", "properties": {}}, "the schema"),
            ({"type": "object", "additionalProperties": True}, "the schema"),
            ({"type": "object", "additionalProperties": False,
              "properties": {"o": {"type": "object", "properties": {}}}}, "properties.o"),
            ({"type": "object", "additionalProperties": False,
              "properties": {"l": {"type": "array", "items": {"type": "object"}}}},
             "properties.l.items"),
            ({"type": "object", "additionalProperties": False,
              "properties": {"r": {"$ref": "#/$defs/R"}},
              "$defs": {"R": {"type": "object", "properties": {}}}}, "#/$defs/R"),
            ({"type": "object", "additionalProperties": False, "patternProperties": {"x": {}}},
             "patternProperties")):
        _refused(lambda: p.tool("t1", description="d", input_schema=schema, **ANN), where)
    _refused(lambda: p.tool("t2", input_schema=STRICT, **ANN), "description")
    assert p.tools == {}, "nothing refused was registered"

    @p.tool("ok", description="d", input_schema=STRICT, **ANN)
    def ok(args, ctx):
        return "fine"
    _refused(lambda: p.tool("ok", description="d", input_schema=STRICT, **ANN), "twice")
    # the SDK's validator and the install agree on every tool refusal fixture
    fixtures = os.path.join(ROOT, "contracts", "plugin-v1", "fixtures", "refusals")
    seen = 0
    for n in sorted(os.listdir(fixtures)):
        fx = json.load(open(os.path.join(fixtures, n)))
        if not str(fx.get("field") or "").startswith("contributes.tools") or \
                fx.get("code") in ("unknown_field", "duplicate_name"):
            continue                    # the SDK checks annotations and schemas, not the rest
        tools = ((fx.get("manifest") or {}).get("contributes") or {}).get("tools") or []
        sdk = [w for t in tools for w in T.validate_tool(t)]
        assert sdk, (n, fx["code"], "the SDK passed a tool the install refuses")
        seen += 1
    assert seen >= 7, seen
    for ex in ("hello-tool",):
        m = json.load(open(os.path.join(EX, ex, "plugin.json")))
        assert all(not T.validate_tool(t) for t in m["contributes"]["tools"])
    # the code against the manifest: a drift refuses the start, each difference named
    m = {"name": "drift", "version": "0.1.0", "contributes": {
        "tools": [{"name": "ok", "description": "d", "inputSchema": STRICT,
                   "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                   "idempotentHint": True, "openWorldHint": True}}],
        "hooks": [{"name": "h", "stages": ["request.finished"], "role": "observer",
                   "payload": "meta"}],
        "decision_providers": [{"name": "p", "questions": ["classify", "judge"]}]}}
    err = io.StringIO()
    q = T.Plugin(here=tempfile.gettempdir(), manifest=m, stdin=io.StringIO(""),
                 stdout=io.StringIO(), stderr=err)
    q.tool("ok", description="d", input_schema=STRICT, **ANN)(lambda a, c: "x")
    q.hook("other")(lambda c, x: None)
    q.decision("p", question="classify")(lambda a, c: "x")
    diff = q.check()
    assert "tool ok: the code and the manifest disagree on annotations" in diff, diff
    assert "hook other: the manifest does not declare it" in diff, diff
    assert "hook h: declared, no handler" in diff and \
        "provider p: declared for judge, no handler" in diff, diff
    assert q.run() == 2 and "not started" in err.getvalue(), err.getvalue()
    return "a missing annotation or a loose schema refused by name; drift refuses the start"


# ---------------------------------------------------------------------------- 2, 3
def _run_examples(r: Rig, base: str, trust: str | None) -> str:
    hook = Webhook()
    change = {"trust": trust} if trust else {}
    nw = copy_example("note-webhook", base, **change)
    kr = copy_example("keyword-router", base, **change)
    try:
        nid = r.add(nw, enable=False, grant=["prompts:meta", "secrets:WEBHOOK_TOKEN"],
                    settings_set={"url": hook.url, "timeout_s": 5},
                    secrets_set={"WEBHOOK_TOKEN": "tok-test-4711"})
        kid = r.add(kr, enable=False, grant=["prompts:redacted", "settings:read"])
        if trust == "host":
            for pid in (nid, kid):
                r.store.grant_trust(pid, "host", confirm=pid, who="admin")
        for pid in (nid, kid):
            r.store.set_enabled(pid, True, "admin")
        st = [r.running(pid) for pid in (nid, kid)]
        want = "host" if trust == "host" else r.host.status(nid)["runtime"]
        assert all(s["runtime"] == want for s in st), st
        assert st[0]["tools"] == ["send_note"] and st[0]["hooks"] == ["note-webhook/finished"]
        assert st[1]["providers"] == ["keyword-router/rules"], st[1]
        # the tool: a note posted with the bearer
        got = r.host.tool_call(nid, "send_note", {"text": "the build is green"})
        assert got["content"][0]["text"] == "sent (200)", got
        assert hook.got[-1]["body"]["note"] == {"text": "the build is green"}, hook.got
        assert hook.got[-1]["auth"] == "Bearer tok-test-4711"
        assert hook.got[-1]["body"]["plugin"] == "note-webhook"
        try:
            r.host.tool_call(nid, "send_note", {"text": ""})
            raise AssertionError("an argument outside the schema went through")
        except PluginError as exc:
            assert exc.code == "bad_arguments"
        # the observer hook, through the runner
        n0 = len(hook.got)
        r.runner.notify("request.finished", _ctx(), {"id": "r-77", "model": "m", "tokens": 9,
                                                     "ms": 21})
        r.runner.bg.drain(10)
        assert wait(lambda: len(hook.got) > n0, 10), "no note for the finished request"
        assert hook.got[-1]["body"]["note"] == {"id": "r-77", "model": "m", "tokens": 9,
                                                "ms": 21}, hook.got[-1]
        calls = r.runner.calls("note-webhook/finished")
        assert calls and calls[0]["outcome"] == "ok", calls
        # the provider, through the providers, both questions
        p = r.providers.store.create({"name": "kw", "kind": "plugin",
                                      "target": "keyword-router/rules", "mode": "on",
                                      "questions": {"classify": {}, "judge": {}}}, "admin")
        row = r.providers.row(p["id"])
        rep = r.providers.ask(row, "classify", "there is a traceback in it", "which class?",
                              {"chat": "talk", "code": "write code", "research": "look up"})
        assert (rep.error, rep.label) == (None, "code"), rep.view()
        rep = r.providers.ask(row, "judge", "the build failed twice", "how did it go?",
                              {"clean": "ok", "failure": "broken", "unclear": "?"})
        assert (rep.error, rep.label) == (None, "failure"), rep.view()
        rep = r.providers.ask(row, "judge", "nothing to say", "how did it go?",
                              {"unclear": "?", "clean": "ok"})
        assert rep.label == "unclear", rep.view()
        assert not r.host.status(kid)["refusals"], "settings:read was granted: no refusal"
        return want
    finally:
        hook.close()


def test_examples_run_sandboxed():
    r = Rig("sdk-sandboxed")
    base = tempfile.mkdtemp(prefix="sdk-ex-")
    try:
        runtime = _run_examples(r, base, None)
    finally:
        r.close()
        shutil.rmtree(base)
    return f"both examples install, run ({runtime} stand-in) and answer"


def test_examples_run_at_host():
    from proxy import pluginrt
    r = Rig("sdk-host")
    r.host.set_runtime("host", pluginrt.HostRuntime())
    base = tempfile.mkdtemp(prefix="sdk-ex-")
    try:
        assert _run_examples(r, base, "host") == "host"
    finally:
        r.close()
        shutil.rmtree(base)
    return "both examples at trust host, granted by typed id"


# ---------------------------------------------------------------------------- 4
PROBE = r'''
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin, Refused

plugin = Plugin()
gate = threading.Event()


@plugin.tool("probe", description="Ask the host for one thing.",
             input_schema={"type": "object", "properties": {"what": {"type": "string"},
                                                            "name": {"type": "string"}},
                           "required": ["what"], "additionalProperties": False},
             read_only=True, destructive=False, idempotent=True, open_world=False)
def probe(args, ctx):
    what = args["what"]
    try:
        if what == "settings":
            return plugin.read_settings()
        if what == "secret":
            return {"value": plugin.request("tandem/secret", {"name": args.get("name")})["value"]}
        if what == "other":
            return plugin.request("tandem/secret", {"name": "TOKEN", "plugin": "someone-else"})
        if what == "engine":
            return plugin.engine({"restart": True})
        if what == "slow":
            gate.wait(10)
            return "slow done"
        if what == "release":
            gate.set()
            return "released"
    except Refused as exc:
        return {"refused": exc.capability, "message": exc.message}
    return "?"


if __name__ == "__main__":
    plugin.main()
'''


def _probe_manifest(name: str) -> dict:
    return {"plugin_api": "1.0", "name": name, "version": "0.1.0", "tandemapp": ">=0.15",
            "hooks_api": "1.0", "entry": {"command": ["python3", "main.py"]},
            "contributes": {"tools": [{
                "name": "probe", "description": "Ask the host for one thing.",
                "inputSchema": {"type": "object", "properties": {"what": {"type": "string"},
                                                                 "name": {"type": "string"}},
                                "required": ["what"], "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                "idempotentHint": True, "openWorldHint": False}}]},
            "settings": {"type": "object", "additionalProperties": False,
                         "properties": {"mood": {"type": "string", "default": "calm"}}},
            "secrets": [{"name": "TOKEN"}, {"name": "OTHER"}],
            "capabilities": ["settings:read", "secrets:TOKEN", "secrets:OTHER",
                             "engine:control"]}


def test_sdk_requests():
    r = Rig("sdk-requests")
    base = tempfile.mkdtemp(prefix="sdk-probe-")
    try:
        d = write_tree(os.path.join(base, "probe"), {
            "plugin.json": json.dumps(_probe_manifest("sdk-probe"), indent=1),
            "main.py": PROBE})
        cli.vendor(d)
        pid = r.add(d, enable=False, settings_set={"mood": "bright"},
                    secrets_set={"TOKEN": "tok-1", "OTHER": "tok-2"})
        r.store.set_enabled(pid, True, "admin")
        r.running(pid)

        def call(**a):
            res = r.host.tool_call(pid, "probe", a, timeout_s=30)
            return json.loads(res["content"][0]["text"]) if res["content"][0]["text"][:1] in \
                "{[" else res["content"][0]["text"]
        got = call(what="settings")
        assert got["refused"] == "settings:read", got
        got = call(what="secret", name="TOKEN")
        assert got["refused"] == "secrets:TOKEN", got
        r.store.write(pid, grant=["settings:read", "secrets:TOKEN", "engine:control"],
                      confirmed={"engine:control"}, who="admin")
        r.running(pid)                              # restarted with the grants
        assert call(what="settings") == {"mood": "bright"}
        assert call(what="secret", name="TOKEN") == {"value": "tok-1"}
        got = call(what="secret", name="OTHER")
        assert got["refused"] == "secrets:OTHER", "a declared secret not granted"
        got = call(what="other")
        assert got["refused"].startswith("secrets:someone-else/"), got
        got = call(what="engine")
        assert got["refused"] == "engine:control" and "not available yet" in got["message"]
        # a handler waiting holds no other call
        slow = {}
        t = threading.Thread(target=lambda: slow.update(r=call(what="slow")))
        t.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        assert call(what="settings") == {"mood": "bright"}
        assert time.monotonic() - t0 < 5 and t.is_alive(), "a call waited behind the slow one"
        assert call(what="release") == "released"
        t.join(10)
        assert slow.get("r") == "slow done", slow
        caps = [x["capability"] for x in r.host.status(pid)["refusals"]]
        assert {"settings:read", "secrets:TOKEN", "secrets:OTHER", "engine:control"} <= set(caps)
    finally:
        r.close()
        shutil.rmtree(base)
    return "settings, secrets and engine through the SDK; refusals named; no call held"


# ---------------------------------------------------------------------------- 5
def test_scaffold():
    base = tempfile.mkdtemp(prefix="sdk-new-")
    r = Rig("sdk-new")
    env = dict(os.environ, PYTHONPATH=SDK)

    def tool(*args, want=0):
        p = subprocess.run([sys.executable, "-m", "tandemapp_plugin", *args],
                           capture_output=True, text=True, timeout=60, env=env, cwd=base)
        assert p.returncode == want, (args, p.returncode, p.stdout, p.stderr)
        return p.stdout + p.stderr

    try:
        for kind in ("tool", "hook", "provider"):
            out = tool("new", f"demo-{kind}", "--kind", kind)
            d = os.path.join(base, f"demo-{kind}")
            assert sorted(os.listdir(d)) == ["main.py", "plugin.json", "tandemapp_plugin"], out
            chk = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "plugin_check.py"),
                                  d], capture_output=True, text=True, timeout=60)
            assert chk.returncode == 0, (kind, chk.stdout, chk.stderr)
            assert "ok demo-" in tool("check", d)
        tool("new", "demo-tool", want=2)
        tool("new", "Bad_Name", want=2)
        tid = r.add(os.path.join(base, "demo-tool"), settings_set={"prefix": "> "})
        hid = r.add(os.path.join(base, "demo-hook"), grant=["prompts:meta"])
        pid = r.add(os.path.join(base, "demo-provider"), grant=["prompts:redacted"])
        for x in (tid, hid, pid):
            r.running(x)
        got = r.host.tool_call(tid, "echo", {"text": "hi"})
        assert got["content"][0]["text"] == "> hi", got
        r.runner.notify("request.finished", _ctx(), {"id": "r-5", "model": "m", "tokens": 1,
                                                     "ms": 2})
        r.runner.bg.drain(10)
        log = os.path.join(r.host.state_dir, "plugin-data", hid, "finished.jsonl")
        line = wait(lambda: os.path.isfile(log) and open(log).read().strip(), 10)
        assert json.loads(line.splitlines()[-1])["id"] == "r-5", line
        p = r.providers.store.create({"name": "d", "kind": "plugin", "target":
                                      "demo-provider/answer", "mode": "on",
                                      "questions": {"classify": {}}}, "admin")
        rep = r.providers.ask(r.providers.row(p["id"]), "classify", "a traceback here", "?",
                              {"chat": "talk", "code": "code"})
        assert (rep.error, rep.label) == (None, "code"), rep.view()
        # a stale copy of the SDK
        with open(os.path.join(base, "demo-hook", "tandemapp_plugin", "__init__.py"), "a") as f:
            f.write("# changed\n")
        assert "differs from SDK" in tool("check", os.path.join(base, "demo-hook"), want=1)
    finally:
        r.close()
        shutil.rmtree(base)
    return "new: tool, hook and provider plugins that install, run and answer"


# ---------------------------------------------------------------------------- 6
def test_engine_stats_install():
    r = Rig("sdk-stats")
    try:
        for eng in ("vllm", "sglang"):
            d = tempfile.mkdtemp(prefix=f"stats-{eng}-")
            src = os.path.join(d, eng)
            shutil.copytree(os.path.join(ROOT, "plugins", eng), src,
                            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"))
            try:
                p, note = r.reg.install(src)
                assert note["action"] == "installed" and p["kind"] == "engine-stats", p
                assert p["manifest"]["engine_stats"]["engine"] == eng
                r.store.set_enabled(p["id"], True, "admin")
            finally:
                shutil.rmtree(d)
        r.host.sync()
        time.sleep(0.3)
        assert r.host.plugins == {} and r.host.statuses() == [], "an engine-stats plugin runs"
        assert sorted(x["kind"] for x in r.store.list()) == ["engine-stats", "engine-stats"]
    finally:
        r.close()
    return "vLLM and SGLang install as kind engine-stats; never started"


# ---------------------------------------------------------------------------- 7
def _slug(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def _links_ok(md: str) -> list[str]:
    bad = []
    text = open(md, encoding="utf-8").read()
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    for target in re.findall(r"\]\(([^)\s]+)\)", body):
        if re.match(r"^[a-z]+://", target):
            continue
        path, _, anchor = target.partition("#")
        f = os.path.normpath(os.path.join(os.path.dirname(md), path)) if path else md
        if not os.path.exists(f):
            bad.append(f"{os.path.relpath(md, ROOT)}: {target}: no such file")
            continue
        if anchor and f.endswith(".md"):
            heads = {_slug(h) for h in re.findall(r"^#+\s+(.*)$", open(f).read(), re.M)}
            if anchor not in heads:
                bad.append(f"{os.path.relpath(md, ROOT)}: {target}: no such heading")
    return bad


def test_docs_and_copies():
    page = os.path.join(ROOT, "docs", "plugins-sdk.md")
    text = open(page, encoding="utf-8").read()
    for name in ("@plugin.tool(", "@plugin.hook(", "@plugin.decision(", "@plugin.backend(",
                 "plugin.settings", "plugin.read_settings()", "plugin.secret(",
                 "plugin.engine(", "plugin.request(", "plugin.data_dir", "plugin.log(",
                 "plugin.check()", "validate_tool", "ToolError", "Refused", "HostError",
                 "ToolDefinitionError", "tandemapp-plugin new", "tandemapp-plugin vendor",
                 "tandemapp-plugin check", "note-webhook", "keyword-router"):
        assert name in text, f"docs/plugins-sdk.md does not name {name}"
    for public in T.__all__:
        if public[0].isupper() and public not in ("ANNOTATIONS", "SDK_VERSION", "PROTOCOL",
                                                  "Call", "Ask", "BackendRequest"):
            assert public in text, f"docs/plugins-sdk.md does not name {public}"
    assert "(plugins-sdk.md)" in open(os.path.join(ROOT, "docs", "README.md")).read()
    assert "(plugins-sdk.md)" in open(os.path.join(ROOT, "docs", "plugins.md")).read()
    bad = []
    for md in ("docs/plugins-sdk.md", "docs/plugins.md", "docs/README.md",
               "contracts/plugin-v1/README.md"):
        bad += _links_ok(os.path.join(ROOT, md))
    assert not bad, bad
    sdk = open(os.path.join(SDK, "tandemapp_plugin", "__init__.py"), "rb").read()
    for ex in sorted(os.listdir(EXAMPLES)):
        d = os.path.join(EXAMPLES, ex)
        assert cli.stale(d) == [], (ex, cli.stale(d))
        assert open(os.path.join(d, "tandemapp_plugin", "__init__.py"), "rb").read() == sdk
        rep = PM.inspect(d, tandemapp="0.15.0", hooks_api="1.0")
        assert rep["ok"], (ex, rep["errors"])
        hosts = [c.split(":", 1)[1] for c in rep["capabilities"] if c.startswith("network:")]
        assert all(h.endswith("example.com") for h in hosts), (ex, hosts)
        m = rep["manifest"]
        for t in (m.get("contributes") or {}).get("tools") or []:
            assert not T.validate_tool(t), (ex, t["name"])
    assert sorted(os.listdir(EXAMPLES)) == ["keyword-router", "note-webhook"]
    return "the page names the API and links resolve; the copies are the SDK"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
