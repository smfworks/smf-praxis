"""Tests for the LLM-driven planner and its deterministic fallback."""
from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.planner import LLMPlanner
from hybridagent.tools import Tool, default_registry


class FakeLLM:
    """Minimal stand-in: forces 'real' mode and returns a fixed completion."""

    def __init__(self, out: str, mode: str = "real"):
        self.out = out
        self._mode = mode

    def _effective_mode(self):
        return self._mode

    def complete(self, prompt, system=None, role="general", sensitivity="normal", difficulty=None):
        return self.out


def test_llm_planner_uses_valid_steps():
    reg = default_registry()
    llm = FakeLLM('{"steps": ['
                  '{"intent": "check calendar", "tool": "list_today_events", "args": {}},'
                  '{"intent": "find mail", "tool": "search_mail", "args": {"query": "refund"}}]}')
    plan = LLMPlanner(reg, llm).plan("look up refund requests")
    tools = [s.tool for s in plan.steps]
    assert tools == ["list_today_events", "search_mail"]
    assert plan.steps[1].args == {"query": "refund"}


def test_llm_planner_drops_hallucinated_tools():
    reg = default_registry()
    llm = FakeLLM('{"steps": ['
                  '{"intent": "read mail", "tool": "search_mail", "args": {}},'
                  '{"intent": "do evil", "tool": "rm_rf_everything", "args": {}}]}')
    plan = LLMPlanner(reg, llm).plan("review mail")
    tools = [s.tool for s in plan.steps]
    assert "search_mail" in tools
    assert "rm_rf_everything" not in tools


def test_llm_planner_drops_invalid_args():
    reg = default_registry()
    # delete_file requires a 'name' argument; empty args should be rejected.
    llm = FakeLLM('{"steps": ['
                  '{"intent": "read mail", "tool": "search_mail", "args": {}},'
                  '{"intent": "delete", "tool": "delete_file", "args": {}}]}')
    plan = LLMPlanner(reg, llm).plan("review mail")
    tools = [s.tool for s in plan.steps]
    assert "search_mail" in tools
    assert "delete_file" not in tools  # missing required arg


def test_llm_planner_falls_back_when_no_valid_steps():
    reg = default_registry()
    llm = FakeLLM('{"steps": [{"intent": "x", "tool": "nope", "args": {}}]}')
    plan = LLMPlanner(reg, llm).plan("Prepare a customer follow-up email")
    # Fell back to the heuristic planner, which emits real registered tools.
    assert plan.steps and all(reg.get(s.tool) is not None for s in plan.steps)


def test_llm_planner_falls_back_in_mock_mode():
    reg = default_registry()
    llm = FakeLLM('{"steps": []}', mode="mock")
    plan = LLMPlanner(reg, llm).plan("anything")
    # Mock mode should delegate to Planner and produce steps.
    assert plan.steps


def test_llm_planner_read_tools_real_mode():
    reg = default_registry()
    llm = FakeLLM('{"read_tools": ["search_mail", "get_file_text"]}')
    chosen = LLMPlanner(reg, llm).read_tools_for("find the project report")
    assert chosen == ["search_mail", "get_file_text"]


def test_llm_planner_read_tools_falls_back_offline():
    reg = default_registry()
    llm = FakeLLM('{"read_tools": ["search_mail"]}', mode="mock")
    chosen = LLMPlanner(reg, llm).read_tools_for("anything")
    # Offline fallback returns the baseline read tools.
    assert "list_today_events" in chosen
    assert "search_mail" in chosen


def test_praxis_agent_uses_llm_planner(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    agent = PraxisAgent.persistent()
    # The agent should now instantiate an LLMPlanner by default.
    assert isinstance(agent.planner, LLMPlanner)


def test_agent_handle_mock_mode_uses_fallback():
    # Force mock mode so we don't depend on whether the dev machine has onboarding.
    agent = PraxisAgent(llm=LLMClient(mode="mock"))
    report = agent.handle("Review recent mail")
    # The heuristic baseline searches both calendar and mail.
    assert any("search_mail" in a or "search related mail" in a
               for a in report.actions)


def test_llm_planner_schema_bound_to_registry():
    reg = default_registry()
    # Replace create_email_draft with a stricter schema that omits the body field.
    old = reg.get("create_email_draft")
    assert old is not None
    narrow = Tool(
        "create_email_draft", old.risk,
        "draft", old.run,
        parameters={
            "type": "object",
            "properties": {"to": {"type": "array"}, "subject": {"type": "string"}},
            "required": ["to", "subject"],
        },
    )
    reg.register(narrow)
    llm = FakeLLM('{"steps": ['
                  '{"intent": "draft", "tool": "create_email_draft", '
                  '"args": {"to": ["a@b.com"], "subject": "hi"}}]}')
    plan = LLMPlanner(reg, llm).plan("draft an email")
    assert any(s.tool == "create_email_draft" for s in plan.steps)
