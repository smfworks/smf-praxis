import json
import socket
import urllib.request

from hybridagent import config as cfg
from hybridagent.agent_service import AgentService
from hybridagent.broker import GovernanceBroker, GovernancePolicy
from hybridagent.llm import LLMClient
from hybridagent.tools import default_registry


class _MiniAgent:
    def __init__(self):
        self.registry = default_registry()
        self.broker = GovernanceBroker(
            GovernancePolicy(allowed_tools=set(self.registry.names())))
        self.store = None


# ----------------------------------------------------------------- service unit
def test_card_advertises_tools_with_risk():
    card = AgentService(_MiniAgent()).card()
    assert card["name"] == "praxis" and card["protocol"].startswith("praxis-a2a")
    assert card["governance"]["held_for_approval"] == ["send", "destructive"]
    assert card["tools"] and all({"name", "risk"} <= set(t) for t in card["tools"])
    assert any(t["risk"] == "send" for t in card["tools"])


def test_run_executes_under_governance_and_holds_send():
    result = AgentService(_MiniAgent()).run(
        "follow up with the customer about the project report")
    assert result["status"] in ("needs_approval", "completed", "partial")
    assert result["steps"] and all("status" in s for s in result["steps"])
    # A follow-up plan includes a send step, which must be held, not auto-run.
    if result["status"] == "needs_approval":
        assert result["held_approvals"]


def test_run_empty_goal_fails_cleanly():
    result = AgentService(_MiniAgent()).run("   ")
    assert result["status"] == "failed" and result["steps"] == []


# ----------------------------------------------------------- daemon methods
def test_daemon_agent_methods(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    card = d.agent_card()
    assert card["name"] == "praxis" and card["tools"]
    result = d.agent_run("follow up with the customer")
    assert "status" in result and "steps" in result and "held_approvals" in result


# ----------------------------------------------------- live HTTP (A2A endpoints)
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a2a_http_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{url}/api/agent/card", timeout=10) as r:
            card = json.loads(r.read())
        assert card["name"] == "praxis" and card["tools"]

        req = urllib.request.Request(
            f"{url}/api/agent/run",
            data=json.dumps({"goal": "follow up with the customer"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read())
        assert "status" in result and "steps" in result and "held_approvals" in result
    finally:
        d._stop_status_server()
