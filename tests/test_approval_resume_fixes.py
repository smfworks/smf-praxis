"""Regressions for approval resume + idle heartbeat throttle (0.19.13)."""
from __future__ import annotations

import sqlite3
import threading
import time
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


class _TwoSendPlanner(Planner):
    def plan(self, goal):
        return Plan(
            goal=goal,
            steps=[
                Step("send first", "send", {"message": f"first:{goal}"}),
                Step("send second", "send", {"message": f"second:{goal}"}),
            ],
        )

    def read_tools_for(self, goal):
        return ["echo"]


class _IdempotentSendPlanner(Planner):
    def plan(self, goal):
        return Plan(
            goal=goal,
            steps=[
                Step(
                    "send once",
                    "send_idempotent",
                    {"message": goal, "idempotency_key": f"task:{goal}"},
                )
            ],
        )

    def read_tools_for(self, goal):
        return []


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


def test_task_bound_orphan_approval_never_falls_through_to_chat(
    tmp_store, mock_agent
):
    report = mock_agent.handle("orphan", task_id="task-interrupted")
    approval_id = report.pending_approvals[0]["approval_id"]
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)

    assert daemon.approve(approval_id) is False
    assert approval_id in mock_agent.broker.pending
    assert mock_agent._send_counter["n"] == 0  # type: ignore[attr-defined]


def test_failed_task_hold_rejects_newly_minted_approvals(
    tmp_store, mock_agent, monkeypatch
):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("hold-failure", max_attempts=1)

    def fail_hold(*args, **kwargs):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(tmp_store, "hold_task_for_approvals", fail_hold)
    daemon.tick()
    assert daemon.manager is not None
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "failed"
    assert not mock_agent.broker.pending
    assert tmp_store.list_approvals() == []


def test_task_hold_requires_exact_task_provenance(tmp_store, mock_agent):
    report = mock_agent.handle("foreign-action", task_id="task-foreign")
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("actual-task", max_attempts=1)
    tmp_store.update_task(task_id, status="running")

    with pytest.raises(ValueError, match="provenance does not match task"):
        tmp_store.hold_task_for_approvals(
            task_id,
            cycle_id=report.cycle_id,
            result={"pending_approvals": report.pending_approvals},
            output="held",
            plan="send",
            actions=report.pending_approvals,
        )
    assert tmp_store.list_task_approval_actions() == []
    assert tmp_store.get_task(task_id)["status"] == "running"


def test_task_hold_requires_exact_approval_arguments(tmp_store, mock_agent):
    report = mock_agent.handle("exact-args", task_id="task-exact")
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("actual-task", max_attempts=1)
    tmp_store.update_task(task_id, status="running")
    approval_id = report.pending_approvals[0]["approval_id"]
    tmp_store._conn.execute(
        "UPDATE approvals SET provenance=? WHERE approval_id=?",
        (f"task:{task_id}", approval_id),
    )
    tmp_store._conn.commit()
    altered = [dict(report.pending_approvals[0], args={"message": "other"})]

    with pytest.raises(ValueError, match="arguments do not match approval"):
        tmp_store.hold_task_for_approvals(
            task_id,
            cycle_id=report.cycle_id,
            result={"pending_approvals": altered},
            output="held",
            plan="send",
            actions=altered,
        )
    assert tmp_store.list_task_approval_actions() == []


@pytest.mark.parametrize(
    "value",
    [
        {"nested": ("tuple",)},
        {1: "non-string-key"},
        {"number": float("nan")},
    ],
)
def test_task_action_json_rejects_non_exact_json(value):
    with pytest.raises(ValueError, match="must be strict JSON"):
        Store._task_action_json(value, "task action")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("effect_type", ["send"], "effect type must be a string"),
        ("idempotency_key", 7, "idempotency key must be a string"),
        ("provider_idempotent", 1, "provider idempotency must be boolean"),
    ],
)
def test_task_hold_rejects_coerced_action_metadata(
    tmp_store, mock_agent, field, value, message
):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("strict-metadata", max_attempts=1)
    tmp_store.update_task(task_id, status="running")
    report = mock_agent.handle("strict-metadata", task_id=task_id)
    action = dict(report.pending_approvals[0])
    action[field] = value

    with pytest.raises(ValueError, match=message):
        tmp_store.hold_task_for_approvals(
            task_id,
            cycle_id=report.cycle_id,
            result={"pending_approvals": [action]},
            output="held",
            plan="send",
            actions=[action],
        )
    assert tmp_store.list_task_approval_actions() == []


