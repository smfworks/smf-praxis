"""Tests for the Ollama cloud + quick model configuration changes."""
import json
from unittest import mock


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_ollama_cloud_provider_in_catalog():
    from hybridagent.providers import CATALOG, ORDER
    assert "ollama-cloud" in CATALOG
    assert "ollama-cloud" in ORDER
    assert CATALOG["ollama-cloud"].needs_key is True
    assert CATALOG["ollama-cloud"].key_env == "OLLAMA_API_TOKEN"


def test_discover_ollama_prefers_v1_models():
    from hybridagent.providers import discover_ollama_models
    response = {"data": [{"id": "llama3.3"}, {"id": "qwen2.5"}]}

    def _fake_open(req, **kw):
        return _FakeResponse(json.dumps(response).encode())

    with mock.patch("urllib.request.urlopen", side_effect=_fake_open):
        models = discover_ollama_models("http://127.0.0.1:11434/v1")
    assert models == ["llama3.3", "qwen2.5"]


def test_discover_ollama_sends_auth_header_for_cloud():
    from hybridagent.providers import discover_ollama_models
    response = {"data": [{"id": "llama3.3"}]}
    captured = {}

    def _fake_open(req, **kw):
        captured["headers"] = dict(req.header_items())
        captured["url"] = req.full_url
        return _FakeResponse(json.dumps(response).encode())

    with mock.patch("urllib.request.urlopen", side_effect=_fake_open):
        models = discover_ollama_models(
            "https://ollama.com/v1", api_key="secret-token")
    assert models == ["llama3.3"]
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["url"] == "https://ollama.com/v1/models"


def test_discover_ollama_falls_back_to_api_tags():
    from hybridagent.providers import discover_ollama_models
    response = {"models": [{"name": "mistral:latest"}]}
    call_count = {"n": 0}

    def _url(req):
        return req.full_url if hasattr(req, "full_url") else str(req)

    def _fake_open(req, **kw):
        call_count["n"] += 1
        if _url(req).endswith("/models"):
            raise RuntimeError("models endpoint unavailable")
        return _FakeResponse(json.dumps(response).encode())

    with mock.patch("urllib.request.urlopen", side_effect=_fake_open):
        models = discover_ollama_models("http://127.0.0.1:11434/v1")
    assert models == ["mistral:latest"]
    assert call_count["n"] == 2


def test_discover_ollama_returns_empty_when_both_endpoints_fail():
    from hybridagent.providers import discover_ollama_models

    def _fake_open(req, **kw):
        raise RuntimeError("unreachable")

    with mock.patch("urllib.request.urlopen", side_effect=_fake_open):
        models = discover_ollama_models("http://127.0.0.1:11434/v1")
    assert models == []


def test_onboard_ollama_cloud_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path))
    from hybridagent import config as cfg
    from hybridagent import onboard
    from hybridagent.providers import CATALOG

    with mock.patch("hybridagent.bootstrap.run"):
        summary = onboard.run_noninteractive(
            "ollama-cloud", "llama3.3", api_key="cloud-token",
            use_env_ref=False)
    assert summary["model"] == "ollama-cloud/llama3.3"
    assert cfg.get_default_model() == "ollama-cloud/llama3.3"
    entry = cfg.provider_entry("ollama-cloud")
    assert entry is not None
    assert entry["baseUrl"] == CATALOG["ollama-cloud"].base_url


def test_cli_model_get_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path))
    from hybridagent import cli

    assert cli.main(["model", "get"]) == 0
    assert capsys.readouterr().out.strip() == "not configured"

    assert cli.main(["model", "set", "ollama/qwen2.5"]) == 0
    out = capsys.readouterr().out
    assert "Set model: ollama/qwen2.5" in out

    assert cli.main(["model", "get"]) == 0
    assert capsys.readouterr().out.strip() == "ollama/qwen2.5"

    # Providers that require a key must reject empty/missing keys.
    assert cli.main(["model", "set", "openai/gpt-4o-mini"]) == 1
    assert "requires an API key" in capsys.readouterr().out


def test_cli_model_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path))
    from hybridagent import cli

    assert cli.main(["model", "set", "openai/gpt-4o-mini",
                     "--api-key", "sk-test"]) == 0
    capsys.readouterr()
    assert cli.main(["model", "list"]) == 0
    out = capsys.readouterr().out
    assert "* openai:" in out
    assert "ollama:" in out
    assert "ollama-cloud:" in out


def test_cli_onboard_lists_models_when_no_model_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path))
    from hybridagent import cli

    with mock.patch("hybridagent.cli.discover_ollama_models",
                    return_value=["llama3.3", "qwen2.5"]):
        assert cli.main(["onboard", "--provider", "ollama"]) == 0
    out = capsys.readouterr().out
    assert "Models available for ollama:" in out
    assert "llama3.3" in out


def test_cli_onboard_noninteractive_cloud(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path))
    from hybridagent import cli
    from hybridagent import config as cfg

    assert cli.main([
        "onboard", "--provider", "ollama-cloud", "--model", "llama3.3",
        "--api-key", "my-token",
    ]) == 0
    out = capsys.readouterr().out
    assert "ollama-cloud/llama3.3" in out
    entry = cfg.provider_entry("ollama-cloud")
    assert entry is not None
    assert entry["keyRef"]["source"] == "auth-profile"
