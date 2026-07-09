"""Regressions for approval resume + idle heartbeat throttle (0.19.13)."""
from __future__ import annotations

import urllib.request

import pytest

from hybridagent.agent import PraxisAgent
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict
from hybridagent.daemon import Daemon, _find_port
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store
from hybridagent.planner import Plan, Planner, Step
from hybridagent.tools import Tool, ToolRegistry


@pytest.fixture
def tmp_store(tmp_path):
    return Store.open(tmp_path / "praxis.db")


def _send_tool(counter=None):
    def run_send(message: str, **kwargs) -> str:
        if counter is not None:
            counter["n"] += 1
        return f"sent:{message}"

    return Tool(
        name="send", description="send", risk=RiskClass.SEND,
        run=run_send, parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )


def _echo_tool():
    def run(message: str, **kwargs) -> str:
        return f"echo:{message}"

    return Tool(
        name="echo", description="echo", risk=RiskClass.DRAFT,
        run=run, parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )


class _SendPlanner(Planner):
    def plan(self, goal):
        return Plan(goal=goal, steps=[Step("send it", "send", {"message": goal})])

    def read_tools_for(self, goal):
        return ["echo"]


@pytest.fixture
def mock_agent(tmp_store):
    counter = {"n": 0}
    registry = ToolRegistry()
    registry.register(_echo_tool())
    registry.register(_send_tool(counter))
    agent = PraxisAgent(
        registry=registry,
        llm=LLMClient(mode="mock"),
        store=tmp_store,
        planner=_SendPlanner(registry),
    )
    agent.broker.policy.allowed_tools = set(registry.names())
    agent.broker.policy.autonomous_risks = {RiskClass.READ, RiskClass.DRAFT}
    agent._send_counter = counter  # type: ignore[attr-defined]
    return agent


def test_one_shot_allow_is_consumed_after_one_use():
    b = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    d1 = b.authorize("a", "send_email", RiskClass.SEND, {"draft_id": "d1"})
    assert d1.verdict is Verdict.NEEDS_APPROVAL
    b.allow_tool_once("send_email")
    d2 = b.authorize("a", "send_email", RiskClass.SEND, {"draft_id": "d2"})
    assert d2.verdict is Verdict.ALLOW
    assert d2.policy_rule == "session_oneshot_allow"
    d3 = b.authorize("a", "send_email", RiskClass.SEND, {"draft_id": "d3"})
    assert d3.verdict is Verdict.NEEDS_APPROVAL


def test_one_shot_still_respects_kill_switch():
    b = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    b.allow_tool_once("send_email")
    b.kill.trip()
    d = b.authorize("a", "send_email", RiskClass.SEND, {"draft_id": "x"})
    assert d.verdict is Verdict.DENY


def test_daemon_approve_executes_waiting_task(tmp_store, mock_agent):
    """Dashboard approve must actually run the held tool (was a no-op before)."""
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, heartbeat_interval=9999,
    )
    task_id = daemon.submit("hello-world", max_attempts=1)
    daemon.tick()
    task = daemon.manager.get(task_id)
    assert task is not None
    assert task.status == "waiting_approval"
    aid = list(mock_agent.broker.pending.keys())[0]
    assert daemon.approve(aid, mode="once") is True
    task = daemon.manager.get(task_id)
    assert task is not None
    assert task.status == "completed"
    assert mock_agent._send_counter["n"] == 1  # type: ignore[attr-defined]
    blob = (task.output or "") + " " + str(getattr(task, "result", "") or "")
    assert "hello-world" in blob or "sent:" in blob
    # Task path must NOT leave a one-shot grant (execution bypasses authorize).
    assert "send" not in mock_agent.broker._session_one_shot_tools
    # Next authorize of the same tool still requires approval.
    d = mock_agent.broker.authorize(
        "a", "send", RiskClass.SEND, {"message": "another"})
    assert d.verdict is Verdict.NEEDS_APPROVAL


def test_daemon_approve_chat_path_emits_resume_without_execute(tmp_store, mock_agent):
    """Chat holds resolve via resume SSE + one-shot allow, not immediate execute."""
    events: list[tuple[str, dict]] = []
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, heartbeat_interval=9999,
    )
    daemon._ensure_agent()
    orig_emit = daemon.emit_event

    def capture(event_type, payload=None):
        events.append((event_type, payload or {}))
        return orig_emit(event_type, payload or {})

    daemon.emit_event = capture  # type: ignore[method-assign]

    d = mock_agent.broker.authorize(
        "praxis-chat", "send", RiskClass.SEND, {"message": "hi"},
        preview="send hi", provenance="chat",
    )
    assert d.verdict is Verdict.NEEDS_APPROVAL
    aid = d.approval_id
    before = mock_agent._send_counter["n"]  # type: ignore[attr-defined]
    assert daemon.approve(aid, mode="once") is True
    assert aid not in mock_agent.broker.pending
    # Tool was NOT executed on chat path.
    assert mock_agent._send_counter["n"] == before  # type: ignore[attr-defined]
    # One-shot granted for the upcoming resume authorize.
    assert "send" in mock_agent.broker._session_one_shot_tools
    assert any(e[0] == "resume" for e in events)
    resume = [e for e in events if e[0] == "resume"][0][1]
    assert resume.get("tool") == "send"
    assert resume.get("mode") == "once"


def test_idle_tick_skips_heartbeat_when_throttled(tmp_store, mock_agent):
    calls = {"n": 0}
    orig = mock_agent.heartbeat

    def hb(**kw):
        calls["n"] += 1
        return orig(**kw)

    mock_agent.heartbeat = hb  # type: ignore[method-assign]
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.05,
        idle_interval=0.05, heartbeat_interval=600.0,
    )
    daemon.tick()  # first idle: heartbeat (last=0 so interval elapsed)
    assert calls["n"] == 1
    daemon.tick()  # second idle within throttle window: no heartbeat
    assert calls["n"] == 1


def test_dashboard_html_has_resume_activeid_and_deny(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
        heartbeat_interval=9999,
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        with urllib.request.urlopen(f"{base}/") as resp:
            html = resp.read().decode("utf-8")
        assert "currentConvId" not in html
        assert "activeId" in html
        assert "denyApproval" in html
        assert "tickErrors" in html
        assert 'class="deny"' in html or "button.deny" in html
    finally:
        daemon._stop_status_server()
