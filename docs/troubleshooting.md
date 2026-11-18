# Troubleshooting

Each entry is a symptom, what causes it, and what to do. The dashboard's Dev view shows the server log live. The engine's own entries (installing, the engine, memory notes) are in [TandemEngine's troubleshooting](https://github.com/TandemLLM/TandemEngine/blob/main/docs/troubleshooting.md).

## The dashboard

**Nobody can sign in, or an account lost its password.** [auth.md](auth.md#recovery) has the steps: a one-time code from an admin or `tandem-app users reset-password NAME` on the controller's machine, and `tandem-app users setup-code` while no owner exists.

## The Chat tab

The Chat tab is in beta ([chat.md](chat.md)).

**The Chat tab asks before every bash command.** The chat runs in Ask mode. The pill in the composer's options row names the mode; pick Auto there, and calls that stay inside the chat's sandbox run without a prompt from the next call on. The default for new chats is at the top of the Tools settings page. A chat from before 0.8.2 has no mode of its own and runs in the default ([chat.md](chat.md#permission-modes)).

**Auto still asks.** The call leaves the sandbox: `web_fetch` to a domain this chat has not fetched yet, an MCP tool its server does not mark read-only, an imported Open WebUI tool that imports a network or process module, or `rm` aimed at `/`, `/workspace` or a home directory. A tool set to “ask” in the tool settings asks in every mode, and a deny rule refuses in every mode, YOLO included.

**“Always allow” does not stop the prompt.** Before 0.8.2 it saved only the first word of a chained command, which never covered the whole command, so one chat saved `cd` 11 times. On 0.8.2 it saves a rule for every part and names them in the prompt. A command with `$(...)` or backticks still matches no rule and offers no “Always”; Auto runs it without asking when it stays in the sandbox.

## vLLM and SGLang behind the proxy

**vLLM answers 400 to every request with tools.** vLLM refuses tools without a tool parser. Start it with `--enable-auto-tool-choice --tool-call-parser <parser>` (`qwen3_coder` for the Qwen3.5 and Qwen3.8 models). The memory writer sends the session's tools with `tool_choice: "none"`, so it needs the parser too.

**SGLang returns tool calls as text.** Start it with `--tool-call-parser <parser>`. SGLang does not hold the model to `tool_choice: "none"`; the memory writer cuts any call it writes.

**The SGLang card says the metrics page is off.** SGLang answers `/metrics` with 404 unless it runs with `--enable-metrics`, and the plugin's hooks need it too.

**Cached tokens read “not reported by this backend”.** vLLM needs `--enable-prompt-tokens-details`, SGLang `--enable-cache-report`. A generic server fills `prompt_tokens_details.cached_tokens` or not; the proxy never estimates.

**Reasoning tokens are null, or the reasoning shows inside the answer.** Start vLLM or SGLang with `--reasoning-parser <parser>` (`qwen3` for Qwen).

**The card says “L1 only”.** No batch from the plugin for 60 s. In the engine's own Python, check that the plugin is installed, that `TANDEM_COLLECT_URL` names this proxy and this backend's id and is reachable from the engine's machine, and that `TANDEM_COLLECT_TOKEN` is the proxy's token. The engine's log has one line when the plugin fails to load; the engine starts either way. `tandem-app launch ... --dry-run` prints the variables it would set.

**Rows say l2_join: approximate.** The proxy could not tag the request (the Responses API, SGLang with `n` above 1) and matched it by time and token counts. A client that sends its own `X-Request-Id` (vLLM) or `rid` (SGLang) keeps it, and the join uses it.

## The proxy

**401 or 403 from another device.** Every request from another device needs an API token (`Authorization: Bearer tdm-...`); 401 names a token that is unknown, revoked or expired. 403 `api_not_exposed` means the protocol's switch is on this machine: set it to network under Settings, API. Make a token on the Access page or with `tandem-app tokens create`.

**A save in the Backends view answers 422 and names a variable.** A `TANDEM_*` variable sets that field, and the environment wins. Change the variable, or remove it and restart.

**A request fails with key_unreadable.** The master key that encrypted the backend's key does not answer: `TANDEM_SECRETS_KEY` changed, the keychain entry or `secrets.key` is gone, or the database was copied without its key. `tandem-app secrets status` says which source answers now. Type the backend's key in again.

**TandemApp refuses to start and names a variable.** An unknown `TANDEM_*` variable, an unknown backend id, or a value the schema refuses. `tandem-app check --env-file app.env` prints the same message without starting.

**404 model_not_found.** No rule, prefix or backend serves the id. When two backends serve one id, use `<backend>/<model>`, give one a public id, or add a routing rule.

**The cost column is empty.** The model has no price. Set one on the model's row, or with `TANDEM_PRICE_<n>`.

**The System view shows no processes for a backend.** The host setting is `exporter`, the process belongs to another user, or the proxy runs in docker without `--pid=host`. For a remote machine, run `tandem-app hostcollect` there with a token.


More entries for an install on Kubernetes are in [install-kubernetes.md](install-kubernetes.md#10-troubleshooting).
