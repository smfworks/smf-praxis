from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.chat_agent import GovernedChatAgent
from hybridagent.reflexion import ReflexionConfig, ReflexiveChatAgent
from hybridagent.tools import Tool, ToolRegistry


def _schema(*required):
    return {"type": "object",
            "properties": {k: {"type": "string"} for k in required},
            "required": list(required)}


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


_ECHO = Tool("echo", RiskClass.DRAFT, "Echo", lambda message="", **k: f"echo:{message}",
             parameters=_schema("message"))
_SEND = Tool("send_email", RiskClass.SEND, "Send",
             lambda draft_id="", **k: f"SENT {draft_id}", parameters=_schema("draft_id"))


def _types(events):
    return [e.type for e in events]


def _final(events):
    return next((e for e in reversed(events) if e.type == "final"), None)


def _run(llm, registry, broker, *, max_steps=2, max_reflections=1):
    inner = GovernedChatAgent(llm, registry, broker, max_steps=max_steps)
    rx = ReflexiveChatAgent(inner, max_reflections=max_reflections)
    return list(rx.run([{"role": "user", "content": "do the thing"}]))


class _StuckThenRecovers:
    """Unknown tool until a reflection appears in the system prompt, then answers."""

    def __init__(self):
        self.calls = 0

    def chat_tools(self, messages, tools=None, system=None):
        self.calls += 1
        if system and "Self-reflection" in system:
            return {"text": "Recovered: a direct answer.", "tool_calls": []}
        return {"text": "", "tool_calls": [{"id": "c1", "name": "ghost", "args": {}}]}


def test_recovers_from_deadend():
    llm = _StuckThenRecovers()
    events = _run(llm, _registry(_ECHO), GovernanceBroker(GovernancePolicy()))
    types = _types(events)
    assert types.count("reflection") == 1
    assert _final(events).data["text"].startswith("Recovered")
    assert llm.calls == 3  # 2 stuck steps (attempt 1) + 1 recovery (attempt 2)


def test_no_retry_on_clean_success():
    class _Clean:
        def __init__(self):
            self.calls = 0

        def chat_tools(self, messages, tools=None, system=None):
            self.calls += 1
            return {"text": "All done.", "tool_calls": []}

    llm = _Clean()
    events = _run(llm, _registry(_ECHO), GovernanceBroker(GovernancePolicy()))
    assert "reflection" not in _types(events)
    assert _final(events).data["text"] == "All done."
    assert llm.calls == 1


def test_no_retry_when_action_held():
    class _Sends:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "", "tool_calls": [
                {"id": "c1", "name": "send_email", "args": {"draft_id": "d1"}}]}

    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    events = _run(_Sends(), _registry(_SEND), broker)
    types = _types(events)
    # A held consequential action is the correct outcome, never a "failure".
    assert "approval" in types and "reflection" not in types
    assert _final(events).data.get("held") is True


def test_no_retry_after_side_effect():
    class _DraftThenStuck:
        def __init__(self):
            self.calls = 0

        def chat_tools(self, messages, tools=None, system=None):
            self.calls += 1
            if self.calls == 1:
                return {"text": "", "tool_calls": [
                    {"id": "c1", "name": "echo", "args": {"message": "hi"}}]}
            return {"text": "", "tool_calls": [{"id": "c2", "name": "ghost", "args": {}}]}

    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"echo"}))
    events = _run(_DraftThenStuck(), _registry(_ECHO), broker)
    types = _types(events)
    # The draft executed (a real side effect), so the failed turn is accepted
    # as-is rather than retried — a retry could duplicate the action.
    assert "tool_result" in types and "reflection" not in types


def test_bounded_reflection_budget():
    class _AlwaysStuck:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "", "tool_calls": [{"id": "c1", "name": "ghost", "args": {}}]}

    one = _run(_AlwaysStuck(), _registry(_ECHO),
               GovernanceBroker(GovernancePolicy()), max_reflections=1)
    assert _types(one).count("reflection") == 1
    assert "tool-step limit" in _final(one).data["text"]

    two = _run(_AlwaysStuck(), _registry(_ECHO),
               GovernanceBroker(GovernancePolicy()), max_reflections=2)
    assert _types(two).count("reflection") == 2


def test_zero_budget_never_retries():
    class _AlwaysStuck:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "", "tool_calls": [{"id": "c1", "name": "ghost", "args": {}}]}

    events = _run(_AlwaysStuck(), _registry(_ECHO),
                  GovernanceBroker(GovernancePolicy()), max_reflections=0)
    assert "reflection" not in _types(events)


def test_config_defaults_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_REFLECT", raising=False)
    default = ReflexionConfig.load()
    assert default.enabled is True and default.max_reflections == 1

    monkeypatch.setenv("PRAXIS_REFLECT", "0")
    assert ReflexionConfig.load().enabled is False
    monkeypatch.setenv("PRAXIS_REFLECT", "1")
    assert ReflexionConfig.load().enabled is True