def test_task_bound_approval_refuses_direct_agent_execution(tmp_store, mock_agent):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("daemon-owned", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))

    result = mock_agent.approve(approval_id)
    assert result == "task-bound approval requires daemon execution"
    assert approval_id in mock_agent.broker.pending
    assert daemon.manager is not None
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "waiting_approval"


def test_task_effect_receipt_is_immutable_and_transitions_are_enforced(
    tmp_store, mock_agent
):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("immutable", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))

    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        tmp_store._conn.execute(
            "UPDATE task_approval_actions SET args_json='{}' "
            "WHERE task_id=? AND approval_id=?",
            (task_id, approval_id),
        )
    tmp_store._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="invalid task approval action"):
        tmp_store._conn.execute(
            "UPDATE task_approval_actions SET status='completed' "
            "WHERE task_id=? AND approval_id=?",
            (task_id, approval_id),
        )
    tmp_store._conn.rollback()

    assert daemon.approve(approval_id) is True
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        tmp_store._conn.execute(
            "UPDATE task_approval_actions SET receipt_json='{}' "
            "WHERE task_id=? AND approval_id=?",
            (task_id, approval_id),
        )
    tmp_store._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        tmp_store._conn.execute(
            "DELETE FROM task_approval_actions WHERE task_id=? AND approval_id=?",
            (task_id, approval_id),
        )
    tmp_store._conn.rollback()


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


def test_daemon_failed_approved_action_marks_task_failed(tmp_store, mock_agent):
    """A provider/tool exception must never turn an approved task into completed work."""
    tool = mock_agent.registry.get("send")
    assert tool is not None

    def fail_send(message: str, **kwargs) -> str:
        raise RuntimeError("provider unavailable")

    tool.run = fail_send
    daemon = Daemon(
        store=tmp_store,
        agent=mock_agent,
        tick_interval=0.1,
        idle_interval=0.1,
        heartbeat_interval=9999,
    )
    task_id = daemon.submit("will-fail", max_attempts=1)
    assert daemon.manager is not None
    daemon.tick()
    waiting = daemon.manager.get(task_id)
    assert waiting is not None and waiting.status == "waiting_approval"
    approval_id = next(iter(mock_agent.broker.pending))
    completed_before = daemon.state.tasks_completed
    failed_before = daemon.state.tasks_failed
    waiting_before = daemon.state.tasks_waiting_approval

    assert daemon.approve(approval_id, mode="once") is False
    failed = daemon.manager.get(task_id)
    assert failed is not None
    assert failed.status == "failed"
    assert "provider unavailable" in failed.error
    assert "manual reconciliation required" in failed.error
    assert "provider unavailable" in failed.output
    action = tmp_store.list_task_approval_actions(approval_id=approval_id)[0]
    assert action["status"] == "manual_reconciliation"
    assert daemon.state.tasks_completed == completed_before
    assert daemon.state.tasks_failed == failed_before + 1
    assert daemon.state.tasks_waiting_approval == max(0, waiting_before - 1)


def test_daemon_preclaim_denial_keeps_task_and_approval_waiting(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store,
        agent=mock_agent,
        tick_interval=0.1,
        idle_interval=0.1,
        heartbeat_interval=9999,
    )
    task_id = daemon.submit("blocked-by-kill-switch", max_attempts=1)
    assert daemon.manager is not None
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    mock_agent.broker.kill.trip()

    assert daemon.approve(approval_id, mode="once") is False
    waiting = daemon.manager.get(task_id)
    assert waiting is not None and waiting.status == "waiting_approval"
    assert approval_id in mock_agent.broker.pending


def test_daemon_waits_for_every_held_task_action(tmp_store, mock_agent):
    mock_agent.planner = _TwoSendPlanner(mock_agent.registry)
    daemon = Daemon(
        store=tmp_store,
        agent=mock_agent,
        tick_interval=0.1,
        idle_interval=0.1,
        heartbeat_interval=9999,
    )
    task_id = daemon.submit("multi", max_attempts=1)
    daemon.tick()
    approvals = list(mock_agent.broker.pending)
    assert len(approvals) == 2

    assert daemon.approve(approvals[0], mode="once") is True
    waiting = daemon.manager.get(task_id)
    assert waiting is not None and waiting.status == "waiting_approval"
    assert mock_agent._send_counter["n"] == 1  # type: ignore[attr-defined]
    assert approvals[1] in mock_agent.broker.pending

    assert daemon.approve(approvals[1], mode="once") is True
    completed = daemon.manager.get(task_id)
    assert completed is not None and completed.status == "completed"
    assert mock_agent._send_counter["n"] == 2  # type: ignore[attr-defined]


