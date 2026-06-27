import hybridagent.mcp_client as mc
from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.chat_agent import GovernedChatAgent
from hybridagent.llm import LLMClient
from hybridagent.tools import Tool, ToolRegistry


def _fake_tool(name, risk):
    return Tool(name=name, risk=risk, description="mcp tool",
                run=lambda **k: "ok", parameters={"type": "object", "properties": {}})


def test_augment_registers_tools_and_extends_allowlist(monkeypatch):
    tools = [_fake_tool("mcp_x_get", RiskClass.READ),
             _fake_tool("mcp_x_delete", RiskClass.DESTRUCTIVE)]
    monkeypatch.setattr(mc, "load_mcp_tools",
                        lambda timeout=20.0: (tools, ["client-handle"]))
    reg = ToolRegistry()
    allow: set[str] = set()
    got, clients = mc.augment_registry_with_mcp(reg, allowlist=allow)
    assert {"mcp_x_get", "mcp_x_delete"} <= set(reg.names())
    assert {"mcp_x_get", "mcp_x_delete"} <= allow
    assert clients == ["client-handle"] and len(got) == 2


def test_augment_no_servers_is_noop(monkeypatch):
    monkeypatch.setattr(mc, "load_mcp_tools", lambda timeout=20.0: ([], []))
    reg = ToolRegistry()
    got, clients = mc.augment_registry_with_mcp(reg, allowlist=set())
    assert got == [] and clients == [] and reg.names() == []


def test_augmented_destructive_tool_is_held_by_broker():
    reg = ToolRegistry()
    reg.register(_fake_tool("mcp_x_delete", RiskClass.DESTRUCTIVE))

    class _CallsDelete:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "", "tool_calls": [
                {"id": "c1", "name": "mcp_x_delete", "args": {}}]}

    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set(reg.names())))
    events = GovernedChatAgent(_CallsDelete(), reg, broker).run(
        [{"role": "user", "content": "delete it"}])
    types = [e.type for e in events]
    assert "approval" in types and "tool_result" not in types


def test_daemon_ensure_agent_loads_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setattr(mc, "load_mcp_tools",
                        lambda timeout=20.0: ([_fake_tool("mcp_demo_ping",
                                                          RiskClass.READ)], []))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    assert "mcp_demo_ping" in d.agent.registry.names()
    assert "mcp_demo_ping" in d.agent.broker.policy.allowed_tools


def test_daemon_ensure_agent_survives_mcp_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))

    def _boom(timeout=20.0):
        raise RuntimeError("server crashed")

    monkeypatch.setattr(mc, "load_mcp_tools", _boom)
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()  # must not raise
    assert d.agent is not None and d._mcp_clients == []


def test_daemon_closes_mcp_clients_on_cleanup():
    from hybridagent.daemon import Daemon
    closed = []

    class _Client:
        def close(self):
            closed.append(True)

    d = Daemon(llm=LLMClient(mode="mock"))
    d._mcp_clients = [_Client(), _Client()]
    d._close_mcp_clients()
    assert closed == [True, True] and d._mcp_clients == []
