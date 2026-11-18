# Glossary

TandemApp's terms. The engine's (anchor, round, StairCut and the rest) are in [TandemEngine's glossary](https://github.com/TandemLLM/TandemEngine/blob/main/docs/glossary.md).

- **adapter** The proxy's code for one backend kind: how it relays a route, where it reads cached and reasoning tokens, how it switches thinking. `proxy/adapters/`.
- **backend** An engine or a cloud API behind the proxy: another TandemEngine, vLLM, SGLang, any OpenAI-compatible or Messages API server, OpenAI, Anthropic, DeepSeek. Each has a kind, a URL, models, an optional key, and switches ([backends.md](backends.md)).
- **feature group** One switchable part of the harness in front of a backend: class, effort, packages, side, loops, e1, tools, compaction, states, memory.
- **host collector** The code that reads a machine's memory, CPU, GPUs, disks and the process behind each backend's port; `tandem-app hostcollect` on a remote machine.
- **L1, L2** The two layers of engine figures behind the proxy: L1 is the engine's `/metrics` page, L2 the plugin's per-request numbers.
- **preset** A provider the Add backend form offers (OpenAI, OpenRouter, Z.ai, Ollama and the rest): its base URL for each protocol, its kind and how it takes a key. `proxy/presets.py`.
- **protocol** What a backend or a client speaks: `openai` (chat completions, completions, embeddings, Responses) or `anthropic` (the Messages API).
- **plugin** A small package inside vLLM or SGLang that posts each request's own times to the proxy.
- **proxy** The router of requests by model id to backends, with one ledger and one dashboard: `tandem-app`, the controller (until engine API 1.8 also the engine's server with `--backends`).

