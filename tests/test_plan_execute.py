import pytest

from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.checkpoints import CheckpointRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.plan_execute import (
    ExecutionReport,
    PlanExecutor,
    PlanStep,
    to_plan_steps,
)
from hybridagent.planner import Plan, Planner, Step
from hybridagent.tools import Tool, ToolRegistry
from hybridagent.workspaces import WorkspaceDirectory


def _schema(*req):
    return {"type": "object",
            "properties": {k: {"type": "string"} for k in req}, "required": list(req)}


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _boom(**k):
    raise RuntimeError("boom")


READ = Tool("get_data", RiskClass.READ, "read", lambda **k: "data")
READ2 = Tool("get_more", RiskClass.READ, "read2", lambda **k: "more")
SEND = Tool("send_it", RiskClass.SEND, "send", lambda **k: "SENT")
BAD = Tool("bad_tool", RiskClass.READ, "raises", _boom)
NEEDS_ARG = Tool("needs_arg", RiskClass.READ, "schema", lambda x="", **k: x,
                 parameters=_schema("x"))


def _broker(*names):
    return GovernanceBroker(GovernancePolicy(allowed_tools=set(names)))


def _statuses(report):
    return {s.id: s.status for s in report.steps}


def test_to_plan_steps_links_linearly():
    steps = to_plan_steps([Step("a", "t1"), Step("b", "t2"), Step("c", "t3")])
    assert [s.id for s in steps] == ["s1", "s2", "s3"]
    assert steps[0].depends_on == [] and steps[1].depends_on == ["s1"]
    assert steps[2].depends_on == ["s2"]


def test_linear_read_plan_completes():
    steps = to_plan_steps([Step("read", "get_data"), Step("more", "get_more")])
    report = PlanExecutor(_registry(READ, READ2),
                          _broker("get_data", "get_more")).execute("job", steps=steps)
    assert isinstance(report, ExecutionReport)
    assert report.status == "completed"
    assert all(s.status == "done" for s in report.steps)


def test_consequential_step_held_and_dependent_skipped():
    steps = [
        PlanStep(id="s1", intent="read", tool="get_data", args={}),
        PlanStep(id="s2", intent="send", tool="send_it", args={}, depends_on=["s1"]),
        PlanStep(id="s3", intent="after", tool="get_data", args={}, depends_on=["s2"]),
    ]
    report = PlanExecutor(_registry(READ, SEND),
                          _broker("get_data", "send_it")).execute("g", steps=steps)
    st = _statuses(report)
    assert st == {"s1": "done", "s2": "held", "s3": "skipped"}
    assert report.status == "needs_approval"
    assert len(report.held_approvals()) == 1


def test_independent_branch_completes_while_other_blocked():
    # s1->s2(held)->s4(skipped); s3 is an independent branch that still completes.
    steps = [
        PlanStep(id="s1", intent="read", tool="get_data", args={}),
        PlanStep(id="s2", intent="send", tool="send_it", args={}, depends_on=["s1"]),
        PlanStep(id="s3", intent="independent", tool="get_more", args={}),
        PlanStep(id="s4", intent="after send", tool="get_data", args={},
                 depends_on=["s2"]),
    ]
    report = PlanExecutor(_registry(READ, READ2, SEND),
                          _broker("get_data", "get_more", "send_it")).execute(
        "g", steps=steps)
    st = _statuses(report)
    assert st["s3"] == "done"  # independent branch unaffected by the held send
    assert st["s2"] == "held" and st["s4"] == "skipped"
    assert report.status == "needs_approval"


def test_denied_step_fails_without_replanner():
    steps = [PlanStep(id="s1", intent="read", tool="get_data", args={})]
    # get_data is NOT allowlisted -> DENY.
    report = PlanExecutor(_registry(READ), _broker()).execute("g", steps=steps)
    assert _statuses(report)["s1"] == "denied" and report.status == "failed"


def test_tool_error_marks_step_failed():
    steps = [PlanStep(id="s1", intent="boom", tool="bad_tool", args={})]
    report = PlanExecutor(_registry(BAD), _broker("bad_tool")).execute("g", steps=steps)
    assert _statuses(report)["s1"] == "failed" and report.status == "failed"


