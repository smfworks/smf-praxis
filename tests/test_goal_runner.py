"""Tests for the Goal Runner (H10) — Level 1 autonomous loop."""
from __future__ import annotations

from hybridagent.goal_runner import GoalRunner


class _FakeAgent:
    """Minimal PraxisAgent stub for loop testing."""
    def __init__(self, summaries: list[str], *, pending: list[dict] | None = None):
        self._summaries = summaries
        self._pending = pending or []
        self.approvals_called = 0

    def handle(self, goal: str):
        class _R:
            def __init__(self, s, p):
                self.goal = goal
                self.actions = [s]
                self.pending_approvals = list(p)
                self.injection_flags = []
            def summary(self):
                return self.actions[0]
        return _R(self._summaries.pop(0) if self._summaries else "done",
                  self._pending)

    def approve(self, _id):
        self.approvals_called += 1
        return {"status": "approved"}


class _AlwaysApproveVerifier:
    """Verifier stub that always approves (for deterministic loop tests)."""
    from hybridagent.verifier import VerificationVerdict
    def verify(self, task, answer, *, held=False, action_denied=False):
        return self.VerificationVerdict(approved=True)


class _AlwaysReviseVerifier:
    """Verifier stub that never approves (forces max_turns)."""
    from hybridagent.verifier import VerificationVerdict
    def verify(self, task, answer, *, held=False, action_denied=False):
        return self.VerificationVerdict(approved=False, critique="not done")


def test_goal_loop_stops_on_approval():
    """When the verifier approves, the loop stops before max_turns."""
    agent = _FakeAgent(["working", "done: all tests pass"])
    runner = GoalRunner(agent, max_turns=5,
                        verifier=_AlwaysApproveVerifier(), threshold=1.0)
    result = runner.run("fix the tests")
    assert result.stopped_reason == "approved"
    assert result.n_turns <= 2  # approved on first turn (progress=1.0)


def test_goal_loop_hits_max_turns_when_never_approved():
    """When the verifier never approves, the loop runs to max_turns."""
    agent = _FakeAgent(["still working"] * 10)
    runner = GoalRunner(agent, max_turns=3,
                        verifier=_AlwaysReviseVerifier(), threshold=1.0)
    result = runner.run("fix the tests")
    assert result.stopped_reason == "max_turns"
    assert result.n_turns == 3


def test_goal_loop_blocks_on_held_action():
    """A held action stops the loop -- never auto-approve a held turn."""
    agent = _FakeAgent(["drafted, pending approval"],
                       pending=[{"approval_id": "a1"}])
    runner = GoalRunner(agent, max_turns=5,
                        verifier=_AlwaysApproveVerifier(), threshold=1.0)
    result = runner.run("send the email")
    assert result.stopped_reason == "blocked"
    assert result.n_turns == 1
    assert result.turns[0].verdict == "blocked"


def test_goal_loop_approve_all_auto_approves():
    """With --approve-all, held actions are approved (dev-only)."""
    agent = _FakeAgent(["drafted, pending approval"],
                       pending=[{"approval_id": "a1"}])
    runner = GoalRunner(agent, max_turns=5,
                        verifier=_AlwaysApproveVerifier(), threshold=1.0)
    runner.run("send the email", approve_all=True)
    assert agent.approvals_called == 1


def test_goal_result_record_is_json_serializable():
    """The to_record output is the anti-comprehension-rot log."""
    import json
    agent = _FakeAgent(["done"])
    runner = GoalRunner(agent, max_turns=3,
                        verifier=_AlwaysApproveVerifier(), threshold=1.0)
    result = runner.run("test goal")
    record = runner.to_record(result)
    # Must be JSON-serializable
    json.dumps(record)
    assert record["goal"] == "test goal"
    assert "turns" in record
    assert "stopped_reason" in record


def test_goal_loop_max_turns_guardrail():
    """max_turns is a hard cap regardless of verifier behavior."""
    agent = _FakeAgent(["x"] * 100)
    runner = GoalRunner(agent, max_turns=2,
                        verifier=_AlwaysReviseVerifier(), threshold=1.0)
    result = runner.run("goal")
    assert result.n_turns == 2
    assert result.stopped_reason == "max_turns"