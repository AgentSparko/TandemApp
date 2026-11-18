"""The providers the Backends page offers when a backend is added: pick one, paste a key, done.

    GET /v1/config/presets      {"presets": [...]} (also in GET /v1/config as `presets`)

A backend speaks one of two protocols: `openai` (chat completions, completions, embeddings, the
Responses API where the server has it) or `anthropic` (the Messages API). A preset is a provider
with the base URL of each protocol it serves, the adapter kind that relays it, whether it is a paid
cloud API (`cloud`: the longer cloud timeouts, the harness off for its paid models) and how it
takes a key (`required`, `optional`, `none`). The two `custom-*` presets are any server of that
protocol, by its URL. A preset only fills the form: the backend stores its kind, URL, key and the
preset's id (`preset`, for its name on the page); a provider that moves its URL breaks nothing
already saved. The URLs are each provider's documented base URL (2026-11-18).
"""

from __future__ import annotations

OPENAI, ANTHROPIC = "openai", "anthropic"


def _p(pid, label, group, endpoints, *, key="required", key_url=None, cloud=None, note=""):
    return {"id": pid, "label": label, "group": group, "key": key, "key_url": key_url,
            "cloud": group == "cloud" if cloud is None else cloud, "note": note,
            "endpoints": [{"protocol": pr, "kind": k, "url": u} for pr, k, u in endpoints]}


PRESETS: tuple[dict, ...] = (
    # cloud APIs: a key and go
    _p("openai", "OpenAI", "cloud", [(OPENAI, "openai", "https://api.openai.com/v1")],
       key_url="https://platform.openai.com/api-keys"),
    _p("anthropic", "Anthropic", "cloud", [(ANTHROPIC, "anthropic", "https://api.anthropic.com")],
       key_url="https://console.anthropic.com/settings/keys"),
    _p("openrouter", "OpenRouter", "cloud", [(OPENAI, "generic", "https://openrouter.ai/api/v1")],
       key_url="https://openrouter.ai/keys", note="hundreds of models behind one key"),
    _p("deepseek", "DeepSeek", "cloud", [(OPENAI, "deepseek", "https://api.deepseek.com/v1"),
                                         (ANTHROPIC, "anthropic", "https://api.deepseek.com/anthropic")],
       key_url="https://platform.deepseek.com/api_keys"),
    _p("zai", "Z.ai (GLM)", "cloud", [(OPENAI, "generic", "https://api.z.ai/api/paas/v4"),
                                      (ANTHROPIC, "anthropic", "https://api.z.ai/api/anthropic")],
       key_url="https://z.ai/manage-apikey/apikey-list",
       note="a Coding Plan key takes https://api.z.ai/api/coding/paas/v4"),
    _p("moonshot", "Moonshot (Kimi)", "cloud", [(OPENAI, "generic", "https://api.moonshot.ai/v1"),
                                                (ANTHROPIC, "anthropic", "https://api.moonshot.ai/anthropic")],
       key_url="https://platform.moonshot.ai/console/api-keys"),
    _p("minimax", "MiniMax", "cloud", [(OPENAI, "generic", "https://api.minimax.io/v1"),
                                       (ANTHROPIC, "anthropic", "https://api.minimax.io/anthropic")],
       key_url="https://platform.minimax.io/user-center/basic-information/interface-key"),
    _p("nous", "Nous Research (Hermes)", "cloud",
       [(OPENAI, "generic", "https://inference-api.nousresearch.com/v1")],
       key_url="https://portal.nousresearch.com", note="the Hermes models of the Nous Portal"),
    _p("gemini", "Google Gemini", "cloud",
       [(OPENAI, "generic", "https://generativelanguage.googleapis.com/v1beta/openai")],
       key_url="https://aistudio.google.com/apikey"),
    _p("xai", "xAI (Grok)", "cloud", [(OPENAI, "generic", "https://api.x.ai/v1")],
       key_url="https://console.x.ai"),
    _p("mistral", "Mistral", "cloud", [(OPENAI, "generic", "https://api.mistral.ai/v1")],
       key_url="https://console.mistral.ai/api-keys"),
    _p("groq", "Groq", "cloud", [(OPENAI, "generic", "https://api.groq.com/openai/v1")],
       key_url="https://console.groq.com/keys"),
    _p("together", "Together AI", "cloud", [(OPENAI, "generic", "https://api.together.xyz/v1")],
       key_url="https://api.together.ai/settings/api-keys"),
    _p("fireworks", "Fireworks AI", "cloud", [(OPENAI, "generic", "https://api.fireworks.ai/inference/v1")],
       key_url="https://fireworks.ai/account/api-keys"),
    _p("cerebras", "Cerebras", "cloud", [(OPENAI, "generic", "https://api.cerebras.ai/v1")],
       key_url="https://cloud.cerebras.ai"),
    _p("qwen", "Qwen (Alibaba Cloud)", "cloud",
       [(OPENAI, "generic", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")],
       key_url="https://modelstudio.console.alibabacloud.com"),
    _p("huggingface", "Hugging Face", "cloud", [(OPENAI, "generic", "https://router.huggingface.co/v1")],
       key_url="https://huggingface.co/settings/tokens", note="the Inference Providers router"),
    # servers you run: a URL, usually no key
    _p("tandemengine", "TandemEngine", "self-hosted", [(OPENAI, "tandemllm", "http://127.0.0.1:8000/v1")],
       key="optional", note="our engine on another box, or this one"),
    _p("vllm", "vLLM", "self-hosted", [(OPENAI, "vllm", "http://127.0.0.1:8000/v1")], key="optional",
       note="its /metrics read for KV, prefix-cache hits, the queue and speculation"),
    _p("sglang", "SGLang", "self-hosted", [(OPENAI, "sglang", "http://127.0.0.1:30000/v1")], key="optional",
       note="started with --enable-metrics for its KV and cache figures"),
    _p("ollama", "Ollama", "self-hosted", [(OPENAI, "generic", "http://127.0.0.1:11434/v1")], key="none"),
    _p("lmstudio", "LM Studio", "self-hosted", [(OPENAI, "generic", "http://127.0.0.1:1234/v1")], key="none"),
    _p("llamacpp", "llama.cpp server", "self-hosted", [(OPENAI, "generic", "http://127.0.0.1:8080/v1")],
       key="optional"),
    # anything else, by its URL
    _p("custom-openai", "Custom (OpenAI-compatible)", "custom", [(OPENAI, "generic", "")],
       key="optional", cloud=False, note="any server that speaks /v1/chat/completions"),
    _p("custom-anthropic", "Custom (Anthropic-compatible)", "custom", [(ANTHROPIC, "anthropic", "")],
       key="optional", cloud=False, note="any server that speaks /v1/messages"),
)

IDS = frozenset(p["id"] for p in PRESETS)


def presets() -> list[dict]:
    return [dict(p, endpoints=[dict(e) for e in p["endpoints"]]) for p in PRESETS]


def by_id(pid: str | None) -> dict | None:
    return next((p for p in PRESETS if p["id"] == pid), None)