def test_schema_invalid_args_fail_before_authorization():
    steps = [PlanStep(id="s1", intent="bad args", tool="needs_arg", args={})]
    report = PlanExecutor(_registry(NEEDS_ARG),
                          _broker("needs_arg")).execute("g", steps=steps)
    assert _statuses(report)["s1"] == "failed"


def test_replan_recovers_and_completes():
    steps = [PlanStep(id="s1", intent="flaky", tool="bad_tool", args={})]

    def replan(goal, failed, reason, remaining):
        return [Step("recover", "get_data", {})]

    report = PlanExecutor(_registry(BAD, READ), _broker("bad_tool", "get_data"),
                          replan=replan, max_replans=1).execute("g", steps=steps)
    assert report.replans == 1 and report.status == "completed"
    assert any(s.tool == "get_data" and s.status == "done" for s in report.steps)
    assert any(s.status == "replanned" for s in report.steps)


def test_replan_budget_is_bounded():
    steps = [PlanStep(id="s1", intent="flaky", tool="bad_tool", args={})]
    calls = []

    def replan(goal, failed, reason, remaining):
        calls.append(failed.tool)
        return [Step("retry bad", "bad_tool", {})]  # replacement also fails

    report = PlanExecutor(_registry(BAD), _broker("bad_tool"),
                          replan=replan, max_replans=1).execute("g", steps=steps)
    assert report.replans == 1  # exactly one replan, then it gives up
    assert report.status == "failed"
    assert len(calls) == 1


def test_no_replanner_degrades_gracefully():
    steps = [
        PlanStep(id="s1", intent="flaky", tool="bad_tool", args={}),
        PlanStep(id="s2", intent="after", tool="get_data", args={}, depends_on=["s1"]),
    ]
    report = PlanExecutor(_registry(BAD, READ),
                          _broker("bad_tool", "get_data")).execute("g", steps=steps)
    st = _statuses(report)
    assert st["s1"] == "failed" and st["s2"] == "skipped"
    assert report.status == "failed"


def test_missing_dependency_fails_not_completes():
    # A dependency id that never completes (typo / non-existent) must FAIL the
    # plan, not silently report "completed" with an orphaned pending step.
    steps = [PlanStep(id="s1", intent="orphan", tool="get_data", args={},
                      depends_on=["nonexistent"])]
    report = PlanExecutor(_registry(READ), _broker("get_data")).execute("g", steps=steps)
    assert report.status == "failed"
    assert _statuses(report)["s1"] == "failed"


def test_cyclic_dependency_fails():
    steps = [
        PlanStep(id="s1", intent="a", tool="get_data", args={}, depends_on=["s2"]),
        PlanStep(id="s2", intent="b", tool="get_data", args={}, depends_on=["s1"]),
    ]
    report = PlanExecutor(_registry(READ), _broker("get_data")).execute("g", steps=steps)
    assert report.status == "failed"
    assert all(s.status == "failed" for s in report.steps)


def test_compliance_events_recorded_when_store_present(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        steps = to_plan_steps([Step("read", "get_data")])
        PlanExecutor(_registry(READ), _broker("get_data"), store=store).execute("g", steps=steps)
        events = store.list_compliance_events()
        assert any(e["event_type"].startswith("plan_") for e in events)
    finally:
        store.close()
def _durable_scope(tmp_path):
    store = Store(tmp_path / "praxis.db")
    org, owner = OrganizationDirectory(store).bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-1", "matter", "Matter", owner_user_id=owner.user_id
    )
    checkpoints = CheckpointRegistry(store)
    run = checkpoints.create_run(
        org.organization_id,
        workspace.workspace_id,
        kind="plan",
        created_by=owner.user_id,
        state={"goal": "work", "steps": [], "outbox": [], "effect_receipts": []},
        schema_manifest={"name": "plan-executor", "version": 1},
    )
    return store, checkpoints, run, org.organization_id, workspace.workspace_id, owner.user_id

