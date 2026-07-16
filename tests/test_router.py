from hybridagent import config as cfg
from hybridagent import onboard
from hybridagent.llm import LLMClient
from hybridagent.router import (
    HARD,
    NORMAL,
    SENSITIVE,
    SIMPLE,
    STANDARD,
    ModelRouter,
    classify_difficulty,
    classify_sensitivity,
)


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_classify_sensitivity():
    assert classify_sensitivity("just a normal status update") == NORMAL
    assert classify_sensitivity("password: hunter2") == SENSITIVE
    assert classify_sensitivity("SSN 123-45-6789 on file") == SENSITIVE
    assert classify_sensitivity("marked HIGHLY CONFIDENTIAL") == SENSITIVE
    assert classify_sensitivity("") == NORMAL


def test_is_local_ref(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("ollama", "llama3.1")
    r = ModelRouter()
    assert r.is_local_ref("ollama/llama3.1") is True
    assert r.is_local_ref("openrouter/openai/gpt-4o-mini") is False
    assert r.is_local_ref("mock") is True


def test_select_default_role(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")
    r = ModelRouter()
    assert r.select("general", NORMAL) == ["openrouter/openai/gpt-4o-mini"]


def test_sensitive_never_routes_to_cloud(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")   # cloud default
    r = ModelRouter()
    assert r.select("general", SENSITIVE) == ["mock"]               # not the cloud model


def test_sensitive_keeps_local_model(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("ollama", "llama3.1")               # local default
    r = ModelRouter()
    assert r.select("general", SENSITIVE) == ["ollama/llama3.1"]


def test_role_override_takes_priority(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    data = cfg.load_config()
    data["agents"]["roles"] = {"planner": "openrouter/anthropic/claude-3.5-sonnet"}
    cfg.save_config(data)
    r = ModelRouter()
    assert r.select("planner", NORMAL) == [
        "openrouter/anthropic/claude-3.5-sonnet", "openai/gpt-4o-mini"]


def test_sensitive_content_uses_mock_in_real_mode(tmp_path, monkeypatch):
    # Real mode + cloud default, but sensitive content must stay offline (mock).
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")
    llm = LLMClient(mode="real")
    out = llm.complete("here is a password: hunter2", sensitivity=SENSITIVE)
    assert out.startswith("[")                # deterministic mock, no network


def test_fallback_walks_candidate_list(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    data = cfg.load_config()
    data["agents"]["roles"] = {"general": "openrouter/broken-model"}
    cfg.save_config(data)
    llm = LLMClient(mode="real")

    def fake_call(ref, prompt, system, max_tokens=None):
        if "broken" in ref:
            raise RuntimeError("primary down")
        return f"ok via {ref}"

    monkeypatch.setattr(llm, "_complete_with_ref", fake_call)
    out = llm.complete("hello")
    assert out == "ok via openai/gpt-4o-mini"   # fell back to the default


_TIERS = {"fast": "ollama/llama3.1", "balanced": "openai/gpt-4o-mini",
          "strong": "openai/gpt-4o"}


def test_classify_difficulty_buckets():
    assert classify_difficulty("hi there") == SIMPLE
    assert classify_difficulty("thanks!") == SIMPLE
    assert classify_difficulty("Analyze the trade-offs of this architecture") == HARD
    assert classify_difficulty("```python\nprint(1)\n```") == HARD
    assert classify_difficulty("x" * 700) == HARD
    assert classify_difficulty("what is the current project status report") == STANDARD


def test_select_prefers_tier_by_difficulty(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    data = cfg.load_config()
    data["agents"]["tiers"] = dict(_TIERS)
    cfg.save_config(data)
    r = ModelRouter()
    assert r.select("general", NORMAL, HARD)[0] == "openai/gpt-4o"
    assert r.select("general", NORMAL, SIMPLE)[0] == "ollama/llama3.1"
    # No difficulty -> behaviour unchanged (default leads).
    assert r.select("general", NORMAL)[0] == "openai/gpt-4o-mini"


def test_sensitivity_pins_local_over_tier(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    data = cfg.load_config()
    data["agents"]["tiers"] = dict(_TIERS)
    cfg.save_config(data)
    r = ModelRouter()
    # A 'hard' turn whose strong tier is a cloud model must still not leak.
    sens = r.select("general", SENSITIVE, HARD)
    assert sens and all(ModelRouter.is_local_ref(x) for x in sens)


def test_no_tiers_is_backward_compatible(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini")
    r = ModelRouter()
    assert r.select("general", NORMAL, HARD) == ["openai/gpt-4o-mini"]
