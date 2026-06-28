"""Per-model inference pricing (USD per 1K tokens).

Turns a provider's reported token usage into real spend for the governed budget.
Local models (Ollama / localhost / the offline mock) are free. An unrecognised
*cloud* model falls back to a conservative default so spend is never silently
under-counted — for a governance product, over-counting an unknown model is the
safer error than billing it $0.

Prices are approximate published list prices (prompt, completion) and are easy to
update; the budget cap only needs them to be the right order of magnitude.
"""
from __future__ import annotations

from . import config as cfg
from .router import ModelRouter

# Substring-matched against the model id (longest match wins), so a dated id like
# "gpt-4o-mini-2024-07-18" still resolves to "gpt-4o-mini".
PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
    "o4-mini": (0.0011, 0.0044),
    "o1-mini": (0.0011, 0.0044),
    "o1": (0.015, 0.06),
    # Anthropic
    "claude-3-5-haiku": (0.0008, 0.004),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-7-sonnet": (0.003, 0.015),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-3-opus": (0.015, 0.075),
    # Google
    "gemini-2.0-flash": (0.0001, 0.0004),
    "gemini-1.5-flash": (0.000075, 0.0003),
    "gemini-1.5-pro": (0.00125, 0.005),
    # Meta / open (typical hosted price)
    "llama-3.1-8b": (0.00005, 0.00008),
    "llama-3.1-70b": (0.00059, 0.00079),
    "llama-3.3-70b": (0.00059, 0.00079),
    # Mistral
    "mistral-large": (0.002, 0.006),
    "mistral-small": (0.0002, 0.0006),
    "mixtral-8x7b": (0.00024, 0.00024),
    # DeepSeek
    "deepseek-chat": (0.00027, 0.0011),
    "deepseek-reasoner": (0.00055, 0.00219),
    # xAI / Perplexity
    "grok-2": (0.002, 0.01),
    "sonar": (0.001, 0.001),
}

# Conservative fallback for an unrecognised *cloud* model (per 1K tokens).
_DEFAULT_RATE = (0.001, 0.003)


def _rate(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    best: tuple[float, float] | None = None
    best_len = -1
    for key, rate in PRICING.items():
        if key in m and len(key) > best_len:
            best, best_len = rate, len(key)
    return best if best is not None else _DEFAULT_RATE


def price_usd(model_ref: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost for one call.

    Local and mock models are free. Unrecognised cloud models use a conservative
    default so the governed budget never under-counts real spend.
    """
    if ModelRouter.is_local_ref(model_ref):
        return 0.0
    _, model = cfg.split_model_ref(model_ref)
    p_rate, c_rate = _rate(model)
    cost = (max(0, prompt_tokens) / 1000.0) * p_rate \
        + (max(0, completion_tokens) / 1000.0) * c_rate
    return round(cost, 6)
