# Reference: metrics and the usage ledger

## Metrics

The controller's `/metrics` carries the proxy's families and the mapped engine families as `qse_backend_*`; an engine in chat mode carries the conductor's, the families the engine contract marks `"owner": "ext"`. The collector's `/metrics` carries `tandem_host_*`. `tools/gen_reference.py` reads them from the metric registrations.

<!-- generated: metrics by tools/gen_reference.py; do not edit by hand -->

### Prometheus families TandemApp registers: the conductor's inside an engine, the proxy's and the collector's

| Metric | Type, labels | What it counts |
|-|-|-|
| `qse_conductor_decisions_total` | `counter, task, tier, mode, decision` | conductor decisions, passes included: the monitor's tasks and the router's class (task route) |
| `qse_conductor_actions_total` | `counter, task, action` | decisions the conductor executed, by task and action |
| `qse_conductor_hidden_tokens_total` | `counter` | tokens the model saw that the client did not send or receive (hidden closes, hidden turns, reminders, router notes new and replayed) |
| `qse_conductor_gave_up_total` | `counter` | turns the conductor ended with its GIVE\_UP sentence |
| `qse_conductor_tier1_seconds` | `histogram` | one tier 1 question (a classifier stage: the decider or Qwen), answered or not |
| `qse_conductor_tier1_failures_total` | `counter, reason` | tier 1 questions without a usable answer, by reason |
| `qse_conductor_package_kept_out_total` | `counter, why, mode` | packages the router kept out of a turn because an offered tool delivers their result, by why and by mode (act, shadow) |
| `qse_conductor_reasoning_tokens_cut_total` | `counter` | reasoning tokens written before a hidden close cut the block (the size of the blocks the conductor closed) |
| `qse_proxy_requests_total` | `counter, backend, kind, finish_reason` | requests relayed to a backend, by its id, its kind and the finish reason |
| `qse_proxy_tokens_total` | `counter, backend, type` | tokens a backend reported in `usage`, by type (prompt, cached, completion, reasoning) |
| `qse_proxy_cost_micros_total` | `counter, backend` | cost at the model's price, micro-dollars |
| `qse_upstream_retries_total` | `counter, backend, reason` | tries again before any byte reached the client, by backend and reason (429, 502, 503, connect) |
| `qse_proxy_overhead_ms` | `histogram` | the proxy's own time per request: arrival to the upstream send, plus the first frame's delay through the proxy |
| `qse_proxy_ttft_seconds` | `histogram` | arrival at the proxy to the first token frame (the network and the backend's queue inside) |
| `qse_backend_up` | `gauge, backend` | 1 when the backend's last health check passed |
| `qse_backend_inflight` | `gauge, backend` | requests relayed to the backend now |
| `qse_backend_scrape_age_seconds` | `gauge, backend` | seconds since the backend's own /metrics page was last read (vLLM, SGLang) |
| `qse_backend_plugin_age_seconds` | `gauge, backend` | seconds since the plugin inside the backend's engine last posted (vLLM, SGLang with tandemllm-\*-plugin) |
| `qse_backend_plugin_joins` | `gauge, backend, join` | requests whose ledger row got the engine's own numbers, by how the plugin's item was matched (exact, approximate) or why it was not (unmatched, untagged) |

<!-- /generated -->

## The usage ledger

The controller keeps one SQLite row per request it relays, in the same schema as the engine's ledger, and the conductor's journal in the same file.

<!-- generated: ledger by tools/gen_reference.py; do not edit by hand -->

### The usage ledger's requests table (schema version 13)

| Column | Type, since | What it holds |
|-|-|-|
| `ts_ms` | `INTEGER, v1` | when the request arrived, ms since the epoch |
| `request_id` | `TEXT, v1` | the response id |
| `model` | `TEXT, v1` | the model name the client sent |
| `client_id` | `TEXT, v1` | k: and 12 hex digits of the bearer token's sha256, or anon |
| `client_kind` | `TEXT, v1` | read from the User-Agent: open-webui, openai-sdk, opencode, hermes, curl, dashboard, other |
| `endpoint` | `TEXT, v1` | the route |
| `stream` | `INTEGER, v1` | 1 for a streamed request |
| `status` | `INTEGER, v1` | the HTTP status |
| `finish_reason` | `TEXT, v1` | stop, length, tool\_calls, timeout, error, abandoned |
| `prompt_tokens` | `INTEGER, v1` | prompt tokens as the client sent them |
| `cached_tokens` | `INTEGER, v1` | prompt tokens served from a cache |
| `completion_tokens` | `INTEGER, v1` | tokens streamed to the client |
| `reasoning_tokens` | `INTEGER, v1` | visible reasoning tokens |
| `queue_ms` | `REAL, v1` | time waiting for the engine |
| `prompt_ms` | `REAL, v1` | prefill time |
| `ttft_ms` | `REAL, v1` | time to the first token |
| `decode_ms` | `REAL, v1` | time after the first token |
| `total_ms` | `REAL, v1` | arrival to the end |
| `decode_tps` | `REAL, v1` | (completion tokens - 1) / decode time; null for a replay |
| `prefill_tps` | `REAL, v1` | forwarded prompt tokens a second |
| `blocks` | `INTEGER, v1` | verify rounds |
| `draft_tokens` | `INTEGER, v1` | tokens the drafters proposed |
| `draft_accepted` | `INTEGER, v1` | drafted tokens the model agreed with |
| `tool_calls` | `INTEGER, v1` | tool calls in the answer |
| `thinking` | `INTEGER, v1` | 1 when the model thought |
| `cache_source` | `TEXT, v1` | where the prompt resumed: resident, end, store, disk, spill, continuation, prepared, response, none |
| `max_tokens` | `INTEGER, v1` | the output limit |
| `error_type` | `TEXT, v1` | the exception class or the refusal |
| `engine_version` | `TEXT, v1` | the VERSION file |
| `code_sha` | `TEXT, v1` | a hash over the code folders |
| `model_served` | `TEXT, v2` | TandemLLM with the conductor on |
| `profile` | `TEXT, v2` | the conductor profile |
| `profile_version` | `INTEGER, v2` | its version |
| `conductor_mode` | `TEXT, v2` | raw, shadow or on |
| `conductor_acts` | `INTEGER, v2` | the session monitor's executed acts |
| `conductor_hidden_tokens` | `INTEGER, v2` | tokens the model saw that the client did not send |
| `conductor_acted` | `INTEGER, v2` | 1 when the conductor changed what the model saw or wrote |
| `lane` | `TEXT, v2` | decode, prefill or self for a side-lane request |
| `backend` | `TEXT, v3` | the backend id of a relayed request |
| `backend_kind` | `TEXT, v3` | its kind |
| `backend_model` | `TEXT, v3` | the id sent upstream |
| `public_model` | `TEXT, v3` | the id the client sent |
| `usage_source` | `TEXT, v3` | backend or none |
| `cache_write_tokens` | `INTEGER, v3` | tokens a provider charged as a cache write |
| `cost_micros` | `INTEGER, v3` | cost in micro-dollars at the model's price |
| `proxy_ms` | `REAL, v3` | the proxy's own time |
| `retries` | `INTEGER, v3` | tries again before the first byte |
| `fallback_from` | `TEXT, v3` | the backend tried first |
| `upstream_status` | `INTEGER, v3` | the backend's HTTP status |
| `user` | `TEXT, v3` | the label of the proxy key |
| `preemptions` | `INTEGER, v4` | preemptions (vLLM) or retractions (SGLang), from the plugin |
| `engine_finish` | `TEXT, v4` | the backend engine's own finish reason |
| `corrupted` | `INTEGER, v4` | vLLM's NaN check |
| `l2_join` | `TEXT, v4` | exact or approximate join of the plugin's numbers |
| `wait_own_ms` | `REAL, v5` | ms the request waited behind the engine's own work (the shadow reader, a summary written ahead, the conductor's question) |
| `parent_request` | `TEXT, v6` | the turn an internal row served (the router's question names the turn it routes) |
| `slot` | `INTEGER, v7` | the engine's sequence slot with --parallel N (0 the main slot, 1 and up a side slot); null without |
| `engine_cached` | `INTEGER, v8` | the prompt tokens the engine read from a cache, the hidden notes included (cached\_tokens counts the client's among them) |
| `forwarded_tokens` | `INTEGER, v8` | what the prefill forwarded, hidden tokens included |
| `wait_mem_ms` | `REAL, v8` | ms the parallel memory admission held the request (inside queue\_ms) |
| `sampling` | `TEXT, v9` | how the request sampled: null greedy, client (its own temperature), regenerate (TandemLLM sampled a regenerate) |
| `system_fingerprint` | `TEXT, v9` |   |
| `node` | `TEXT, v9` |   |
| `deployment` | `TEXT, v9` |   |
| `upstream_request` | `TEXT, v9` |   |
| `token_id` | `TEXT, v9` |   |
| `api` | `TEXT, v9` |   |

The conductor's journal shares the file: `conductor_events` has 53 columns (`id`, `ts_ms`, `request_id`, `session_key`, `kind`, `task`, `tier`, `mode`, `sp`, `rule`, `trigger`, `score`, `decision`, `action`, `cap`, `tier1_model`, `question_id`, `probabilities`, `tier1_ms`, `hidden_text_id`, `hidden_tokens`, `tokens_at_cut`, `position`, `outcome`, `next_checker`, `policy_version`, `client_kind`, `model`, `profile`, `profile_version`, `router_version`, `class_id`, `confidence`, `source`, `kept`, `stages`, `classifier_ms`, `effort`, `budget`, `budget_source`, `thinking`, `decisions`, `injected`, `replayed`, `shadow`, `digest`, `label`, `label_note`, `labelled_at`, `acted`, `changed`, `detail`, `extra`), and `conductor_texts` keeps every hidden text once, by its hash.


<!-- /generated -->