def test_daemon_rejected_task_approval_fails_waiting_task(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store,
        agent=mock_agent,
        tick_interval=0.1,
        idle_interval=0.1,
        heartbeat_interval=9999,
    )
    task_id = daemon.submit("reject-me", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))

    assert daemon.deny_approval(approval_id) is True
    failed = daemon.manager.get(task_id)
    assert failed is not None and failed.status == "failed"
    assert "rejected" in failed.error
    assert approval_id not in mock_agent.broker.pending


def test_expired_task_approval_reconciles_on_approval_attempt(tmp_store, mock_agent):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("expire-on-click", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    expired_at = time.time() - 1
    mock_agent.broker.pending[approval_id].expires_at = expired_at
    tmp_store._conn.execute(
        "UPDATE approvals SET expires_at=? WHERE approval_id=?",
        (expired_at, approval_id),
    )
    tmp_store._conn.commit()

    assert daemon.approve(approval_id, approved_by="late-reviewer") is False
    assert daemon.manager is not None
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "failed"
    assert "expired" in task.error
    action = tmp_store.list_task_approval_actions(approval_id=approval_id)[0]
    assert action["status"] == "rejected"
    assert tmp_store.get_approval(approval_id)["status"] == "expired"


def test_expired_task_approval_reconciles_on_daemon_start(tmp_store, mock_agent):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("expire-on-start", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    tmp_store._conn.execute(
        "UPDATE approvals SET expires_at=? WHERE approval_id=?",
        (time.time() - 1, approval_id),
    )
    tmp_store._conn.commit()

    restarted = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    assert restarted.manager is not None
    task = restarted.manager.get(task_id)
    assert task is not None and task.status == "failed"
    assert "expired" in task.error
    assert approval_id not in mock_agent.broker.pending
    assert tmp_store.get_approval(approval_id)["status"] == "expired"


def test_cancelling_waiting_task_cancels_unclaimed_actions(tmp_store, mock_agent):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("cancel-me", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    assert daemon.manager is not None

    assert daemon.manager.cancel(task_id) is True
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "cancelled"
    action = tmp_store.list_task_approval_actions(approval_id=approval_id)[0]
    assert action["status"] == "cancelled"
    assert tmp_store.get_approval(approval_id)["status"] == "rejected"
    assert daemon.approve(approval_id) is False
    assert mock_agent._send_counter["n"] == 0  # type: ignore[attr-defined]


def test_cancellation_refuses_durably_claimed_action(tmp_store, mock_agent):
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("in-flight", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    claimed = mock_agent.broker.approve(approval_id, approved_by="tester")
    assert claimed is not None
    assert daemon.manager is not None

    assert daemon.manager.cancel(task_id) is False
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "waiting_approval"
    action = tmp_store.list_task_approval_actions(approval_id=approval_id)[0]
    assert action["status"] == "pending_execution"


def test_two_store_approval_claim_has_one_provider_winner(tmp_path):
    database = tmp_path / "race.db"
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def run_send(message: str, **kwargs) -> str:
        with calls_lock:
            calls["n"] += 1
        return f"sent:{message}"

    store1 = Store.open(database)
    registry1 = ToolRegistry()
    registry1.register(_send_tool())
    tool1 = registry1.get("send")
    assert tool1 is not None
    tool1.run = run_send
    agent1 = PraxisAgent(
        registry=registry1,
        llm=LLMClient(mode="mock"),
        store=store1,
        planner=_SendPlanner(registry1),
    )
    agent1.broker.policy.allowed_tools = {"send"}
    daemon1 = Daemon(store=store1, agent=agent1, heartbeat_interval=9999)
    task_id = daemon1.submit("race", max_attempts=1)
    daemon1.tick()
    approval_id = next(iter(agent1.broker.pending))

    store2 = Store.open(database)
    registry2 = ToolRegistry()
    registry2.register(_send_tool())
    tool2 = registry2.get("send")
    assert tool2 is not None
    tool2.run = run_send
    agent2 = PraxisAgent(
        registry=registry2,
        llm=LLMClient(mode="mock"),
        store=store2,
        planner=_SendPlanner(registry2),
    )
    agent2.broker.policy.allowed_tools = {"send"}
    daemon2 = Daemon(store=store2, agent=agent2, heartbeat_interval=9999)

    barrier = threading.Barrier(3)
    results: list[bool] = []
    errors: list[BaseException] = []

    def approve(daemon):
        try:
            barrier.wait()
            results.append(daemon.approve(approval_id))
        except BaseException as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    workers = [
        threading.Thread(target=approve, args=(daemon,))
        for daemon in (daemon1, daemon2)
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(results) == [False, True]
    assert calls["n"] == 1
    task = store1.get_task(task_id)
    assert task is not None and task["status"] == "completed"


def _idempotent_task_agent(store, run_send, *, effect_type="send_message"):
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="send_idempotent",
            description="provider-idempotent send",
            risk=RiskClass.SEND,
            run=run_send,
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["message", "idempotency_key"],
            },
            effect_type=effect_type,
            idempotency_key_arg="idempotency_key",
        )
    )
    agent = PraxisAgent(
        registry=registry,
        llm=LLMClient(mode="mock"),
        store=store,
        planner=_IdempotentSendPlanner(registry),
    )
    agent.broker.policy.allowed_tools = set(registry.names())
    return agent


def test_daemon_restart_recovers_crash_after_claim_before_provider(tmp_store):
    calls = {"n": 0, "crash": True}

    def run_send(message: str, idempotency_key: str) -> str:
        if calls["crash"]:
            calls["crash"] = False
            raise SystemExit("crash after durable claim")
        calls["n"] += 1
        return f"sent:{message}:{idempotency_key}"

    agent = _idempotent_task_agent(tmp_store, run_send)
    daemon = Daemon(store=tmp_store, agent=agent, heartbeat_interval=9999)
    task_id = daemon.submit("before-provider", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(agent.broker.pending))
    with pytest.raises(SystemExit, match="durable claim"):
        daemon.approve(approval_id)

    restarted = Daemon(
        store=tmp_store,
        agent=_idempotent_task_agent(tmp_store, run_send),
        heartbeat_interval=9999,
    )
    recovered = restarted.manager.get(task_id)
    assert recovered is not None and recovered.status == "completed"
    assert calls["n"] == 1


def test_daemon_restart_recovers_crash_after_provider_before_receipt(tmp_store):
    provider = {"effects": set(), "calls": 0}

    def run_send(message: str, idempotency_key: str) -> str:
        provider["calls"] += 1
        provider["effects"].add(idempotency_key)
        return f"sent:{message}:{idempotency_key}"

    agent = _idempotent_task_agent(tmp_store, run_send)
    daemon = Daemon(store=tmp_store, agent=agent, heartbeat_interval=9999)
    task_id = daemon.submit("after-provider", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(agent.broker.pending))

    def crash_before_receipt(*args, **kwargs):
        raise SystemExit("crash before durable receipt")

    agent.memory.add_episodic = crash_before_receipt
    with pytest.raises(SystemExit, match="durable receipt"):
        daemon.approve(approval_id)

    restarted = Daemon(
        store=tmp_store,
        agent=_idempotent_task_agent(tmp_store, run_send),
        heartbeat_interval=9999,
    )
    recovered = restarted.manager.get(task_id)
    assert recovered is not None and recovered.status == "completed"
    assert provider["effects"] == {"task:after-provider"}
    assert provider["calls"] == 2


def test_daemon_restart_rejects_changed_provider_effect_type(tmp_store):
    calls = {"n": 0}

    def run_send(message: str, idempotency_key: str) -> str:
        calls["n"] += 1
        return f"sent:{message}:{idempotency_key}"

    agent = _idempotent_task_agent(tmp_store, run_send, effect_type="send_message_v1")
    daemon = Daemon(store=tmp_store, agent=agent, heartbeat_interval=9999)
    task_id = daemon.submit("effect-version", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(agent.broker.pending))
    assert agent.broker.approve(approval_id, approved_by="tester") is not None

    restarted = Daemon(
        store=tmp_store,
        agent=_idempotent_task_agent(
            tmp_store, run_send, effect_type="send_message_v2"
        ),
        heartbeat_interval=9999,
    )
    assert restarted.manager is not None
    task = restarted.manager.get(task_id)
    assert task is not None and task.status == "failed"
    assert "manual reconciliation required" in task.error
    assert calls["n"] == 0


def test_daemon_approve_rejects_changed_provider_contract(tmp_store, mock_agent):
    calls = {"n": 0}
    tool = mock_agent.registry.get("send")
    assert tool is not None

    def run_changed(**kwargs):
        calls["n"] += 1
        return "unexpected"

    tool.run = run_changed
    daemon = Daemon(store=tmp_store, agent=mock_agent, heartbeat_interval=9999)
    task_id = daemon.submit("contract-drift", max_attempts=1)
    daemon.tick()
    approval_id = next(iter(mock_agent.broker.pending))
    tool.effect_type = "changed-provider-effect"

    assert daemon.approve(approval_id, approved_by="reviewer") is False
    assert calls["n"] == 0
    assert daemon.manager is not None
    task = daemon.manager.get(task_id)
    assert task is not None and task.status == "failed"
    assert "registered tool contract" in task.error
    action = tmp_store.list_task_approval_actions(approval_id=approval_id)[0]
    assert action["status"] == "failed"


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
