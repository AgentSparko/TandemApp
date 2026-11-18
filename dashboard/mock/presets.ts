// The providers Add a backend offers, as proxy/presets.py answers them (GET /v1/config/presets).
// Generated from it; tests/test_presets.py checks that the two lists match.
import type { Preset } from '../src/api/config';

export const PRESETS: Preset[] = [
  {
    "id": "openai",
    "label": "OpenAI",
    "group": "cloud",
    "key": "required",
    "key_url": "https://platform.openai.com/api-keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "openai",
        "url": "https://api.openai.com/v1"
      }
    ]
  },
  {
    "id": "anthropic",
    "label": "Anthropic",
    "group": "cloud",
    "key": "required",
    "key_url": "https://console.anthropic.com/settings/keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": "https://api.anthropic.com"
      }
    ]
  },
  {
    "id": "openrouter",
    "label": "OpenRouter",
    "group": "cloud",
    "key": "required",
    "key_url": "https://openrouter.ai/keys",
    "cloud": true,
    "note": "hundreds of models behind one key",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://openrouter.ai/api/v1"
      }
    ]
  },
  {
    "id": "deepseek",
    "label": "DeepSeek",
    "group": "cloud",
    "key": "required",
    "key_url": "https://platform.deepseek.com/api_keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "deepseek",
        "url": "https://api.deepseek.com/v1"
      },
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": "https://api.deepseek.com/anthropic"
      }
    ]
  },
  {
    "id": "zai",
    "label": "Z.ai (GLM)",
    "group": "cloud",
    "key": "required",
    "key_url": "https://z.ai/manage-apikey/apikey-list",
    "cloud": true,
    "note": "a Coding Plan key takes https://api.z.ai/api/coding/paas/v4",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.z.ai/api/paas/v4"
      },
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": "https://api.z.ai/api/anthropic"
      }
    ]
  },
  {
    "id": "moonshot",
    "label": "Moonshot (Kimi)",
    "group": "cloud",
    "key": "required",
    "key_url": "https://platform.moonshot.ai/console/api-keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.moonshot.ai/v1"
      },
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": "https://api.moonshot.ai/anthropic"
      }
    ]
  },
  {
    "id": "minimax",
    "label": "MiniMax",
    "group": "cloud",
    "key": "required",
    "key_url": "https://platform.minimax.io/user-center/basic-information/interface-key",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.minimax.io/v1"
      },
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": "https://api.minimax.io/anthropic"
      }
    ]
  },
  {
    "id": "nous",
    "label": "Nous Research (Hermes)",
    "group": "cloud",
    "key": "required",
    "key_url": "https://portal.nousresearch.com",
    "cloud": true,
    "note": "the Hermes models of the Nous Portal",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://inference-api.nousresearch.com/v1"
      }
    ]
  },
  {
    "id": "gemini",
    "label": "Google Gemini",
    "group": "cloud",
    "key": "required",
    "key_url": "https://aistudio.google.com/apikey",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai"
      }
    ]
  },
  {
    "id": "xai",
    "label": "xAI (Grok)",
    "group": "cloud",
    "key": "required",
    "key_url": "https://console.x.ai",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.x.ai/v1"
      }
    ]
  },
  {
    "id": "mistral",
    "label": "Mistral",
    "group": "cloud",
    "key": "required",
    "key_url": "https://console.mistral.ai/api-keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.mistral.ai/v1"
      }
    ]
  },
  {
    "id": "groq",
    "label": "Groq",
    "group": "cloud",
    "key": "required",
    "key_url": "https://console.groq.com/keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.groq.com/openai/v1"
      }
    ]
  },
  {
    "id": "together",
    "label": "Together AI",
    "group": "cloud",
    "key": "required",
    "key_url": "https://api.together.ai/settings/api-keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.together.xyz/v1"
      }
    ]
  },
  {
    "id": "fireworks",
    "label": "Fireworks AI",
    "group": "cloud",
    "key": "required",
    "key_url": "https://fireworks.ai/account/api-keys",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.fireworks.ai/inference/v1"
      }
    ]
  },
  {
    "id": "cerebras",
    "label": "Cerebras",
    "group": "cloud",
    "key": "required",
    "key_url": "https://cloud.cerebras.ai",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://api.cerebras.ai/v1"
      }
    ]
  },
  {
    "id": "qwen",
    "label": "Qwen (Alibaba Cloud)",
    "group": "cloud",
    "key": "required",
    "key_url": "https://modelstudio.console.alibabacloud.com",
    "cloud": true,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
      }
    ]
  },
  {
    "id": "huggingface",
    "label": "Hugging Face",
    "group": "cloud",
    "key": "required",
    "key_url": "https://huggingface.co/settings/tokens",
    "cloud": true,
    "note": "the Inference Providers router",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "https://router.huggingface.co/v1"
      }
    ]
  },
  {
    "id": "tandemengine",
    "label": "TandemEngine",
    "group": "self-hosted",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "our engine on another box, or this one",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "tandemllm",
        "url": "http://127.0.0.1:8000/v1"
      }
    ]
  },
  {
    "id": "vllm",
    "label": "vLLM",
    "group": "self-hosted",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "its /metrics read for KV, prefix-cache hits, the queue and speculation",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "vllm",
        "url": "http://127.0.0.1:8000/v1"
      }
    ]
  },
  {
    "id": "sglang",
    "label": "SGLang",
    "group": "self-hosted",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "started with --enable-metrics for its KV and cache figures",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "sglang",
        "url": "http://127.0.0.1:30000/v1"
      }
    ]
  },
  {
    "id": "ollama",
    "label": "Ollama",
    "group": "self-hosted",
    "key": "none",
    "key_url": null,
    "cloud": false,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "http://127.0.0.1:11434/v1"
      }
    ]
  },
  {
    "id": "lmstudio",
    "label": "LM Studio",
    "group": "self-hosted",
    "key": "none",
    "key_url": null,
    "cloud": false,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "http://127.0.0.1:1234/v1"
      }
    ]
  },
  {
    "id": "llamacpp",
    "label": "llama.cpp server",
    "group": "self-hosted",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": "http://127.0.0.1:8080/v1"
      }
    ]
  },
  {
    "id": "custom-openai",
    "label": "Custom (OpenAI-compatible)",
    "group": "custom",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "any server that speaks /v1/chat/completions",
    "endpoints": [
      {
        "protocol": "openai",
        "kind": "generic",
        "url": ""
      }
    ]
  },
  {
    "id": "custom-anthropic",
    "label": "Custom (Anthropic-compatible)",
    "group": "custom",
    "key": "optional",
    "key_url": null,
    "cloud": false,
    "note": "any server that speaks /v1/messages",
    "endpoints": [
      {
        "protocol": "anthropic",
        "kind": "anthropic",
        "url": ""
      }
    ]
  }
];
