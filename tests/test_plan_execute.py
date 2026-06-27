from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.plan_execute import (
    ExecutionReport,
    PlanExecutor,
    PlanStep,
    to_plan_steps,
)
from hybridagent.planner import Step
from hybridagent.tools import Tool, ToolRegistry


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
    from hybridagent.persistence import Store
    store = Store.open(tmp_path / "praxis.db")
    try:
        steps = to_plan_steps([Step("read", "get_data")])
        PlanExecutor(_registry(READ), _broker("get_data"),
                     store=store).execute("g", steps=steps)
        events = store.list_compliance_events()
        assert any(e["event_type"].startswith("plan_") for e in events)
    finally:
        store.close()
