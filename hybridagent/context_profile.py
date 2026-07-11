"""Model-specific harness profiles (H08).

The harness course (L5) found that context anxiety is severe on some
models (Sonnet 4.5 — rushed finish, skipped verification; needs full
context resets) and mild on others (Opus 4.5 — compaction suffices). A
one-size-fits-all compaction policy is wrong: it over-compacts strong
models (wasting context they could use) and under-compacts anxious ones
(letting them rush). This module maps model families to compaction
profiles so the harness adapts to the model it's driving.

A profile controls three knobs:
  * ``context_char_budget`` -- the max chars before compaction triggers.
    Larger = trust the model with more context. Smaller = compact sooner.
  * ``keep_recent_turns`` -- how many recent turns to preserve verbatim.
    Fewer = more aggressive summarization (suits anxious models).
  * ``reset_on_compact`` -- whether to do a full context reset (drop
    summarized turns entirely) vs. summarize them in place. The course
    calls this the compaction-vs-reset tradeoff: resets give a clean
    mental state (no "I'm running out of time" anxiety) but depend on the
    handoff artifacts; compaction keeps continuity but the agent still
    knows context was large.

Profiles are matched by model name substring (case-insensitive), so
``claude-sonnet-4.5`` matches the ``sonnet`` profile. An unknown model
gets the default profile (the existing 24000-char budget, 8 recent turns,
no reset — the current behavior, so this is backward-compatible).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ContextProfile:
    """Compaction policy for one model family.

    Attributes:
        context_char_budget: max chars before compaction triggers.
        keep_recent_turns: recent turns kept verbatim (rest summarized).
        reset_on_compact: if True, drop summarized turns entirely (full
            reset) instead of summarizing them in place.
        rationale: human-readable note for why this profile is tuned this
            way (for the operator log / docs).
    """
    context_char_budget: int = 24000
    keep_recent_turns: int = 8
    reset_on_compact: bool = False
    rationale: str = "default"


# ---------------------------------------------------------------------------
# Model-family profiles.
#
# The values are starting points, not gospel. The course's principle:
# "harness design needs specific understanding of the target model, not a
# one-size-fits-all template." Recalibrate by running the goal loop (H10)
# on a task suite per model and observing when context anxiety kicks in
# (rushed finish, skipped verification). The H05 verifier score over turns
# is the signal: a sudden drop near the context limit means the budget is
# too high for that model.
# ---------------------------------------------------------------------------

_PROFILES: dict[str, ContextProfile] = {
    # Sonnet-class: context anxiety is severe (course L5). Compact sooner
    # (smaller budget), keep fewer recent turns, and reset on compact to
    # give a clean mental state rather than letting the agent feel the
    # weight of a large summarized history.
    "sonnet": ContextProfile(
        context_char_budget=16000,
        keep_recent_turns=4,
        reset_on_compact=True,
        rationale="Sonnet-class: severe context anxiety (course L5). "
                  "Compact sooner, keep fewer turns, reset to clean state.",
    ),
    # Opus-class: context anxiety is mild. Trust the model with more
    # context (larger budget), keep more turns, summarize in place rather
    # than reset — the model handles a large summarized history well.
    "opus": ContextProfile(
        context_char_budget=32000,
        keep_recent_turns=10,
        reset_on_compact=False,
        rationale="Opus-class: mild context anxiety (course L5). "
                  "Larger budget, more turns, compaction over reset.",
    ),
    # GPT-5-class: frontier models with large windows. Trust with the most
    # context; compaction is a safety net, not the primary mechanism.
    "gpt-5": ContextProfile(
        context_char_budget=48000,
        keep_recent_turns=12,
        reset_on_compact=False,
        rationale="GPT-5-class: large window, compaction as safety net.",
    ),
    # Gemini 2.5 Flash (the H05 verifier default): long context window,
    # but used as the verifier not the generator — profile is for when it
    # IS the generator.
    "gemini": ContextProfile(
        context_char_budget=40000,
        keep_recent_turns=10,
        reset_on_compact=False,
        rationale="Gemini-class: long context window, compaction over reset.",
    ),
    # Qwen (local vLLM/llama.cpp): smaller windows, compact aggressively.
    # The 7B Q3_K_M verifier has a 4096-token context; be conservative.
    "qwen": ContextProfile(
        context_char_budget=12000,
        keep_recent_turns=4,
        reset_on_compact=True,
        rationale="Qwen-class (local): smaller windows, compact aggressively.",
    ),
}

# Keys sorted longest-first so "gpt-5" matches before "gpt" if both existed.
_MATCH_KEYS = sorted(_PROFILES, key=len, reverse=True)


def profile_for(model: str) -> ContextProfile:
    """Return the compaction profile for a model name.

    Matching is by substring (case-insensitive), longest-key-first, so
    ``claude-sonnet-4.5`` matches the ``sonnet`` profile. An unknown model
    gets the default profile (the existing 24000/8/no-reset behavior), so
    this is backward-compatible — calling code that never sets a profile
    behaves exactly as before.

    Args:
        model: the model reference string (e.g. ``"claude-sonnet-4.5"``,
            ``"gpt-5.5"``, ``"qwen2.5-7b-instruct"``).

    Returns:
        The matching :class:`ContextProfile`, or the default if no match.
    """
    if not model:
        return ContextProfile()
    name = model.lower()
    for key in _MATCH_KEYS:
        if key in name:
            return _PROFILES[key]
    return ContextProfile()


def profile_names() -> list[str]:
    """The registered profile family names (for docs / introspection)."""
    return sorted(_PROFILES)