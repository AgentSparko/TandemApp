"""tandemapp_plugin: write a TandemApp plugin in Python with the standard library only (SRV-861).

    from tandemapp_plugin import Plugin, ToolError

    plugin = Plugin()                                   # reads plugin.json beside the script

    @plugin.tool("greet", description="Say hello.", read_only=True, destructive=False,
                 idempotent=True, open_world=False,
                 input_schema={"type": "object", "properties": {"name": {"type": "string"}},
                               "required": ["name"], "additionalProperties": False})
    def greet(args, ctx):
        return f"{plugin.settings.get('greeting', 'Hello')}, {args['name']}."

    @plugin.hook("finished")                            # a hook the manifest declares
    def finished(call, ctx):
        plugin.log(f"request {call.event.get('id')} finished")
        return {"action": "continue"}

    @plugin.decision("keywords", question="classify")   # a provider and one of its questions
    def classify(ask, ctx):
        return {"label": "chat", "confidence": 0.5}

    if __name__ == "__main__":
        plugin.run()

THE PROTOCOL (docs/plugins.md): MCP over stdio, one JSON-RPC 2.0 message a line, revision
2025-06-18, plus `hooks/call`, `decision/ask` and `backend/request` from the plugin host, and the
plugin's own requests `tandem/settings`, `tandem/secret` and `tandem/engine`. `run()` is the whole
loop: it reads stdin on one thread, answers each host request on a worker thread (so a handler
may itself ask the host, e.g. for a secret, without blocking the reader), writes stdout under a
lock, and keeps stdout for the protocol: `log()` writes to stderr.

TOOLS state all four MCP annotations (readOnlyHint, destructiveHint, idempotentHint,
openWorldHint) and an input schema that says additionalProperties false on every object: the
decorator takes them as required keyword arguments and refuses a tool that breaks the rule,
naming what is wrong (`validate_tool`). `check()` compares what the code registers with the
manifest's contributions; `run()` refuses to start when they differ, with each difference on
stderr, since the host offers only what the manifest declares, with the manifest's schema.

Nothing here reaches the network or the controller's files: a plugin gets its settings
(TANDEM_PLUGIN_SETTINGS), its data folder (TANDEM_PLUGIN_DATA), its granted capabilities and the
secrets granted to it, and asks the host for the rest. Copy this package into the plugin's folder
(`python -m tandemapp_plugin vendor <folder>`): a sandboxed plugin sees only its own tree.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback

__all__ = ["Plugin", "ToolError", "Refused", "HostError", "ToolDefinitionError", "Call", "Ask",
           "BackendRequest", "validate_tool", "validate_schema", "ANNOTATIONS", "SDK_VERSION",
           "PROTOCOL"]

SDK_VERSION = "0.1.0"
PROTOCOL = "2025-06-18"
HOOKS_API = "1.0"
PLUGIN_API = "1.0"
ANNOTATIONS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
#: the hooks-v1 actions a hook may answer
ACTIONS = ("continue", "modify", "deny", "decide")
REFUSED = -32001                     # the host's refusal: data.capability names what was missing
NO_METHOD = -32601
BAD_PARAMS = -32602
INTERNAL = -32603
REQUEST_S = 30.0
_UNSET = object()                    # an annotation not given


class ToolDefinitionError(ValueError):
    """A tool (or hook, provider, backend) the SDK refuses to register, with why."""


class ToolError(Exception):
    """Raise in a tool to answer with isError: the message is the tool's text."""


