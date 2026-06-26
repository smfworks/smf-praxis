"""Provider catalog + OpenAI/Anthropic/Ollama-compatible calls (stdlib only).

Mirrors OpenClaw's provider model: a default model is stored as a
``provider/model-id`` reference and each provider has a base URL, a wire
"compatibility" (openai|anthropic), and an API-key environment variable.

No third-party dependencies — HTTP is done with urllib so the package stays
dependency-free.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .logging_util import get_logger

_log = get_logger("praxis.providers")

# HTTP statuses worth a retry (rate-limit + transient upstream failures).
_RETRYABLE = {429, 500, 502, 503, 504}


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
    "google": Provider(
        id="google", label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        compatibility="openai", key_env="GEMINI_API_KEY", needs_key=True,
        suggested_models=["gemini-2.0-flash", "gemini-2.0-flash-lite",
                          "gemini-1.5-pro", "gemini-1.5-flash"],
        notes="Google AI Studio key (GEMINI_API_KEY); OpenAI-compatible endpoint.",
    ),
    "mistral": Provider(
        id="mistral", label="Mistral AI",
        base_url="https://api.mistral.ai/v1", compatibility="openai",
        key_env="MISTRAL_API_KEY", needs_key=True,
        suggested_models=["mistral-large-latest", "mistral-small-latest",
                          "open-mistral-nemo", "codestral-latest"],
    ),
    "groq": Provider(
        id="groq", label="Groq (fast inference)",
        base_url="https://api.groq.com/openai/v1", compatibility="openai",
        key_env="GROQ_API_KEY", needs_key=True,
        suggested_models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                          "mixtral-8x7b-32768", "gemma2-9b-it"],
        notes="Groq Cloud key (GROQ_API_KEY); very low-latency inference.",
    ),
    "deepseek": Provider(
        id="deepseek", label="DeepSeek",
        base_url="https://api.deepseek.com/v1", compatibility="openai",
        key_env="DEEPSEEK_API_KEY", needs_key=True,
        suggested_models=["deepseek-chat", "deepseek-reasoner"],
        notes="DeepSeek Platform key (DEEPSEEK_API_KEY).",
    ),
    "perplexity": Provider(
        id="perplexity", label="Perplexity (Sonar)",
        base_url="https://api.perplexity.ai", compatibility="openai",
        key_env="PERPLEXITY_API_KEY", needs_key=True,
        suggested_models=["sonar", "sonar-pro", "sonar-reasoning"],
        notes="Perplexity key (PERPLEXITY_API_KEY); web-grounded Sonar models.",
    ),
    "together": Provider(
        id="together", label="Together AI",
        base_url="https://api.together.xyz/v1", compatibility="openai",
        key_env="TOGETHER_API_KEY", needs_key=True,
        suggested_models=["meta-llama/Llama-3.3-70B-Instruct-Turbo",
                          "Qwen/Qwen2.5-72B-Instruct-Turbo",
                          "mistralai/Mixtral-8x7B-Instruct-v0.1"],
    ),
    "fireworks": Provider(
        id="fireworks", label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1", compatibility="openai",
        key_env="FIREWORKS_API_KEY", needs_key=True,
        suggested_models=["accounts/fireworks/models/llama-v3p3-70b-instruct",
                          "accounts/fireworks/models/qwen2p5-72b-instruct",
                          "accounts/fireworks/models/deepseek-v3"],
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

ORDER = ["ollama", "openai", "anthropic", "google", "xai", "mistral", "groq",
         "deepseek", "perplexity", "together", "fireworks", "openrouter",
         "github", "vercel-ai-gateway", "custom"]


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
         timeout: float = 60.0, temperature: float = 0.0,
         max_tokens: int = 1024) -> str:
    """Send a single completion via the provider's wire protocol."""
    url_root = (base_url or provider.base_url).rstrip("/")
    if provider.compatibility == "anthropic":
        return _chat_anthropic(url_root, model, prompt, system, api_key,
                               timeout, temperature, max_tokens)
    return _chat_openai(url_root, model, prompt, system, api_key,
                        timeout, temperature, max_tokens)


def _post(url: str, headers: dict, payload: dict, timeout: float,
          retries: int = 2, backoff: float = 0.5) -> dict:
    """POST JSON with bounded exponential-backoff retry on transient errors."""
    attempt = 0
    while True:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in _RETRYABLE and attempt < retries:
                wait = backoff * (2 ** attempt)
                _log.warning("provider HTTP %s (attempt %d); retrying in %.1fs",
                             e.code, attempt + 1, wait)
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"provider HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                _log.warning("provider unreachable (%s, attempt %d); retrying in %.1fs",
                             e.reason, attempt + 1, wait)
                time.sleep(wait)
                attempt += 1
                continue
            raise RuntimeError(f"provider unreachable: {e.reason}") from e


def _chat_openai(root: str, model: str, prompt: str, system: str | None,
                 api_key: str | None, timeout: float,
                 temperature: float = 0.0, max_tokens: int = 1024) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    data = _post(f"{root}/chat/completions", headers,
                 {"model": model, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens}, timeout)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}") from exc


def _chat_anthropic(root: str, model: str, prompt: str, system: str | None,
                    api_key: str | None, timeout: float,
                    temperature: float = 0.0, max_tokens: int = 1024) -> str:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key
    payload = {"model": model, "max_tokens": max_tokens,
               "temperature": temperature,
               "messages": [{"role": "user", "content": prompt}]}
    if system:
        payload["system"] = system
    data = _post(f"{root}/messages", headers, payload, timeout)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}")
    return "".join(block.get("text", "") for block in blocks)