def test_fresh_durable_executor_plans_instead_of_accepting_empty_seed(tmp_path):
    _, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)

    class OneStepPlanner(Planner):
        def __init__(self):
            pass

        def plan(self, goal):
            return Plan(goal, [Step("read", "get_data")])

    report = PlanExecutor(
        _registry(READ),
        _broker("get_data"),
        planner=OneStepPlanner(),
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    ).execute("fresh work")
    assert report.status == "completed"
    assert [(step.tool, step.status) for step in report.steps] == [("get_data", "done")]

def test_durable_plan_executor_interrupts_with_typed_outbox_checkpoint(tmp_path):
    store, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"get_data", "send_it"}), store=store)
    steps = [
        PlanStep(id="s1", intent="read", tool="get_data", args={}),
        PlanStep(
            id="s2",
            intent="send",
            tool="send_it",
            args={
                "target": "public_web",
                "text": "hello",
                "classification": "public",
                "connector": "public_web",
            },
            depends_on=["s1"],
        ),
        PlanStep(id="s3", intent="after", tool="get_data", args={}, depends_on=["s2"]),
    ]
    report = PlanExecutor(
        _registry(READ, SEND),
        broker,
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    ).execute("g", steps=steps)
    durable = checkpoints.get_run(org_id, workspace_id, run.run_id)
    latest = checkpoints.latest(org_id, workspace_id, run.run_id)
    assert report.status == "needs_approval"
    assert durable is not None and durable.status == "interrupted"
    assert durable.interrupt_type == "approval"
    assert durable.interrupt_payload["step_id"] == "s2"
    assert durable.interrupt_payload["approval_id"].startswith("appr-")
    assert latest is not None
    assert [step["status"] for step in latest.state["steps"]] == ["done", "held", "skipped"]
    assert latest.state["outbox"] == [
        {
            "approval_id": durable.interrupt_payload["approval_id"],
            "args": {
                "classification": "public",
                "connector": "public_web",
                "target": "public_web",
                "text": "hello",
            },
            "effect_type": "send_it",
            "intent": "send",
            "requires_provider_idempotency": False,
            "status": "pending_approval",
            "step_id": "s2",
            "tool": "send_it",
        }
    ]

def test_durable_plan_executor_resumes_from_checkpointed_plan_steps(tmp_path):
    _, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    checkpoints.checkpoint(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        state={
            "goal": "resume work",
            "steps": [
                {
                    "id": "s1",
                    "intent": "already done",
                    "tool": "get_data",
                    "args": {},
                    "depends_on": [],
                    "status": "done",
                    "output": "data",
                    "approval_id": "",
                },
                {
                    "id": "s2",
                    "intent": "resume pending",
                    "tool": "get_more",
                    "args": {},
                    "depends_on": ["s1"],
                    "status": "pending",
                    "output": "",
                    "approval_id": "",
                },
            ],
            "outbox": [],
            "effect_receipts": [],
        },
    )
    report = PlanExecutor(
        _registry(READ, READ2),
        _broker("get_data", "get_more"),
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    ).execute("resume work")
    latest = checkpoints.latest(org_id, workspace_id, run.run_id)
    assert report.status == "completed"
    assert [step.status for step in report.steps] == ["done", "done"]
    assert latest is not None
    assert [step["status"] for step in latest.state["steps"]] == ["done", "done"]

def test_durable_plan_executor_honors_cancellation_before_next_step(tmp_path):
    _, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    calls: list[str] = []
    counting = Tool(
        "counting_read", RiskClass.READ, "counts", lambda **k: calls.append("ran") or "ok"
    )
    checkpoints.checkpoint(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        state={
            "goal": "cancelled",
            "steps": [
                {
                    "id": "s1",
                    "intent": "already done",
                    "tool": "get_data",
                    "args": {},
                    "depends_on": [],
                    "status": "done",
                    "output": "data",
                    "approval_id": "",
                },
                {
                    "id": "s2",
                    "intent": "must not run",
                    "tool": "counting_read",
                    "args": {},
                    "depends_on": ["s1"],
                    "status": "pending",
                    "output": "",
                    "approval_id": "",
                },
            ],
            "outbox": [],
            "effect_receipts": [],
        },
    )
    checkpoints.cancel(org_id, workspace_id, run.run_id, actor_id=actor_id, reason="stop")
    report = PlanExecutor(
        _registry(READ, counting),
        _broker("get_data", "counting_read"),
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    ).execute("cancelled")
    assert report.status == "cancelled"
    assert calls == []

