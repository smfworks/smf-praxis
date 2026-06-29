
from hybridagent import config as cfg
from hybridagent import onboard
from hybridagent.llm import LLMClient


def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_noninteractive_onboard_writes_config_shape(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    summary = onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")
    assert summary["model"] == "openrouter/openai/gpt-4o-mini"

    data = cfg.load_config()
    assert data["agents"]["defaults"]["model"] == "openrouter/openai/gpt-4o-mini"
    prov = data["providers"]["openrouter"]
    assert prov["baseUrl"].startswith("https://openrouter.ai")
    assert prov["compatibility"] == "openai"
    assert prov["keyRef"] == {"source": "env", "id": "OPENROUTER_API_KEY"}


def test_model_ref_split():
    assert cfg.split_model_ref("openrouter/openai/gpt-4o-mini") == (
        "openrouter", "openai/gpt-4o-mini")
    assert cfg.split_model_ref("ollama/llama3.1") == ("ollama", "llama3.1")


def test_paste_key_stored_in_auth_profile(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openai", "gpt-4o-mini",
                               api_key="sk-test-123", use_env_ref=False)
    # Key is not in the main config…
    assert "sk-test-123" not in cfg.config_path().read_text()
    # …but is resolvable from the auth profile.
    assert cfg.resolve_api_key("openai") == "sk-test-123"


def test_env_ref_resolves_from_environment(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-xyz")
    assert cfg.resolve_api_key("openrouter") == "sk-env-xyz"


def test_auto_mode_uses_mock_when_unconfigured(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.delenv("PRAXIS_LLM", raising=False)
    llm = LLMClient()
    assert llm._effective_mode() == "mock"
    out = llm.complete("hello world")
    assert out.startswith("[")          # deterministic mock output


def test_auto_mode_switches_to_real_when_configured(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.delenv("PRAXIS_LLM", raising=False)
    onboard.run_noninteractive("ollama", "llama3.1")
    llm = LLMClient()
    assert llm._effective_mode() == "real"
