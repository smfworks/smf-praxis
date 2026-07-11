"""Tests for model-specific harness profiles (H08)."""
from __future__ import annotations

from hybridagent.context_profile import ContextProfile, profile_for, profile_names


def test_sonnet_profile_compacts_sooner():
    """Sonnet-class: smaller budget, fewer turns, reset on compact."""
    p = profile_for("claude-sonnet-4.5")
    assert p.context_char_budget < 24000  # smaller than default
    assert p.keep_recent_turns < 8
    assert p.reset_on_compact is True


def test_opus_profile_keeps_more():
    """Opus-class: larger budget, more turns, no reset."""
    p = profile_for("claude-opus-4.6")
    assert p.context_char_budget > 24000  # larger than default
    assert p.keep_recent_turns > 8
    assert p.reset_on_compact is False


def test_gpt5_profile_large_window():
    """GPT-5-class: largest budget, compaction as safety net."""
    p = profile_for("gpt-5.5")
    assert p.context_char_budget >= 40000
    assert p.reset_on_compact is False


def test_qwen_profile_conservative():
    """Qwen (local, small window): compact aggressively."""
    p = profile_for("qwen2.5-7b-instruct")
    assert p.context_char_budget <= 16000
    assert p.reset_on_compact is True


def test_unknown_model_gets_default():
    """An unknown model gets the backward-compatible default profile."""
    p = profile_for("some-unknown-model-xyz")
    assert p.context_char_budget == 24000
    assert p.keep_recent_turns == 8
    assert p.reset_on_compact is False
    assert p.rationale == "default"


def test_empty_model_gets_default():
    p = profile_for("")
    assert p.context_char_budget == 24000


def test_case_insensitive_match():
    """Matching is case-insensitive."""
    assert profile_for("CLAUDE-SONNET-4.5").context_char_budget == \
        profile_for("claude-sonnet-4.5").context_char_budget


def test_profile_names_returns_sorted():
    names = profile_names()
    assert "sonnet" in names
    assert "opus" in names
    assert names == sorted(names)


def test_profile_is_dataclass():
    """ContextProfile is a dataclass with the four documented fields."""
    p = ContextProfile()
    assert hasattr(p, "context_char_budget")
    assert hasattr(p, "keep_recent_turns")
    assert hasattr(p, "reset_on_compact")
    assert hasattr(p, "rationale")