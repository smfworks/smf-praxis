import pytest

from hybridagent import config as cfg
from hybridagent import providers
from hybridagent.llm import LLMClient
from hybridagent.m365_tools import build_m365_agent


class CountingBroker:
    def __init__(self):
        self.calls = []

    def execute(self, tool, args=None, approval_id=None):
        self.calls.append(tool)
        res = {"ok": True, "outcome": "success", "result": {}}
        if tool == "create_email_draft":
            res["result"] = {"draftId": "d1"}
        return res

    def approve(self, tool, args=None):
        return {"ok": True, "approvalId": "a1"}


def test_read_tool_executed_once_per_cycle():
    # Regression: perception + plan both reference reads; they must not double-call.
    client = CountingBroker()
    agent, _ = build_m365_agent(client)
    agent.handle("Review recent mail and gather context")
    assert client.calls.count("search_mail") == 1
    assert client.calls.count("list_today_events") == 1


def test_malformed_model_ref_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.save_config({
        "agents": {"defaults": {"model": "ollama"}},   # missing /model
        "providers": {"ollama": {"baseUrl": "http://x/v1", "compatibility": "openai"}},
    })
    llm = LLMClient(mode="real")
    with pytest.raises(RuntimeError) as e:
        llm.complete("hi")
    assert "Malformed model ref" in str(e.value)


def test_chat_raises_on_malformed_openai_response(monkeypatch):
    monkeypatch.setattr(providers, "_post", lambda *a, **k: {"choices": []})
    prov = providers.CATALOG["openrouter"]
    with pytest.raises(RuntimeError):
        providers.chat(prov, "m", "hi", None, "key")


def test_chat_raises_on_malformed_anthropic_response(monkeypatch):
    monkeypatch.setattr(providers, "_post", lambda *a, **k: {"error": "boom"})
    prov = providers.CATALOG["anthropic"]
    with pytest.raises(RuntimeError):
        providers.chat(prov, "claude-3-5-sonnet-latest", "hi", None, "key")
