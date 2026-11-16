# The plugin SDK

`tandemapp_plugin` is a small Python package for writing TandemApp [plugins](plugins.md): it reads the requests of the plugin host, calls your functions and writes the answers, so a plugin is a few decorated functions instead of a JSON-RPC reader. It uses the standard library only and lives in [`sdk/python`](../sdk/python).

Two complete plugins are built on it: [`note-webhook`](../sdk/python/examples/note-webhook) (an observer hook and a tool that post notes to a webhook) and [`keyword-router`](../sdk/python/examples/keyword-router) (a decision provider for the conductor's classify and judge questions). The tests install and run both through the plugin host.

## Start a plugin

```
pip install sdk/python                   # or: PYTHONPATH=sdk/python python -m tandemapp_plugin ...
tandemapp-plugin new my-tool             # --kind tool (default), hook or provider; --trust host
python tools/plugin_check.py my-tool     # the install's own checks, nothing installed
```

`new` writes the folder `my-tool/` with `plugin.json`, `main.py` and a copy of the SDK (`tandemapp_plugin/`), and never writes into a folder that exists. A sandboxed plugin sees only its own tree, so the SDK travels inside it: `tandemapp-plugin vendor <folder>` copies it again after an SDK update, and `tandemapp-plugin check <folder>` says when a copy is out of date or a declared tool would be refused.

Then install it like any plugin: `POST /v1/fleet/plugins {"source": {"type": "folder", "path": ...}}`, grant what it asks for, enable it ([plugins](plugins.md#install)). Sign it with `tools/plugin_sign.py` when it should run at trust host on a signature ([signing](plugins.md#sign-a-plugin)).

## A plugin

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # the SDK copy beside it
from tandemapp_plugin import Plugin, ToolError

plugin = Plugin()                     # reads plugin.json beside the script

@plugin.tool("greet", description="Say hello to someone by name.",
             input_schema={"type": "object", "properties": {"name": {"type": "string"}},
                           "required": ["name"], "additionalProperties": False},
             read_only=True, destructive=False, idempotent=True, open_world=False)
def greet(args, ctx):
    if not args["name"].strip():
        raise ToolError("a name, please")            # answered with isError
    return f"{plugin.settings.get('greeting', 'Hello')}, {args['name']}."

if __name__ == "__main__":
    plugin.main()
```

The manifest still declares every contribution ([the manifest](plugins.md#the-manifest)): the host offers only what `plugin.json` declares, with the manifest's schema. `plugin.main()` first compares the two (`plugin.check()`) and refuses to start when they differ, one line a difference on stderr, so a schema changed in one place and not the other shows in the plugin's log instead of as a refused call.

## Contributions

| decorator | the handler gets | it returns |
|-|-|-|
| `@plugin.tool(name, description=, input_schema=, read_only=, destructive=, idempotent=, open_world=, title=None)` | `(args, ctx)`: the arguments, already checked against the schema by the host | text, a list of MCP content items, a full result `{content, ...}`, or any JSON value (sent as text and `structuredContent`); `raise ToolError(text)` for an error result |
| `@plugin.hook(name)` | `(call, ctx)`: `call.event` at the hook's payload level, `call.stage`, `call.mode`, `call.request`, `call.hook` | a hooks-v1 result `{action: continue \| modify \| deny \| decide, patch?, decision?, reason?, labels?}`, or `None` for continue |
| `@plugin.decision(provider, question=)` | `(ask, ctx)`: `ask.question`, `ask.options`, `ask.text` (redacted or full) or `ask.chars` (meta), `ask.instructions`, `ask.model`, `ask.deadline_ms` | `{label, confidence?, probabilities?}` or the label; the label must be one of `ask.options` |
| `@plugin.backend(name)` | `(req, ctx)`: `req.method`, `req.path`, `req.body` | `{status, body}`, or `{status, events: [...]}` for a stream |

A provider registers once per question it answers, so one function can serve a question and another the next. `ctx.plugin` is the plugin and `ctx.id` the host's request id.

**The four annotations.** A tool states `read_only`, `destructive`, `idempotent` and `open_world` (MCP's `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), each true or false, and an input schema of type object that says `"additionalProperties": false` on every object in it, nested ones, array items and `$defs` it refers to included. The decorator refuses a tool that misses one (`ToolDefinitionError`, naming the annotation or the place in the schema), as the install would. `validate_tool(tool)` gives the same lines for a tool written as a dict, e.g. one of a manifest.

## What a plugin may ask

| | what | needs |
|-|-|-|
| `plugin.settings` | its settings as the host passed them at start, each default filled in | nothing |
| `plugin.read_settings()` | its settings as they are now (`tandem/settings`) | `settings:read` |
| `plugin.secret(name)` | a secret granted to it: from its environment, else asked of the host (`tandem/secret`); `None` when granted and not set | `secrets:<name>` |
| `plugin.engine(params)` | engine control (`tandem/engine`) | refused until engine control for plugins exists |
| `plugin.request(method, params)` | any request of the plugin's to the host | its capability |
| `plugin.capabilities`, `plugin.granted(cap)` | what was granted | nothing |
| `plugin.data_dir` | its own folder (`TANDEM_PLUGIN_DATA`) | nothing |
| `plugin.log(...)` | a line to its log (stderr; stdout is the protocol) | nothing |

A refusal raises `Refused`, whose `capability` names what was missing; any other error from the host raises `HostError`. A handler may ask the host while it runs: the loop reads on one thread and answers each request on its own, so other calls are not held up meanwhile.

## The examples

**note-webhook** contributes the observer hook `finished` at `request.finished` (payload meta: the request's id, model, tokens and time) and the tool `send_note`. Both post JSON to the `url` setting, with the secret `WEBHOOK_TOKEN` as the bearer when it is granted and set. It asks for `network:hooks.example.com`: change that to your webhook's host (and its `url` default) before you install it, since a sandboxed plugin reaches only the hosts it was granted.

**keyword-router** contributes the provider `rules` for `classify` and `judge`. Its `rules` setting is a list of `{question, label, words}`; the first rule of the question whose word the text holds names the label, when the question offers that label, else it answers the first option with confidence 0. It asks for `prompts:redacted` (the text after the conductor's redaction) and `settings:read`, and reads its rules from the host at each question when that is granted. To have it answer, add a decision provider of kind `plugin` with target `keyword-router/rules` ([decision providers](plugins.md#decision-providers)).

## Tests

`tests/test_plugin_sdk.py` installs both examples and a scaffold of each kind, starts them through the plugin host (sandboxed, with the tests' stand-in runtime, and at trust host) and calls them: the webhook is a server on the test machine, so no example needs the network. It also checks the SDK's refusals against the install's, the examples' copies of the SDK against `sdk/python`, the engine-stats manifests through the installer, and this page's links. The box harness runs it with the rest of the suite; `.github/workflows/tests.yml` runs it as its own job, by hand.
