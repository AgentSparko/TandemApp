# Reference: variables and knobs

The proxy, the collector and the plugins read `TANDEM_*` variables. TandemApp's code reads some of the engine's `QSE_*` variables too, where it starts chatd, runs TandemNode or reads the engine's files. Inside an engine the conductor reads the `QWEN38_*` knobs the engine contract names (`contracts/engine-ext-v1/knobs.json`), with the defaults of `tandemapp/knobs.py`. `tools/gen_reference.py` builds every table here from the code. The old sign-in's variables, which the first start imports once, are in [auth.md](../auth.md#moving-from-the-old-sign-in).

<!-- generated: tandem-vars by tools/gen_reference.py; do not edit by hand -->

### TANDEM_* variables: the proxy, the collector and the plugins

| Variable | Example | What it sets |
|-|-|-|
| `TANDEM_BACKENDS` | `deepseek,vllm1` | the backends the environment declares |
| `TANDEM_BACKEND_<ID>_KIND` | `deepseek` | generic \| tandemllm \| vllm \| sglang \| openai \| deepseek \| anthropic |
| `TANDEM_BACKEND_<ID>_URL` | `https://api.deepseek.com/v1` |   |
| `TANDEM_BACKEND_<ID>_KEY` | `(a key)` | held in memory, never stored |
| `TANDEM_BACKEND_<ID>_LABEL` | `DeepSeek` |   |
| `TANDEM_BACKEND_<ID>_MODELS` | `deepseek-flash,deepseek-v4-pro` |   |
| `TANDEM_BACKEND_<ID>_MODELS_MODE` | `discover` | discover (GET /models, plus MODELS) \| list |
| `TANDEM_BACKEND_<ID>_FALLBACK` | `openrouter` | backends tried after this one, in order |
| `TANDEM_BACKEND_<ID>_MAX_INFLIGHT` | `none` | requests at once, past it a fallback or a 503; none (the default): no limit from the proxy, the backend takes what it accepts |
| `TANDEM_BACKEND_<ID>_ENABLED` | `1` |   |
| `TANDEM_BACKEND_<ID>_INJECT_USAGE` | `1` | ask a stream for its usage frame |
| `TANDEM_BACKEND_<ID>_HARNESS` | `off` | off \| shadow \| on |
| `TANDEM_BACKEND_<ID>_HARNESS_PROFILE` | `coding` | the conductor profile of its requests |
| `TANDEM_BACKEND_<ID>_WRITER` | `auto` | the background memory writer on this backend (proxy/memwriter.py): auto (on, off for a paid cloud model) \| off \| shadow \| on |
| `TANDEM_BACKEND_<ID>_WRITER_USD_DAY` | `0.25` | its spend cap a day on a paid model |
| `TANDEM_BACKEND_<ID>_WRITER_RUNS_DAY` | `48` | its runs a day |
| `TANDEM_BACKEND_<ID>_WRITER_PROMPT_TOKENS` | `32000` | its prompt at most (older messages dropped) |
| `TANDEM_BACKEND_<ID>_TIMEOUT_CONNECT` | `10` | seconds; also \_TIMEOUT\_FIRST\_BYTE, \_TIMEOUT\_IDLE |
| `TANDEM_BACKEND_<ID>_ANTHROPIC_AUTH` | `x-api-key` | kind anthropic: x-api-key \| bearer; also \_ANTHROPIC\_API\_VERSION, \_ANTHROPIC\_SAMPLING (drop \| pass), \_ANTHROPIC\_DEFAULT\_MAX\_TOKENS, \_ANTHROPIC\_AUTO\_CACHE, \_ANTHROPIC\_THINKING\_DISPLAY (summarized \| ... |
| `TANDEM_BACKEND_<ID>_HOST` | `auto` | where the System view reads the backend's machine: auto (this machine when the URL is), same, collector, exporter, off |
| `TANDEM_BACKEND_<ID>_HOST_URL` | `(a URL)` | the remote collector (python -m proxy.hostcollect), or a node\_exporter page |
| `TANDEM_BACKEND_<ID>_HOST_DCGM_URL` | `(a URL)` | a DCGM exporter page (mode exporter) |
| `TANDEM_COLLECTOR_TOKEN` | `...` | the remote collectors' bearer token, in memory only (proxy/hostcollect.py) |
| `TANDEM_ROUTE_<n>` | `gpt-*=openai` | routing rules, ordered by n |
| `TANDEM_PRICE_<n>` | `deepseek/deepseek-flash=0.30,0.006,1.2` | USD per million: input, cached input, output, and optionally cache write, then the 1-hour cache write |
| `TANDEM_PRICE_<n>_OFFPEAK` | `0.15,0.003,0.6` | the same model outside the provider's peak hours (DeepSeek) |
| `TANDEM_DEFAULT_BACKEND` | `engine` | a model id that no deployment, rule or backend serves goes to this backend as asked (a TandemLLM engine answers any id with its own model); unset: OpenAI's 404 |
| `TANDEM_SECRETS_KEY` | `<base64 of 32 bytes>` | the master key (proxy/secrets.py) |
| `TANDEM_APP_ENV` |   | read in `packaging/linux/tandem-app.sh` |
| `TANDEM_APP_PYTHON` |   | read in `packaging/linux/tandem-app.sh, packaging/tandemapp_install.py` |
| `TANDEM_APP_STATE` |   | read in `packaging/container-start.sh, packaging/linux/tandem-app.service` |
| `TANDEM_AUTH` |   | read in `packaging/app.env.example, proxy/authlegacy.py` |
| `TANDEM_BACKEND_DEEPSEEK_KEY` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_DEEPSEEK_KIND` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_DEEPSEEK_LABEL` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_FALLBACK` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_HOST` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_HOST_DCGM_URL` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_HOST_URL` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_KIND` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_MAX_INFLIGHT` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_TIMEOUT_FIRST_BYTE` |   | read in `packaging/app.env.example` |
| `TANDEM_BACKEND_VLLM1_URL` |   | read in `packaging/app.env.example` |
| `TANDEM_CHAT` |   | read in `packaging/app.env.example, packaging/container-start.sh` |
| `TANDEM_CHAT_ENGINE_BACKEND` |   | read in `proxy/chatengine.py` |
| `TANDEM_CHAT_ENV` |   | read in `packaging/app.env.example, packaging/linux/chatd.env.example` |
| `TANDEM_CLIENTS_JSON` |   | read in `proxy/app.py` |
| `TANDEM_COLLECT` |   | read in `proxy/launch.py` |
| `TANDEM_COLLECT_BASE` |   | read in `proxy/launch.py` |
| `TANDEM_COLLECT_TOKEN` |   | read in `plugins/sglang/tandem_sglang_plugin/_push.py, plugins/vllm/tandem_vllm_plugin/__init__.py` |
| `TANDEM_COLLECT_URL` |   | read in `plugins/sglang/tandem_sglang_plugin/_push.py, plugins/vllm/tandem_vllm_plugin/__init__.py` |
| `TANDEM_CONDUCTOR` |   | read in `packaging/app.env.example, packaging/linux/tandem-app.sh` |
| `TANDEM_CONFIG_DB` |   | read in `packaging/app.env.example, proxy/__main__.py` |
| `TANDEM_DASHBOARD_LATER` |   | read in `packaging/tandemapp_build.py` |
| `TANDEM_DASHBOARD_SHA256` |   | read in `packaging/tandemapp_build.py, tandemapp/bundle.py` |
| `TANDEM_DASHBOARD_URL` |   | read in `packaging/tandemapp_build.py, tandemapp/bundle.py` |
| `TANDEM_DECISION_REPLAY` |   | read in `proxy/app.py` |
| `TANDEM_DEPLOY` |   | read in `proxy/authlocal.py` |
| `TANDEM_DIR` |   | read in `proxy/nodeagent.py` |
| `TANDEM_DMI_DIR` |   | read in `proxy/fleettrust.py` |
| `TANDEM_ENGINE_INSTALL_SH` |   | read in `packaging/app.env.example, proxy/fleetupdate.py` |
| `TANDEM_ENGINE_REF` |   | read in `packaging/app.env.example, proxy/fleethealth.py` |
| `TANDEM_FLEET_RETENTION_DAYS` |   | read in `proxy/__main__.py` |
| `TANDEM_FLEET_STORE` |   | read in `proxy/__main__.py` |
| `TANDEM_HOST` |   | read in `packaging/app.env.example, packaging/tandemapp_install.py` |
| `TANDEM_IDLE_TIMEOUT_S` |   | read in `proxy/__main__.py, proxy/app.py` |
| `TANDEM_INGRESS_API` |   | read in `proxy/exposure.py` |
| `TANDEM_INSTALL_PYTHON` |   | read in `packaging/install.sh` |
| `TANDEM_LINK_KEY` |   | read in `proxy/nodeengines.py` |
| `TANDEM_LINK_LISTEN` |   | read in `proxy/nodeengines.py` |
| `TANDEM_LINK_PEER` |   | read in `proxy/nodeengines.py` |
| `TANDEM_LOCAL_MODE` |   | read in `packaging/app.env.example, packaging/tandemapp_install.py` |
| `TANDEM_LOCAL_NODE` |   | read in `proxy/app.py, proxy/fleet.py` |
| `TANDEM_MACHINE_ID_FILE` |   | read in `proxy/fleettrust.py` |
| `TANDEM_MIN_FREE_GIB` |   | read in `proxy/nodeengines.py` |
| `TANDEM_NODE_AGENT` |   | read in `packaging/app.env.example, packaging/linux/tandem-app.sh` |
| `TANDEM_NODE_CHAT` |   | read in `proxy/nodeagent.py, proxy/nodechat.py` |
| `TANDEM_NODE_DIR` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_HOST` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_MDNS` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_PORT` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_SECRET` |   | read in `proxy/app.py, proxy/nodeagent.py` |
| `TANDEM_NODE_VIRTUAL` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_WEIGHTS` |   | read in `proxy/nodeagent.py` |
| `TANDEM_NODE_WHEEL` |   | read in `proxy/app.py, proxy/fleetupdate.py` |
| `TANDEM_NOTIFY_URL` |   | read in `tandemapp/conductor/monitor/notify.py, tandemapp/conductor/monitor/sessiontasks.py` |
| `TANDEM_PF_PREFIX` |   | read in `proxy/nodeinstall.py, proxy/preflight.py` |
| `TANDEM_PF_SELF` |   | read in `proxy/nodeagent.py, proxy/preflight.py` |
| `TANDEM_PLUGIN` |   | read in `proxy/pluginhost.py` |
| `TANDEM_PLUGIN_CAPABILITIES` |   | read in `proxy/pluginhost.py` |
| `TANDEM_PLUGIN_DATA` |   | read in `proxy/pluginhost.py, proxy/pluginrt.py` |
| `TANDEM_PLUGIN_EGRESS_PORT` |   | read in `proxy/pluginrt.py` |
| `TANDEM_PLUGIN_MEM_MB` |   | read in `proxy/pluginrt.py` |
| `TANDEM_PLUGIN_SANDBOX` |   | read in `proxy/enginecodes.py, proxy/pluginrt.py` |
| `TANDEM_PLUGIN_SETTINGS` |   | read in `proxy/pluginhost.py` |
| `TANDEM_PLUGIN_VERSION` |   | read in `proxy/pluginhost.py` |
| `TANDEM_PORT` |   | read in `packaging/app.env.example, packaging/tandemapp_install.py` |
| `TANDEM_PRICE_1` |   | read in `packaging/app.env.example` |
| `TANDEM_PRICE_1_OFFPEAK` |   | read in `packaging/app.env.example` |
| `TANDEM_ROUTE_1` |   | read in `packaging/app.env.example` |
| `TANDEM_SCRATCH` |   | read in `tandemapp/conductor/packages.py, tandemapp/knobs.py` |
| `TANDEM_SETUP_CODE_FILE` |   | read in `packaging/app.env.example, proxy/authlegacy.py` |
| `TANDEM_SSH_SECRET` |   | read in `proxy/sshonce.py` |
| `TANDEM_STAGE` |   | read in `proxy/nodeengines.py` |
| `TANDEM_STAGE_CACHES` |   | read in `proxy/nodeengines.py` |
| `TANDEM_STAGE_CACHE_DIR` |   | read in `proxy/nodeengines.py` |
| `TANDEM_STAGE_CACHE_GB` |   | read in `proxy/nodeengines.py` |
| `TANDEM_STAGE_SNAPSHOTS_GB` |   | read in `proxy/nodeengines.py` |
| `TANDEM_TRUSTED_PROXY` |   | read in `packaging/app.env.example, proxy/accounts.py` |
| `TANDEM_URL` |   | read in `tandemapp/conductor/packages.py, tandemapp/knobs.py` |
| `TANDEM_USAGE_LEDGER` |   | read in `packaging/app.env.example, proxy/__main__.py` |

<!-- /generated -->

<!-- generated: qse-vars by tools/gen_reference.py; do not edit by hand -->

### QSE_* variables TandemApp's code reads

| Variable | Read in |
|-|-|
| `QSE_ADMIN_TOKEN` | `ops/chatd.service, ops/chatd.sh, proxy/authlegacy.py` and 3 more |
| `QSE_ALLOW_FROM` | `proxy/enginecodes.py` |
| `QSE_BACKENDS` | `proxy/__main__.py, proxy/backends_import.py` |
| `QSE_CHATD` | `ops/chatd.service, proxy/engineapi.py, proxy/nodeagent.py` and 1 more |
| `QSE_CHATD_ENV` | `ops/chatd.sh, proxy/nodechat.py` |
| `QSE_CHAT_DATA` | `ops/chatd.sh, ops/sandbox/install.sh, ops/sandbox/uninstall.sh` and 6 more |
| `QSE_CHAT_ENGINE` | `ops/chatd.sh, packaging/linux/chatd.env.example, proxy/appchat.py` and 1 more |
| `QSE_CHAT_ENGINE_KEY` | `packaging/linux/chatd.env.example, proxy/appchat.py, proxy/tokenscli.py` |
| `QSE_CHAT_FORWARD_KEY` | `ops/chatd.sh, packaging/linux/chatd.env.example, proxy/appchat.py` and 1 more |
| `QSE_CHAT_HOST` | `packaging/linux/chatd.env.example, proxy/appchat.py` |
| `QSE_CHAT_LOG` | `ops/chatd.sh, proxy/appchat.py, proxy/nodechat.py` |
| `QSE_CHAT_MAX_RUNS` | `packaging/linux/chatd.env.example, proxy/appchat.py` |
| `QSE_CHAT_URL` | `ops/chatd.sh, packaging/linux/chatd.env.example, packaging/tandemapp_install.py` and 6 more |
| `QSE_DASHBOARD_LOGIN` | `proxy/authlegacy.py` |
| `QSE_DISK_CACHE` | `proxy/nodeengines.py` |
| `QSE_EXT` | `proxy/settingsengine.py, tandemapp/engine_ext.py` |
| `QSE_K8S_EGRESS_PORT` | `proxy/pluginrt.py` |
| `QSE_LIVE_DECODE_WARMUP_TOKENS` | `tandemapp/live.py` |
| `QSE_LIVE_HZ` | `tandemapp/live.py` |
| `QSE_LIVE_MOSTLY_CACHED_PCT` | `tandemapp/live.py` |
| `QSE_LIVE_MOSTLY_CACHED_TOKENS` | `tandemapp/live.py` |
| `QSE_LIVE_NOW_WINDOW_S` | `tandemapp/live.py` |
| `QSE_LIVE_PREFILL_WARMUP_CHUNKS` | `tandemapp/live.py` |
| `QSE_MAX_BODY_MB` | `tandemapp/http_body.py` |
| `QSE_METRICS_TOKEN` | `ops/chatd.service, ops/chatd.sh, proxy/authlegacy.py` and 1 more |
| `QSE_MODE` | `proxy/engineonboard.py, proxy/nodechat.py, proxy/settingsengine.py` |
| `QSE_PARALLEL` | `proxy/nodeengines.py` |
| `QSE_RUNNER_BUSY_WORK` | `tandemapp/live.py` |
| `QSE_SANDBOX_BACKEND` | `packaging/linux/chatd.env.example, proxy/app.py, proxy/fleethealth.py` and 1 more |
| `QSE_SANDBOX_FLOOR_GB` | `packaging/linux/chatd.env.example` |
| `QSE_SANDBOX_IMAGE` | `proxy/fleethealth.py, proxy/pluginrt.py` |
| `QSE_SANDBOX_MAX_RUNNING` | `packaging/container-start.sh, packaging/linux/chatd.env.example` |
| `QSE_SANDBOX_MEM_GB` | `packaging/container-start.sh, packaging/linux/chatd.env.example` |
| `QSE_SANDBOX_SOCKET` | `ops/sandbox/install.sh, packaging/container-start.sh, packaging/linux/chatd.env.example` and 5 more |
| `QSE_SECRETS` | `ops/chatd.sh, proxy/nodechat.py` |
| `QSE_SIDE_MAX_LEN` | `proxy/nodeengines.py` |
| `QSE_STATE_DIR` | `ops/chatd.sh, proxy/nodechat.py` |
| `QSE_STATUS_PORT` | `tandemapp/static.py` |
| `QSE_TEST` | `proxy/app.py, tandemapp/ledger.py` |
| `QSE_USAGE_LEDGER` | `tandemapp/ledger.py` |

<!-- /generated -->

<!-- generated: ext-knobs by tools/gen_reference.py; do not edit by hand -->

### The QWEN38_* knobs the conductor reads inside an engine (engine-ext-v1 knobs.json)

| Variable | Code default | Read in |
|-|-|-|
| `QWEN38_CLIENT_CWD` | `note` | `tandemapp/conductor/router.py` |
| `QWEN38_CONDUCTOR_FIT` | `model` | `tandemapp/conductor/modelfit.py` |
| `QWEN38_FAIL_OPEN` | `general` | `tandemapp/conductor/router.py` |
| `QWEN38_LIBRARY_HOLD` | `message` | `tandemapp/conductor/libsearch.py` |
| `QWEN38_MEMORY_ROUTES` | `auto` | `tandemapp/conductor/memory.py` |
| `QWEN38_MODEL_NAME` | `none` | `tandemapp/conductor/schema.py` |
| `QWEN38_NARRATOR_MODEL` | `none` | `tandemapp/conductor/narrator.py` |
| `QWEN38_NOTES_HEAD` | `off` | `tandemapp/conductor/router.py` |
| `QWEN38_NOTES_HEAD_ORDER` | `memory` | `tandemapp/conductor/router.py` |
| `QWEN38_NOTE_ANCHOR` | `on` | `tandemapp/conductor/router.py` |
| `QWEN38_NOTE_DEDUP` | `parts` | `tandemapp/conductor/router.py` |
| `QWEN38_PACKAGE_HOLD` | `pick` | `tandemapp/conductor/router.py` |
| `QWEN38_SESSION_GENERATIONS` | `on` | `tandemapp/conductor/router.py` |
| `QWEN38_SHADOW_ASK` | `idle` | `tandemapp/conductor/deferred.py` |
| `QWEN38_SHADOW_ASK_IDLE_S` | `5` | `tandemapp/conductor/deferred.py` |
| `QWEN38_STATUS_CLIENTS` | `open-webui` | `tandemapp/conductor/states.py` |
| `QWEN38_TANDEM_SCRATCH` | `none` | `tandemapp/conductor/packages.py` |
| `QWEN38_TANDEM_URL` | `none` | `tandemapp/conductor/packages.py` |
| `QWEN38_TOOL_FIT` | `on` | `tandemapp/conductor/router.py` |

<!-- /generated -->