class HostError(Exception):
    """The host answered a request of the plugin's with an error."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Refused(HostError):
    """The host refused a request: the plugin lacks `capability` (granted on its page)."""

    @property
    def capability(self) -> str | None:
        return (self.data or {}).get("capability") if isinstance(self.data, dict) else None


# ================================================================================ validation
def _extra_props(node, path: str, out: list, root=None, depth: int = 0) -> None:
    """Every object level of a schema says additionalProperties false (refs to $defs too)."""
    if depth > 32 or not isinstance(node, dict):
        return
    root = node if root is None else root
    if "$ref" in node:
        ref = node["$ref"]
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            sub = (root.get("$defs") or {}).get(ref.split("/")[-1])
            _extra_props(sub, f"{path}->{ref}", out, root, depth + 1)
        return
    is_object = node.get("type") == "object" or (isinstance(node.get("type"), list) and
                                                  "object" in node["type"]) or \
        "properties" in node
    if is_object:
        if node.get("additionalProperties") is not False:
            out.append(f"{path or 'the schema'}: an object says \"additionalProperties\": false")
        if "patternProperties" in node:
            out.append(f"{path}: patternProperties allows extra properties")
    for k, sub in (node.get("properties") or {}).items():
        _extra_props(sub, f"{path}.properties.{k}" if path else f"properties.{k}", out, root,
                     depth + 1)
    items = node.get("items")
    if isinstance(items, dict):
        _extra_props(items, f"{path}.items", out, root, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(node.get(key) or []):
            _extra_props(sub, f"{path}.{key}[{i}]", out, root, depth + 1)


def validate_schema(schema) -> list[str]:
    """Why an input schema is not strict, one line each; [] when it is."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["the input schema is a JSON schema of type object"]
    out: list[str] = []
    _extra_props(schema, "", out)
    return out


def validate_tool(tool) -> list[str]:
    """Why an MCP tool would be refused at install, one line each naming the tool and the
    annotation or the place in its schema; [] when it would pass."""
    if not isinstance(tool, dict):
        return ["a tool is an object {name, description, inputSchema, annotations}"]
    name = tool.get("name") if isinstance(tool.get("name"), str) else "?"
    out = []
    if not isinstance(tool.get("name"), str) or not tool["name"]:
        out.append("a tool needs a name")
    if not isinstance(tool.get("description"), str) or not tool["description"].strip():
        out.append(f"tool {name}: a description says what it does")
    ann = tool.get("annotations")
    if not isinstance(ann, dict):
        out.append(f"tool {name}: annotations are missing: {', '.join(ANNOTATIONS)}")
    else:
        for a in ANNOTATIONS:
            if a not in ann:
                out.append(f"tool {name}: the annotation {a} is missing")
            elif not isinstance(ann[a], bool):
                out.append(f"tool {name}: the annotation {a} is true or false")
    for why in validate_schema(tool.get("inputSchema")):
        out.append(f"tool {name}: inputSchema {why}")
    return out


# ================================================================================ what comes in
class Call:
    """A `hooks/call`: the hooks-v1 envelope; `event` at the hook's payload level."""

    def __init__(self, params: dict):
        self.params = params
        self.hook = params.get("hook")
        self.stage = params.get("stage")
        self.mode = params.get("mode")
        self.request = params.get("request") or {}
        self.event = params.get("event") or {}
        self.payload = params.get("payload")
        self.id = params.get("id")


class Ask:
    """A `decision/ask`: `text` is the input at redacted or full, `chars` its size at meta."""

    def __init__(self, params: dict):
        self.params = params
        self.provider = params.get("provider")
        self.question = params.get("question")
        self.instructions = params.get("instructions")
        self.options = params.get("options") or {}
        self.model = params.get("model")
        self.deadline_ms = params.get("deadline_ms")
        inp = params.get("input") or {}
        self.text = inp.get("text")
        self.chars = inp.get("chars", len(self.text) if isinstance(self.text, str) else None)


class BackendRequest:
    """A `backend/request`: an OpenAI-style request routed to a contributed backend."""

    def __init__(self, params: dict):
        self.params = params
        self.backend = params.get("backend")
        self.method = params.get("method")
        self.path = params.get("path")
        self.body = params.get("body")


class Context:
    """What a handler gets besides its input: the plugin, and `id` of the host's request."""

    def __init__(self, plugin: "Plugin", rid):
        self.plugin = plugin
        self.id = rid


