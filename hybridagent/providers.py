"""Provider catalog + OpenAI/Anthropic/Ollama-compatible calls (stdlib only).

Mirrors OpenClaw's provider model: a default model is stored as a
``provider/model-id`` reference and each provider has a base URL, a wire
"compatibility" (openai|anthropic), and an API-key environment variable.

No third-party dependencies — HTTP is done with urllib so the package stays
dependency-free.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class Provider:
    id: str
    label: str
    base_url: str
    compatibility: str          # "openai" | "anthropic"
    key_env: str | None         # env var holding the API key (None = no key, e.g. local Ollama)
    needs_key: bool
    suggested_models: list[str] = field(default_factory=list)
    notes: str = ""


# Provider IDs mirror OpenClaw conventions; OpenRouter + GitHub Models added
# because Michael asked for them (both OpenAI-compatible).
CATALOG: dict[str, Provider] = {
    "ollama": Provider(
        id="ollama", label="Ollama (local open models)",
        base_url="http://127.0.0.1:11434/v1", compatibility="openai",
        key_env="OLLAMA_API_KEY", needs_key=False,
        suggested_models=["llama3.1", "qwen2.5", "mistral", "phi3.5"],
        notes="Local by default; no key required. Models are auto-discovered.",
    ),
    "openrouter": Provider(
        id="openrouter", label="OpenRouter",
        base_url="https://openrouter.ai/api/v1", compatibility="openai",
        key_env="OPENROUTER_API_KEY", needs_key=True,
        suggested_models=[
            "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.1-70b-instruct", "google/gemini-flash-1.5",
        ],
    ),
    "github": Provider(
        id="github", label="GitHub Models",
        base_url="https://models.github.ai/inference", compatibility="openai",
        key_env="GITHUB_TOKEN", needs_key=True,
        suggested_models=["gpt-4o-mini", "gpt-4o", "o4-mini",
                          "Llama-3.3-70B-Instruct", "Phi-3.5-MoE-instruct"],
        notes="Uses your GitHub PAT (GITHUB_TOKEN) with models: read.",
    ),
    "openai": Provider(
        id="openai", label="OpenAI",
        base_url="https://api.openai.com/v1", compatibility="openai",
        key_env="OPENAI_API_KEY", needs_key=True,
        suggested_models=["gpt-4o-mini", "gpt-4o", "o4-mini"],
    ),
    "anthropic": Provider(
        id="anthropic", label="Anthropic",
        base_url="https://api.anthropic.com/v1", compatibility="anthropic",
        key_env="ANTHROPIC_API_KEY", needs_key=True,
        suggested_models=["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
    ),
    "xai": Provider(
        id="xai", label="xAI (Grok)",
        base_url="https://api.x.ai/v1", compatibility="openai",
        key_env="XAI_API_KEY", needs_key=True,
        suggested_models=["grok-2-latest", "grok-2-mini", "grok-beta"],
        notes="xAI Console API key (XAI_API_KEY); OpenAI-compatible endpoint.",
    ),
    "vercel-ai-gateway": Provider(
        id="vercel-ai-gateway", label="Vercel AI Gateway",
        base_url="https://ai-gateway.vercel.sh/v1", compatibility="openai",
        key_env="AI_GATEWAY_API_KEY", needs_key=True,
        suggested_models=[
            "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet",
            "xai/grok-2-latest", "meta/llama-3.1-70b-instruct",
        ],
        notes="Single key (AI_GATEWAY_API_KEY) fronts many providers; model ids are 'vendor/model'.",
    ),
    "custom": Provider(
        id="custom", label="Custom (OpenAI-compatible)",
        base_url="", compatibility="openai",
        key_env="CUSTOM_API_KEY", needs_key=True,
        notes="Any OpenAI-compatible endpoint; you supply the base URL + key env var.",
    ),
}

ORDER = ["ollama", "openrouter", "github", "openai", "anthropic",
         "xai", "vercel-ai-gateway", "custom"]


def discover_ollama_models(base_url: str, timeout: float = 3.0) -> list[str]:
    """Best-effort model discovery from a local Ollama host (/api/tags)."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def chat(provider: Provider, model: str, prompt: str, system: str | None,
         api_key: str | None, base_url: str | None = None,
         timeout: float = 60.0) -> str:
    """Send a single completion via the provider's wire protocol."""
    url_root = (base_url or provider.base_url).rstrip("/")
    if provider.compatibility == "anthropic":
        return _chat_anthropic(url_root, model, prompt, system, api_key, timeout)
    return _chat_openai(url_root, model, prompt, system, api_key, timeout)


def _post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"provider HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"provider unreachable: {e.reason}") from e


def _chat_openai(root: str, model: str, prompt: str, system: str | None,
                 api_key: str | None, timeout: float) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    data = _post(f"{root}/chat/completions", headers,
                 {"model": model, "messages": messages}, timeout)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}")


def _chat_anthropic(root: str, model: str, prompt: str, system: str | None,
                    api_key: str | None, timeout: float) -> str:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key
    payload = {"model": model, "max_tokens": 1024,
               "messages": [{"role": "user", "content": prompt}]}
    if system:
        payload["system"] = system
    data = _post(f"{root}/messages", headers, payload, timeout)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}")
    return "".join(block.get("text", "") for block in blocks)
