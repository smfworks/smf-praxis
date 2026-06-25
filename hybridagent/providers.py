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