# ================================================================================ the plugin
class Plugin:
    """One plugin: its manifest, its handlers and the loop. `here`: the plugin's folder (default:
    the folder of the script that made it); `manifest`: a manifest dict instead of plugin.json."""

    def __init__(self, here: str | None = None, manifest: dict | None = None,
                 stdin=None, stdout=None, stderr=None, env=None):
        if here is None:
            main = sys.modules.get("__main__")
            f = getattr(main, "__file__", None)
            here = os.path.dirname(os.path.abspath(f)) if f else os.getcwd()
        self.here = here
        if manifest is None:
            with open(os.path.join(here, "plugin.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        self.manifest = manifest
        self.name = manifest.get("name") or "plugin"
        self.version = manifest.get("version") or "0.0.0"
        self.env = os.environ if env is None else env
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._err = stderr or sys.stderr
        self.tools: dict[str, dict] = {}
        self._tool_fns: dict = {}
        self.hooks: dict = {}
        self.decisions: dict = {}           # (provider, question) -> fn
        self.backends: dict = {}
        self._wlock = threading.Lock()
        self._pending: dict = {}
        self._plock = threading.Lock()
        self._nid = 0
        self._closed = threading.Event()

    # ------------------------------------------------------------------ what the host gives
    @property
    def settings(self) -> dict:
        """Its settings as the host passed them at start, each schema default filled in."""
        try:
            s = json.loads(self.env.get("TANDEM_PLUGIN_SETTINGS") or "{}")
        except ValueError:
            s = {}
        return s if isinstance(s, dict) else {}

    @property
    def capabilities(self) -> list[str]:
        try:
            c = json.loads(self.env.get("TANDEM_PLUGIN_CAPABILITIES") or "[]")
        except ValueError:
            c = []
        return c if isinstance(c, list) else []

    def granted(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def data_dir(self) -> str:
        """Its own folder for files it keeps (made when missing)."""
        d = self.env.get("TANDEM_PLUGIN_DATA") or os.path.join(self.here, ".data")
        os.makedirs(d, exist_ok=True)
        return d

    def log(self, *parts) -> None:
        """A line to the plugin's log (stderr; the host replaces every secret it gave)."""
        try:
            self._err.write(" ".join(str(p) for p in parts) + "\n")
            self._err.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ the plugin's requests
    def request(self, method: str, params: dict | None = None, timeout: float = REQUEST_S):
        """A request of the plugin's to the host; raises Refused or HostError."""
        with self._plock:
            self._nid += 1
            rid = f"{self.name}-{self._nid}"
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[rid] = box
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise HostError(INTERNAL, f"the host did not answer {method} within {timeout} s")
        finally:
            with self._plock:
                self._pending.pop(rid, None)
        if msg is None:
            raise HostError(INTERNAL, "the host closed the stream")
        if "error" in msg:
            e = msg["error"] if isinstance(msg["error"], dict) else {}
            cls = Refused if e.get("code") == REFUSED else HostError
            raise cls(e.get("code", INTERNAL), e.get("message") or "error", e.get("data"))
        return msg.get("result")

    def read_settings(self) -> dict:
        """Its settings as they are now (tandem/settings; needs settings:read)."""
        return (self.request("tandem/settings") or {}).get("settings") or {}

    def secret(self, name: str, ask: bool = True) -> str | None:
        """A secret granted to it (secrets:<name>): from its environment, else asked of the host
        (tandem/secret). None when it is granted but not set; Refused when not granted."""
        if name in self.env and self.granted(f"secrets:{name}"):
            return self.env[name]
        if not ask:
            return None
        return (self.request("tandem/secret", {"name": name}) or {}).get("value")

    def engine(self, params: dict | None = None):
        """Engine control (tandem/engine, engine:control): refused until it exists."""
        return self.request("tandem/engine", params or {})

    # ------------------------------------------------------------------ registration
    def tool(self, name: str, *, description: str = "", input_schema=None, read_only=_UNSET,
             destructive=_UNSET, idempotent=_UNSET, open_world=_UNSET, title: str | None = None):
        """Register a tool. Every annotation must be given, true or false: read_only
        (readOnlyHint), destructive (destructiveHint), idempotent (idempotentHint), open_world
        (openWorldHint); and the input schema must be strict. A tool that misses one is refused
        here (ToolDefinitionError naming the annotation), as the install would refuse it. The
        handler gets (arguments, ctx) and returns text, a list of MCP content items, or a full
        result; raising ToolError answers with isError."""
        given = {"readOnlyHint": read_only, "destructiveHint": destructive,
                 "idempotentHint": idempotent, "openWorldHint": open_world}
        ann = {k: v for k, v in given.items() if v is not _UNSET}
        t = {"name": name, "description": description, "inputSchema": input_schema,
             "annotations": ann}
        if title:
            t["title"] = title
        why = validate_tool(t)
        if why:
            raise ToolDefinitionError("; ".join(why))
        if name in self.tools:
            raise ToolDefinitionError(f"tool {name}: registered twice")

        def put(fn):
            self.tools[name] = t
            self._tool_fns[name] = fn
            return fn
        return put

    def hook(self, name: str):
        """Register the handler of a hook the manifest declares (contributes.hooks[].name). It gets
        (Call, ctx) and returns a hooks-v1 result, or None for {action: continue}."""
        def put(fn):
            if name in self.hooks:
                raise ToolDefinitionError(f"hook {name}: registered twice")
            self.hooks[name] = fn
            return fn
        return put

    def decision(self, provider: str, question: str):
        """Register the answer of a decision provider the manifest declares to one question. It
        gets (Ask, ctx) and returns {label, confidence?, probabilities?} or a label."""
        def put(fn):
            if (provider, question) in self.decisions:
                raise ToolDefinitionError(f"provider {provider}: {question} registered twice")
            self.decisions[(provider, question)] = fn
            return fn
        return put

    def backend(self, name: str):
        """Register a contributed backend's handler: it gets (BackendRequest, ctx) and returns
        {status, body} or {status, events}."""
        def put(fn):
            self.backends[name] = fn
            return fn
        return put

    # ------------------------------------------------------------------ the manifest's word
    def check(self) -> list[str]:
        """What the code registers against what the manifest declares, one line a difference."""
        c = self.manifest.get("contributes") or {}
        out = []
        declared = {t.get("name"): t for t in c.get("tools") or []}
        for n, t in self.tools.items():
            m = declared.get(n)
            if m is None:
                out.append(f"tool {n}: the manifest does not declare it")
                continue
            for k in ("inputSchema", "annotations", "description"):
                if m.get(k) != t.get(k):
                    out.append(f"tool {n}: the code and the manifest disagree on {k}")
        for n in declared:
            if n not in self.tools:
                out.append(f"tool {n}: declared, no handler")
        for t in declared.values():
            out += [f"manifest: {w}" for w in validate_tool(t)]
        hooks = {h.get("name") for h in c.get("hooks") or []}
        out += [f"hook {n}: the manifest does not declare it" for n in self.hooks
                if n not in hooks]
        out += [f"hook {n}: declared, no handler" for n in sorted(hooks - set(self.hooks))]
        provs = {p.get("name"): set(p.get("questions") or [])
                 for p in c.get("decision_providers") or []}
        for (p, q) in self.decisions:
            if p not in provs:
                out.append(f"provider {p}: the manifest does not declare it")
            elif q not in provs[p]:
                out.append(f"provider {p}: the manifest does not list the question {q}")
        for p, qs in provs.items():
            for q in sorted(qs):
                if (p, q) not in self.decisions:
                    out.append(f"provider {p}: declared for {q}, no handler")
        bks = {b.get("name") for b in c.get("backends") or []}
        out += [f"backend {n}: the manifest does not declare it" for n in self.backends
                if n not in bks]
        out += [f"backend {n}: declared, no handler" for n in sorted(bks - set(self.backends))]
        return out

    # ------------------------------------------------------------------ the loop
    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        with self._wlock:
            self._out.write(line)
            self._out.flush()

    def _reply(self, rid, result=None, error: tuple | None = None) -> None:
        if error is None:
            self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        else:
            code, message, data = (tuple(error) + (None,))[:3]
            e = {"code": code, "message": message}
            if data is not None:
                e["data"] = data
            self._send({"jsonrpc": "2.0", "id": rid, "error": e})

    def _initialize(self, params: dict) -> dict:
        caps = {"tools": {}} if self.tools else {}
        return {"protocolVersion": PROTOCOL, "capabilities": caps,
                "serverInfo": {"name": self.name, "version": self.version,
                               "sdk": f"tandemapp_plugin {SDK_VERSION}"}}

    def _tool_call(self, params: dict, ctx: Context) -> dict:
        name = params.get("name")
        fn = self._tool_fns.get(name)
        if fn is None:
            raise LookupError(f"no tool {name!r}")
        args = params.get("arguments") or {}
        try:
            got = fn(args, ctx)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if isinstance(got, str):
            return {"content": [{"type": "text", "text": got}]}
        if isinstance(got, list):
            return {"content": got}
        if isinstance(got, dict) and "content" in got:
            return got
        out = {"content": [{"type": "text", "text": json.dumps(got)}]}
        if isinstance(got, dict):
            out["structuredContent"] = got
        return out

    def _hook_call(self, params: dict, ctx: Context) -> dict:
        fn = self.hooks.get(params.get("hook"))
        if fn is None:
            raise LookupError(f"no hook {params.get('hook')!r}")
        got = fn(Call(params), ctx)
        if got is None:
            return {"action": "continue"}
        if not isinstance(got, dict) or got.get("action") not in ACTIONS:
            raise ValueError(f"a hook answers {{action: {', '.join(ACTIONS)}, ...}}")
        return got

    def _decision(self, params: dict, ctx: Context) -> dict:
        key = (params.get("provider"), params.get("question"))
        fn = self.decisions.get(key)
        if fn is None:
            raise LookupError(f"no provider {key[0]!r} for {key[1]!r}")
        a = Ask(params)
        got = fn(a, ctx)
        if isinstance(got, str):
            got = {"label": got}
        if not isinstance(got, dict) or not isinstance(got.get("label"), str):
            raise ValueError("a provider answers {label, confidence?}")
        if a.options and got["label"] not in a.options:
            raise ValueError(f"the label {got['label']!r} is not one of the options")
        return got

    def _backend(self, params: dict, ctx: Context) -> dict:
        fn = self.backends.get(params.get("backend"))
        if fn is None:
            raise LookupError(f"no backend {params.get('backend')!r}")
        got = fn(BackendRequest(params), ctx)
        if not isinstance(got, dict) or "status" not in got:
            raise ValueError("a backend answers {status, body} or {status, events}")
        return got

    def handle(self, msg: dict) -> None:
        """Answer one request of the host's (on a worker thread)."""
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        ctx = Context(self, rid)
        try:
            if method == "initialize":
                res = self._initialize(params)
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": list(self.tools.values())}
            elif method == "tools/call":
                res = self._tool_call(params, ctx)
            elif method == "hooks/call":
                res = self._hook_call(params, ctx)
            elif method == "decision/ask":
                res = self._decision(params, ctx)
            elif method == "backend/request":
                res = self._backend(params, ctx)
            else:
                raise LookupError(f"no method {method}")
        except LookupError as exc:
            return self._reply(rid, error=(NO_METHOD, str(exc).strip("'\"")))
        except ValueError as exc:
            return self._reply(rid, error=(BAD_PARAMS, str(exc)))
        except Exception as exc:                                   # noqa: BLE001
            self.log(f"[sdk] {method} failed: {type(exc).__name__}: {exc}")
            self.log(traceback.format_exc().rstrip())
            return self._reply(rid, error=(INTERNAL, f"{type(exc).__name__}: {exc}"))
        self._reply(rid, res)

    def _dispatch(self, msg: dict) -> None:
        if "method" not in msg:                     # an answer to one of the plugin's requests
            with self._plock:
                box = self._pending.get(msg.get("id"))
            if box is not None:
                box.put(msg)
            return
        if "id" not in msg:                         # a notification: nothing to answer
            return
        threading.Thread(target=self.handle, args=(msg,), name=f"sdk-{msg.get('method')}",
                         daemon=True).start()

    def run(self, strict: bool = True) -> int:
        """The loop: until stdin closes. `strict`: refuse to start when the code and the manifest
        differ (`check`)."""
        if strict:
            diff = self.check()
            if diff:
                for d in diff:
                    self.log(f"[sdk] {d}")
                self.log("[sdk] not started: the code and plugin.json differ")
                return 2
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.log("[sdk] a line that is not JSON, ignored")
                continue
            if isinstance(msg, dict):
                self._dispatch(msg)
        self._closed.set()
        with self._plock:
            for box in self._pending.values():
                box.put(None)
        return 0

    def main(self) -> None:
        """`run()`, then exit with its code."""
        sys.exit(self.run())