def test_durable_plan_executor_never_executes_terminal_run(tmp_path):
    _, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    calls: list[str] = []
    counting = Tool(
        "counting_read", RiskClass.READ, "counts", lambda **kwargs: calls.append("ran") or "ok"
    )
    checkpoints.fail(org_id, workspace_id, run.run_id, actor_id=actor_id, reason="failed")

    report = PlanExecutor(
        _registry(counting),
        _broker("counting_read"),
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    ).execute(
        "must not run", steps=[PlanStep(id="s1", intent="blocked", tool="counting_read", args={})]
    )
    assert report.status == "failed"
    assert calls == []

def test_durable_plan_executor_resumes_approved_held_step(tmp_path):
    calls: list[str] = []
    send = Tool(
        "approved_send",
        RiskClass.SEND,
        "send",
        lambda **kwargs: calls.append(kwargs["text"]) or "SENT",
        parameters=_schema("target", "text", "classification", "connector", "idempotency_key"),
        effect_type="send_message",
        idempotency_key_arg="idempotency_key",
    )
    store, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"approved_send", "get_data"}), store=store
    )
    executor = PlanExecutor(
        _registry(send, READ),
        broker,
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    )
    args = {
        "target": "approved_business_system",
        "text": "hello",
        "classification": "internal",
        "connector": "approved_business_system",
        "idempotency_key": "provider-key-approved",
    }
    held = executor.execute(
        "send",
        steps=[
            PlanStep(id="s1", intent="send", tool="approved_send", args=args),
            PlanStep(
                id="s2",
                intent="read after send",
                tool="get_data",
                args={},
                depends_on=["s1"],
            ),
        ],
    )
    approval_id = held.held_approvals()[0]
    assert [step.status for step in held.steps] == ["held", "skipped"]
    assert broker.approve(approval_id, approved_by=actor_id, approved_role="owner") is not None
    checkpoints.resume(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        schema_manifest={"name": "plan-executor", "version": 1},
    )

    resumed = executor.execute("send")
    assert resumed.status == "completed"
    assert [step.status for step in resumed.steps] == ["done", "done"]
    assert calls == ["hello"]
    assert (
        checkpoints.get_effect(org_id, workspace_id, run.run_id, "provider-key-approved")
        is not None
    )

