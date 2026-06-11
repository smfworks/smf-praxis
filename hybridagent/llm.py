"""LLM client.

Modes (env ``PRAXIS_LLM``):
    auto (default)  - use the configured provider if onboarding has been run,
                      otherwise fall back to the offline mock
    mock            - always use the deterministic offline mock
    real            - always use the configured provider (errors if unconfigured)

Run ``praxis onboard`` to pick a provider (Ollama / OpenRouter / GitHub / OpenAI
/ Anthropic / custom) and model; config is stored OpenClaw-style under
``~/.praxis/``.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from . import config as cfg
from .providers import CATALOG, chat


@dataclass
class LLMClient:
    model: str = "praxis-local"
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_LLM", "auto"))

    def _effective_mode(self) -> str:
        if self.mode in ("mock", "real"):
            return self.mode
        return "real" if cfg.is_configured() else "mock"  # auto

    def complete(self, prompt: str, system: str | None = None) -> str:
        if self._effective_mode() == "real":
            return self._complete_real(prompt, system)
        seed = hashlib.sha256(((system or "") + prompt).encode()).hexdigest()[:8]
        head = prompt.strip().splitlines()[0][:100] if prompt.strip() else "(empty)"
        return f"[{seed}] {head}"

    def summarize(self, text: str) -> str:
        if self._effective_mode() == "real":
            return self._complete_real("Summarize concisely:\n" + text, system=None)
        seed = hashlib.sha256(text.encode()).hexdigest()[:6]
        first = text.strip().splitlines()[0][:120] if text.strip() else ""
        return f"summary[{seed}]: {first}"

    def _complete_real(self, prompt: str, system: str | None) -> str:
        model_ref = cfg.get_default_model()
        if not model_ref:
            raise RuntimeError(
                "No provider configured. Run 'praxis onboard' to pick a "
                "provider and model (or set PRAXIS_LLM=mock for offline use)."
            )
        provider_id, model = cfg.split_model_ref(model_ref)
        provider = CATALOG.get(provider_id)
        entry = cfg.provider_entry(provider_id) or {}
        if not provider:
            raise RuntimeError(f"Unknown provider '{provider_id}' in config.")
        api_key = cfg.resolve_api_key(provider_id)
        if provider.needs_key and not api_key:
            raise RuntimeError(
                f"Missing API key for '{provider_id}'. Set {provider.key_env} or "
                f"re-run 'praxis onboard' and paste the key."
            )
        return chat(
            provider=provider, model=model, prompt=prompt, system=system,
            api_key=api_key, base_url=entry.get("baseUrl"),
        )
