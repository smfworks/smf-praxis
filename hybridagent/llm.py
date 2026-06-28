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
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import config as cfg
from .logging_util import get_logger
from .pricing import price_usd
from .providers import CATALOG, chat, chat_messages, chat_messages_stream, chat_messages_tools
from .router import NORMAL, ModelRouter, classify_difficulty, classify_sensitivity

_log = get_logger("praxis.llm")


def _mock_fill_args(schema: dict) -> dict:
    """Best-effort args for the offline mock's tool calls: fill required params
    with type-appropriate placeholders so schema validation passes."""
    props = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or list(props)
    defaults = {"string": "mock", "integer": 0, "number": 0,
                "boolean": False, "array": [], "object": {}}
    out: dict = {}
    for key in required:
        spec = props.get(key) or {}
        ptype = spec.get("type")
        out[key] = defaults.get(ptype, "mock") if isinstance(ptype, str) else "mock"
    return out


def _difficulty_of(payload: object) -> str:
    """Classify request difficulty from a prompt string or a messages list
    (user turns only) so routing can prefer a stronger or faster model."""
    if isinstance(payload, list):
        text = "\n".join(str(m.get("content", "")) for m in payload
                         if isinstance(m, dict) and m.get("role") == "user")
    else:
        text = str(payload or "")
    return classify_difficulty(text)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class _RunUsage:
    """Token + cost tally accumulated across one governed run's LLM calls."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    model: str = ""
    models: list[str] = field(default_factory=list)
    fallbacks: int = 0

    def as_dict(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "calls": self.calls, "model": self.model,
                "models": list(self.models), "fallbacks": self.fallbacks}


@dataclass
class LLMClient:
    model: str = "praxis-local"
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_LLM", "auto"))
    router: ModelRouter = field(default_factory=ModelRouter)
    context_char_budget: int = field(
        default_factory=lambda: _env_int("PRAXIS_CTX_BUDGET", 24000))
    keep_recent_turns: int = 8
    _usage: _RunUsage = field(default_factory=_RunUsage, init=False, repr=False)

    def _effective_mode(self) -> str:
        if self.mode in ("mock", "real"):
            return self.mode
        return "real" if cfg.is_configured() else "mock"  # auto

    # ------------------------------------------------------------- accounting
    def reset_usage(self) -> None:
        """Zero the per-run token/cost tally (call before a governed run)."""
        self._usage = _RunUsage()

    def usage_snapshot(self) -> dict:
        """Tokens + USD cost tallied since the last :meth:`reset_usage`."""
        return self._usage.as_dict()

    def _account(self, model_ref: str, usage: dict) -> None:
        """Fold one real provider call's token usage + cost into the tally."""
        p = int(usage.get("prompt_tokens", 0) or 0)
        c = int(usage.get("completion_tokens", 0) or 0)
        self._usage.prompt_tokens += p
        self._usage.completion_tokens += c
        self._usage.cost_usd += price_usd(model_ref, p, c)
        self._usage.calls += 1
        self._usage.model = model_ref
        if model_ref not in self._usage.models:
            self._usage.models.append(model_ref)

    # ------------------------------------------------------------------ public
    def complete(self, prompt: str, system: str | None = None,
                 role: str = "general", sensitivity: str = NORMAL,
                 difficulty: str | None = None) -> str:
        if self._effective_mode() == "real":
            return self._route(prompt, system, role, sensitivity, difficulty)
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
        messages = self._maybe_compact(messages)
        convo = "\n".join(str(m.get("content", "")) for m in messages)
        if sensitivity is None:
            sensitivity = classify_sensitivity((system or "") + "\n" + convo)
        if self._effective_mode() == "real":
            return self._route_messages(messages, system, role, sensitivity)
        return self._mock_chat(messages, system)

    def chat_stream(self, messages: list[dict], system: str | None = None,
                    role: str = "general",
                    sensitivity: str | None = None) -> Iterator[str]:
        """Streaming variant of :meth:`chat` — yields assistant text deltas.

        Same sensitivity routing as :meth:`chat`: secrets never reach a cloud
        provider. Offline (mock) mode still "streams" by chunking the mock reply
        so the UI behaves identically with or without a provider configured.
        """
        messages = self._maybe_compact(messages)
        convo = "\n".join(str(m.get("content", "")) for m in messages)
        if sensitivity is None:
            sensitivity = classify_sensitivity((system or "") + "\n" + convo)
        if self._effective_mode() == "real":
            yield from self._stream_messages(messages, system, role, sensitivity)
        else:
            yield from self._mock_chat_stream(messages, system)

    def chat_tools(self, messages: list[dict], tools: list[dict],
                   system: str | None = None, role: str = "general",
                   sensitivity: str | None = None) -> dict:
        """One tool-calling turn for the governed chat loop.

        Returns ``{"text": str, "tool_calls": [{"id","name","args"}]}``. Same
        sensitivity routing as :meth:`chat`. Offline (mock) mode emits a
        deterministic tool call when the latest user turn names an available
        tool, so the governed loop is exercisable without a provider.
        """
        convo = "\n".join(str(m.get("content", "")) for m in messages)
        if sensitivity is None:
            sensitivity = classify_sensitivity((system or "") + "\n" + convo)
        if self._effective_mode() == "real":
            return self._tools_messages(messages, tools, system, role, sensitivity)
        return self._mock_chat_tools(messages, tools, system)

    def _maybe_compact(self, messages: list[dict]) -> list[dict]:
        """Compact an over-budget conversation: keep recent turns, summarize the
        rest. Applied to the chat surfaces only (not the tool loop, where
        tool-call / tool-result pairing must stay intact)."""
        from .context import compact_messages
        return compact_messages(
            messages, max_chars=self.context_char_budget,
            keep_recent=self.keep_recent_turns, summarize=self.summarize)

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

    def _mock_chat_stream(self, messages: list[dict],
                          system: str | None) -> Iterator[str]:
        """Chunk the deterministic mock reply into word-sized pieces so the
        offline experience streams just like a real provider."""
        text = self._mock_chat(messages, system)
        token = ""
        for ch in text:
            token += ch
            if ch.isspace():
                yield token
                token = ""
        if token:
            yield token

    def _mock_chat_tools(self, messages: list[dict], tools: list[dict],
                         system: str | None) -> dict:
        last_user = ""
        for m in reversed(messages or []):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                last_user = m["content"]
                break
        called = {m.get("name") for m in (messages or []) if m.get("role") == "tool"}
        for spec in tools or []:
            name = spec.get("name", "")
            if name and name in last_user and name not in called:
                return {"text": "", "tool_calls": [{
                    "id": f"call_{name}", "name": name,
                    "args": _mock_fill_args(spec.get("parameters") or {})}]}
        return {"text": self._mock_chat(messages, system), "tool_calls": []}

    # ------------------------------------------------------------------- route
    def _route(self, prompt: str, system: str | None,
               role: str, sensitivity: str, difficulty: str | None = None) -> str:
        candidates = self.router.select(role, sensitivity,
                                        difficulty or _difficulty_of(prompt))
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
                self._usage.fallbacks += 1
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
        usage: dict = {}
        text = chat(
            provider=provider, model=model, prompt=prompt, system=system,
            api_key=api_key, base_url=entry.get("baseUrl"), usage_sink=usage)
        self._account(model_ref, usage)
        return text

    # ----------------------------------------------------------- chat (multi-turn)
    def _route_messages(self, messages: list[dict], system: str | None,
                        role: str, sensitivity: str) -> str:
        candidates = self.router.select(role, sensitivity, _difficulty_of(messages))
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
                self._usage.fallbacks += 1
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
        usage: dict = {}
        text = chat_messages(
            provider=provider, model=model, messages=messages, system=system,
            api_key=api_key, base_url=entry.get("baseUrl"), usage_sink=usage)
        self._account(model_ref, usage)
        return text

    # ------------------------------------------------------- chat (streaming)
    def _stream_messages(self, messages: list[dict], system: str | None,
                         role: str, sensitivity: str) -> Iterator[str]:
        candidates = self.router.select(role, sensitivity, _difficulty_of(messages))
        if not candidates:
            raise RuntimeError(
                "No provider configured. Run 'praxis onboard' to pick a "
                "provider and model (or set PRAXIS_LLM=mock for offline use)."
            )
        last_exc: Exception | None = None
        for ref in candidates:
            if ref == "mock":
                yield from self._mock_chat_stream(messages, system)
                return
            started = False
            try:
                for piece in self._chat_stream_with_ref(ref, messages, system):
                    started = True
                    yield piece
                return
            except RuntimeError as exc:
                if started:
                    # Tokens already emitted downstream — switching models now
                    # would splice two different answers together. Surface it.
                    raise
                last_exc = exc
                _log.warning("model %s stream failed (%s); trying next candidate",
                             ref, exc)
                continue
        raise last_exc if last_exc else RuntimeError("no usable model")

    def _chat_stream_with_ref(self, model_ref: str, messages: list[dict],
                              system: str | None) -> Iterator[str]:
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
        yield from chat_messages_stream(
            provider=provider, model=model, messages=messages, system=system,
            api_key=api_key, base_url=entry.get("baseUrl"),
        )

    # ------------------------------------------------------- chat (tool-calling)
    def _tools_messages(self, messages: list[dict], tools: list[dict],
                        system: str | None, role: str, sensitivity: str) -> dict:
        candidates = self.router.select(role, sensitivity, _difficulty_of(messages))
        if not candidates:
            raise RuntimeError(
                "No provider configured. Run 'praxis onboard' to pick a "
                "provider and model (or set PRAXIS_LLM=mock for offline use)."
            )
        last_exc: Exception | None = None
        for ref in candidates:
            if ref == "mock":
                return self._mock_chat_tools(messages, tools, system)
            try:
                return self._chat_tools_with_ref(ref, messages, tools, system)
            except RuntimeError as exc:
                last_exc = exc
                _log.warning("model %s tool-call failed (%s); trying next candidate",
                             ref, exc)
                continue
        raise last_exc if last_exc else RuntimeError("no usable model")

    def _chat_tools_with_ref(self, model_ref: str, messages: list[dict],
                             tools: list[dict], system: str | None) -> dict:
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
        result = chat_messages_tools(
            provider=provider, model=model, messages=messages, tools=tools,
            system=system, api_key=api_key, base_url=entry.get("baseUrl"),
        )
        self._account(model_ref, result.get("usage") or {})
        return result

    # Backwards-compatible seam (documented in FRAMEWORK.md).
    def _complete_real(self, prompt: str, system: str | None) -> str:
        return self._route(prompt, system, "general", NORMAL)