def test_durable_plan_executor_retries_provider_idempotent_intent_after_crash(tmp_path):
    calls: list[dict] = []

    def _send_effect(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise SystemExit("simulated process crash after provider accepted request")
        return "SENT"

    send_effect = Tool(
        "send_effect",
        RiskClass.SEND,
        "send",
        _send_effect,
        parameters=_schema("target", "text", "classification", "connector", "idempotency_key"),
        effect_type="send_message",
        idempotency_key_arg="idempotency_key",
    )
    store, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_effect"}), store=store)
    executor = PlanExecutor(
        _registry(send_effect),
        broker,
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    )
    args = {
        "target": "approved_business_system",
        "text": "hello",
        "classification": "internal",
        "connector": "approved_business_system",
        "idempotency_key": "provider-key-1",
    }
    held = executor.execute(
        "send", steps=[PlanStep(id="s1", intent="send", tool="send_effect", args=args)]
    )
    approval_id = held.held_approvals()[0]
    assert calls == []
    assert broker.approve(approval_id, approved_by=actor_id, approved_role="owner") is not None
    checkpoints.resume(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        schema_manifest={"name": "plan-executor", "version": 1},
    )
    with pytest.raises(SystemExit, match="simulated process crash"):
        executor.execute("send")
    crashed = checkpoints.latest(org_id, workspace_id, run.run_id)
    assert crashed is not None
    assert crashed.state["steps"][0]["status"] == "running"
    assert crashed.state["outbox"][0]["status"] == "pending_execution"

    report = executor.execute("send")
    latest = checkpoints.latest(org_id, workspace_id, run.run_id)
    receipt = checkpoints.get_effect(org_id, workspace_id, run.run_id, "provider-key-1")
    assert report.status == "completed"
    assert len(calls) == 2
    assert receipt is not None and receipt.effect_type == "send_message"
    assert latest is not None
    assert latest.state["effect_receipts"] == [
        {
            "effect_type": "send_message",
            "idempotency_key": "provider-key-1",
            "receipt_id": receipt.receipt_id,
            "step_id": "s1",
        }
    ]
    assert latest.state["delivery_semantics"] == {
        "consequential_side_effects": "provider_idempotent_at_least_once",
        "exactly_once_network_execution": False,
    }

def test_durable_plan_executor_recovers_receipt_written_before_checkpoint(tmp_path):
    calls: list[str] = []
    send = Tool(
        "receipt_send",
        RiskClass.SEND,
        "send",
        lambda **kwargs: calls.append(kwargs["text"]) or "SENT",
        parameters=_schema("target", "text", "classification", "connector", "idempotency_key"),
        effect_type="send_message",
        idempotency_key_arg="idempotency_key",
    )
    store, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"receipt_send"}), store=store)
    original_checkpoint = checkpoints.checkpoint

    def crash_after_receipt(*args, **kwargs):
        if (
            checkpoints.get_effect(org_id, workspace_id, run.run_id, "provider-key-receipt")
            is not None
        ):
            raise SystemExit("simulated crash after receipt commit")
        return original_checkpoint(*args, **kwargs)

    executor = PlanExecutor(
        _registry(send),
        broker,
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    )
    args = {
        "target": "approved_business_system",
        "text": "hello",
        "classification": "internal",
        "connector": "approved_business_system",
        "idempotency_key": "provider-key-receipt",
    }
    held = executor.execute(
        "send", steps=[PlanStep(id="s1", intent="send", tool="receipt_send", args=args)]
    )
    approval_id = held.held_approvals()[0]
    assert broker.approve(approval_id, approved_by=actor_id, approved_role="owner") is not None
    checkpoints.resume(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        schema_manifest={"name": "plan-executor", "version": 1},
    )
    checkpoints.checkpoint = crash_after_receipt
    with pytest.raises(SystemExit, match="after receipt commit"):
        executor.execute("send")
    checkpoints.checkpoint = original_checkpoint

    report = executor.execute("send")
    latest = checkpoints.latest(org_id, workspace_id, run.run_id)
    assert report.status == "completed"
    assert calls == ["hello"]
    assert latest is not None
    assert latest.state["steps"][0]["status"] == "done"
    assert latest.state["outbox"][0]["status"] == "completed"
    assert latest.state["effect_receipts"][0]["idempotency_key"] == "provider-key-receipt"

def test_durable_plan_executor_rejects_malformed_persisted_step(tmp_path):
    _, checkpoints, run, org_id, workspace_id, actor_id = _durable_scope(tmp_path)
    checkpoints.checkpoint(
        org_id,
        workspace_id,
        run.run_id,
        actor_id=actor_id,
        state={
            "goal": "malformed",
            "steps": [
                {
                    "id": 1,
                    "intent": "read",
                    "tool": "get_data",
                    "args": {},
                    "depends_on": [],
                    "status": "pending",
                    "output": "",
                    "approval_id": "",
                }
            ],
            "outbox": [],
            "effect_receipts": [],
        },
    )
    executor = PlanExecutor(
        _registry(READ),
        _broker("get_data"),
        checkpoints=checkpoints,
        organization_id=org_id,
        workspace_id=workspace_id,
        run_id=run.run_id,
        actor_id=actor_id,
    )
    with pytest.raises(ValueError, match="string fields"):
        executor.execute("malformed")
