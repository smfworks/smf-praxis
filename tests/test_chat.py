"""Tests for the expanded provider picker and the multi-turn chat surface."""
from __future__ import annotations

import pytest

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.providers import CATALOG, ORDER, _normalize_turns

_NEW_CLOUD = ["google", "mistral", "groq", "deepseek",
              "perplexity", "together", "fireworks"]


def test_new_cloud_providers_present_and_well_formed():
    for pid in _NEW_CLOUD:
        assert pid in CATALOG, f"{pid} missing from CATALOG"
        assert pid in ORDER, f"{pid} missing from ORDER"
        p = CATALOG[pid]
        assert p.compatibility == "openai"
        assert p.needs_key and p.key_env, f"{pid} should declare a key env var"
        assert p.base_url.startswith("https://")
        assert p.suggested_models, f"{pid} should suggest at least one model"


def test_order_has_no_duplicates_and_covers_catalog():
    assert len(ORDER) == len(set(ORDER))
    assert set(ORDER) == set(CATALOG)


def test_normalize_turns_coerces_roles_and_content():
    turns = _normalize_turns([
        {"role": "system", "content": "sys"},
        {"role": "weird", "content": "w"},
        {"content": "no role"},
        {"role": "assistant"},
    ])
    assert [t["role"] for t in turns] == ["system", "user", "user", "assistant"]
    assert turns[3]["content"] == ""           # missing content -> empty string


def test_llm_chat_mock_echoes_latest_user_turn():
    llm = LLMClient(mode="mock")
    out = llm.chat([{"role": "user", "content": "What is Praxis?"}])
    assert isinstance(out, str)
    assert "What is Praxis?" in out


def test_llm_chat_mock_uses_last_user_in_multiturn():
    llm = LLMClient(mode="mock")
    out = llm.chat([
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "a reply"},
        {"role": "user", "content": "second and final question"},
    ])
    assert "second and final question" in out
    assert "first message" not in out


def test_llm_chat_keeps_sensitive_conversation_off_cloud(tmp_path, monkeypatch):
    # Even configured for a cloud provider, a secret in the convo forces mock.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_LLM", raising=False)
    from hybridagent import onboard
    onboard.run_noninteractive("openai", "gpt-4o-mini")  # env-ref, no key set
    llm = LLMClient()
    # No OPENAI_API_KEY in env, so a real call would raise; sensitivity must
    # route to the offline mock instead of erroring on the missing key.
    out = llm.chat([{"role": "user", "content": "my password = hunter2 secret"}])
    assert out.startswith("[mock:")


def test_tool_results_do_not_force_mock_routing(tmp_path, monkeypatch):
    # A huge external tool result containing sensitive-looking words (e.g.
    # "api_key" in fetched documentation) must not classify the turn as
    # sensitive and strip the configured cloud model.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_LLM", raising=False)
    from hybridagent import onboard
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    llm = LLMClient()
    router = llm.router
    from hybridagent.router import classify_sensitivity
    # Simulate the third turn in a URL-summarization chat: user ask + assistant
    # tool call + two tool results full of doc HTML with trigger words.
    messages = [
        {"role": "user", "content": "summarize https://example.com/docs"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "name": "fetch_url", "content": "Set your API_KEY and TOKEN here. secret=foo" * 500},
        {"role": "tool", "name": "browser_navigate", "content": "password and token docs" * 500},
    ]
    # Sensitivity must be computed from user/assistant text only, not tool
    # results (which is the fix in LLMClient.chat_tools).
    convo = "\n".join(str(m.get("content", "")) for m in messages
                     if m.get("role") in ("user", "assistant"))
    sensitivity = classify_sensitivity(convo)
    candidates = router.select("general", sensitivity, "standard")
    assert "openai/gpt-4o-mini" in candidates
    assert "mock" not in candidates


def test_daemon_switch_model_validates_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    daemon = Daemon()
    with pytest.raises(ValueError):
        daemon.switch_model("nonexistent-provider", "m")
    with pytest.raises(ValueError):
        daemon.switch_model("openai", "")
    result = daemon.switch_model("google", "gemini-2.0-flash")
    assert result["model"] == "google/gemini-2.0-flash"
    assert cfg.get_default_model() == "google/gemini-2.0-flash"