def chat_messages(provider: Provider, model: str, messages: list[dict],
                  system: str | None = None, api_key: str | None = None,
                  base_url: str | None = None, timeout: float = 60.0,
                  temperature: float = 0.3, max_tokens: int = 1024) -> str:
    """Multi-turn chat completion.

    ``messages`` is an ordered list of ``{"role": "user"|"assistant"|"system",
    "content": str}`` turns; ``system`` is an optional leading system prompt
    merged ahead of the conversation. Routes to the provider's wire protocol.
    """
    root = (base_url or provider.base_url).rstrip("/")
    if provider.compatibility == "anthropic":
        return _chat_messages_anthropic(root, model, messages, system, api_key,
                                        timeout, temperature, max_tokens)
    return _chat_messages_openai(root, model, messages, system, api_key,
                                 timeout, temperature, max_tokens)


def _normalize_turns(messages: list[dict]) -> list[dict]:
    """Coerce arbitrary message dicts into clean {role, content} turns."""
    out: list[dict] = []
    for m in messages or []:
        role = str(m.get("role", "user"))
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": str(m.get("content", ""))})
    return out


def _chat_messages_openai(root: str, model: str, messages: list[dict],
                          system: str | None, api_key: str | None, timeout: float,
                          temperature: float, max_tokens: int) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    wire: list[dict] = []
    if system:
        wire.append({"role": "system", "content": system})
    wire.extend(_normalize_turns(messages))
    data = _post(f"{root}/chat/completions", headers,
                 {"model": model, "messages": wire,
                  "temperature": temperature, "max_tokens": max_tokens}, timeout)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}") from exc


def _chat_messages_anthropic(root: str, model: str, messages: list[dict],
                             system: str | None, api_key: str | None,
                             timeout: float, temperature: float,
                             max_tokens: int) -> str:
    headers = {"Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    # Anthropic takes the system prompt as a top-level field and only
    # user/assistant turns in ``messages`` — hoist any system turns out.
    sys_parts = [system] if system else []
    conv: list[dict] = []
    for turn in _normalize_turns(messages):
        if turn["role"] == "system":
            sys_parts.append(turn["content"])
            continue
        conv.append(turn)
    payload: dict = {"model": model, "max_tokens": max_tokens,
                     "temperature": temperature,
                     "messages": conv or [{"role": "user", "content": ""}]}
    merged_system = "\n\n".join(p for p in sys_parts if p)
    if merged_system:
        payload["system"] = merged_system
    data = _post(f"{root}/messages", headers, payload, timeout)
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}")
    return "".join(block.get("text", "") for block in blocks)


def embed(provider: Provider, model: str, texts: list[str],
          api_key: str | None, base_url: str | None = None,
          timeout: float = 60.0) -> list[list[float]]:
    """Embed texts via an OpenAI-compatible /embeddings endpoint.

    Works with OpenAI, Ollama (>=0.1.x exposes /v1/embeddings), OpenRouter, and
    any compatible host. Anthropic has no embeddings API, so callers should route
    embeddings to a different provider.
    """
    root = (base_url or provider.base_url).rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = _post(f"{root}/embeddings", headers,
                 {"model": model, "input": texts}, timeout)
    try:
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [it["embedding"] for it in items]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected embeddings response: {str(data)[:300]}") from exc


def chat_multimodal(provider: Provider, model: str, prompt: str,
                    images: list[dict], system: str | None, api_key: str | None,
                    base_url: str | None = None, timeout: float = 90.0,
                    temperature: float = 0.0, max_tokens: int = 1024) -> str:
    """Vision chat. ``images`` items are ``{"media_type": str, "data": base64}``."""
    root = (base_url or provider.base_url).rstrip("/")
    if provider.compatibility == "anthropic":
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": img["media_type"],
                "data": img["data"]}})
        headers = {"Content-Type": "application/json",
                   "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        payload = {"model": model, "max_tokens": max_tokens,
                   "temperature": temperature,
                   "messages": [{"role": "user", "content": content}]}
        if system:
            payload["system"] = system
        data = _post(f"{root}/messages", headers, payload, timeout)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError(f"unexpected provider response: {str(data)[:300]}")
        return "".join(b.get("text", "") for b in blocks)

    content = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{img['media_type']};base64,{img['data']}"}})
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": content}]
    data = _post(f"{root}/chat/completions", headers,
                 {"model": model, "messages": messages,
                  "temperature": temperature, "max_tokens": max_tokens}, timeout)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected provider response: {str(data)[:300]}") from exc


def transcribe(provider: Provider, model: str, audio_path: str,
               api_key: str | None, base_url: str | None = None,
               timeout: float = 120.0) -> str:
    """Speech-to-text via an OpenAI-compatible /audio/transcriptions endpoint."""
    import mimetypes
    import uuid
    root = (base_url or provider.base_url).rstrip("/")
    boundary = "----praxis" + uuid.uuid4().hex
    fname = os.path.basename(audio_path)
    with open(audio_path, "rb") as fh:
        audio = fh.read()
    mt = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\""
            f"\r\n\r\n{model}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{fname}\"\r\nContent-Type: {mt}\r\n\r\n").encode()
    body += audio + b"\r\n" + f"--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{root}/audio/transcriptions", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"transcribe HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"transcribe unreachable: {e.reason}") from e
    return data.get("text", "")
