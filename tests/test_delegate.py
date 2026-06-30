"""Delegate tool — model-callable scoped subagent delegation (Phase A / G3)."""
from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_delegate_tool_registered_as_draft(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import RiskClass
    from hybridagent.tools import default_registry
    reg = default_registry()
    assert "delegate" in reg.names()
    assert reg.get("delegate").risk is RiskClass.DRAFT


def test_subagent_registries_exclude_delegate(tmp_path, monkeypatch):
    """Subagents must not get the delegate tool, else unbounded recursion."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent.orchestrator import AgentSpecializer
    for role in ("researcher", "drafter", "compliance", "predictor"):
        assert "delegate" not in AgentSpecializer.registry_for(role).names()


def test_delegate_requires_goal():
    from hybridagent.real_tools import delegate
    assert "required" in delegate(goal="")


def test_delegate_runs_subagent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import delegate
    out = delegate(goal="summarize today's events", role="researcher")
    assert "subagent" in out
    assert "run" in out


def test_delegate_is_governed_draft(tmp_path, monkeypatch):
    """The delegate call itself is DRAFT (proceeds), but a subagent's SEND tools
    still get held — verify the broker classifies delegate as draft (autonomous)."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent.agent import PraxisAgent
    from hybridagent.broker import RiskClass
    a = PraxisAgent.persistent()
    dec = a.broker.authorize("agent", "delegate", RiskClass.DRAFT,
                             {"goal": "research X"}, cycle_id="t")
    assert dec.verdict.value == "allow"
