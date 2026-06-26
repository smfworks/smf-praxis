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
from .logging_util import get_logger
from .providers import CATALOG, chat, chat_messages
from .router import NORMAL, ModelRouter, classify_sensitivity

_log = get_logger("praxis.llm")


@dataclass
class LLMClient:
    model: str = "praxis-local"
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_LLM", "auto"))
    router: ModelRouter = field(default_factory=ModelRouter)

    def _effective_mode(self) -> str:
        if self.mode in ("mock", "real"):
            return self.mode
        return "real" if cfg.is_configured() else "mock"  # auto

    # ------------------------------------------------------------------ public
    def complete(self, prompt: str, system: str | None = None,
                 role: str = "general", sensitivity: str = NORMAL) -> str:
        if self._effective_mode() == "real":
            return self._route(prompt, system, role, sensitivity)
        return self._mock_complete(prompt, system)

    def summarize(self, text: str, role: str = "summarizer",
                  sensitivity: str | None = None) -> str:
        if sensitivity is None:
            sensitivity = classify_sensitivity(text)
        if self._effective_mode() == "real":
            return self._route("Summarize concisely:\n" + text, None,
                               role, sensitivity)
        return self._mock_summarize(text)

    def chat(self, messages: list[dict], system: str | None = None,
             role: str = "general", sensitivity: str | None = None) -> str:
        """Multi-turn conversational completion.

        ``messages`` is an ordered list of ``{"role", "content"}`` turns. The
        combined conversation is sensitivity-classified so secrets are never
        routed to a cloud provider (they fall back to a local/offline model).
        """
        convo = "\n".join(str(m.get("content", "")) for m in messages)
        if sensitivity is None:
            sensitivity = classify_sensitivity((system or "") + "\n" + convo)
        if self._effective_mode() == "real":
            return self._route_messages(messages, system, role, sensitivity)
        return self._mock_chat(messages, system)

    # -------------------------------------------------------------------- mock
    def _mock_complete(self, prompt: str, system: str | None) -> str:
        seed = hashlib.sha256(((system or "") + prompt).encode()).hexdigest()[:8]
        head = prompt.strip().splitlines()[0][:100] if prompt.strip() else "(empty)"
        return f"[{seed}] {head}"

    def _mock_summarize(self, text: str) -> str:
        seed = hashlib.sha256(text.encode()).hexdigest()[:6]
        first = text.strip().splitlines()[0][:120] if text.strip() else ""
        return f"summary[{seed}]: {first}"

    def _mock_chat(self, messages: list[dict], system: str | None) -> str:
        last_user = ""
        for m in reversed(messages or []):
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break
        seed = hashlib.sha256(((system or "") + last_user).encode()).hexdigest()[:8]
        head = last_user.strip().splitlines()[0][:160] if last_user.strip() else "(empty)"
        return (f"[mock:{seed}] You said: {head}\n\n"
                "(Offline mock reply — run `praxis onboard` and pick a cloud "
                "model for real chat.)")

    # ------------------------------------------------------------------- route
    def _route(self, prompt: str, system: str | None,
               role: str, sensitivity: str) -> str:
        candidates = self.router.select(role, sensitivity)
        if not candidates:
            raise RuntimeError(
                "No provider configured. Run 'praxis onboard' to pick a "
                "provider and model (or set PRAXIS_LLM=mock for offline use)."
            )
        last_exc: Exception | None = None
        for ref in candidates:
            if ref == "mock":          # router's offline-safe choice (e.g. sensitive)
                return self._mock_complete(prompt, system)
            try:
                return self._complete_with_ref(ref, prompt, system)
            except RuntimeError as exc:
                last_exc = exc
                _log.warning("model %s failed (%s); trying next candidate", ref, exc)
                continue
        raise last_exc if last_exc else RuntimeError("no usable model")

    def _complete_with_ref(self, model_ref: str, prompt: str,
                           system: str | None) -> str:
        provider_id, model = cfg.split_model_ref(model_ref)
        if not model:
            raise RuntimeError(
                f"Malformed model ref {model_ref!r}; expected 'provider/model-id'. "
                f"Re-run 'praxis onboard'."
            )
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

    # ----------------------------------------------------------- chat (multi-turn)
    def _route_messages(self, messages: list[dict], system: str | None,
                        role: str, sensitivity: str) -> str:
        candidates = self.router.select(role, sensitivity)
        if not candidates:
            raise RuntimeError(
                "No provider configured. Run 'praxis onboard' to pick a "
                "provider and model (or set PRAXIS_LLM=mock for offline use)."
            )
        last_exc: Exception | None = None
        for ref in candidates:
            if ref == "mock":
                return self._mock_chat(messages, system)
            try:
                return self._chat_with_ref(ref, messages, system)
            except RuntimeError as exc:
                last_exc = exc
                _log.warning("model %s failed (%s); trying next candidate", ref, exc)
                continue
        raise last_exc if last_exc else RuntimeError("no usable model")

    def _chat_with_ref(self, model_ref: str, messages: list[dict],
                       system: str | None) -> str:
        provider_id, model = cfg.split_model_ref(model_ref)
        if not model:
            raise RuntimeError(
                f"Malformed model ref {model_ref!r}; expected 'provider/model-id'. "
                f"Re-run 'praxis onboard'."
            )
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
        return chat_messages(
            provider=provider, model=model, messages=messages, system=system,
            api_key=api_key, base_url=entry.get("baseUrl"),
        )

    # Backwards-compatible seam (documented in FRAMEWORK.md).
    def _complete_real(self, prompt: str, system: str | None) -> str:
        return self._route(prompt, system, "general", NORMAL)
