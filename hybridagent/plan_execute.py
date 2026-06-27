"""Hierarchical plan-and-execute — governed long-horizon goal completion.

The governed chat loop (:mod:`hybridagent.chat_agent`) reasons turn-by-turn; this
is the complementary *planner-driven* mode for multi-step goals. A goal is
decomposed into a **dependency graph** of tool-bound steps, executed in
dependency order under the broker, **monitored** per step, and **replanned** when
a step fails — so a long job degrades gracefully (skip the dependents of a failed
step) or recovers (swap in an alternative) instead of dead-ending on the first
error.

Every step is authorized by the same :class:`~hybridagent.broker.GovernanceBroker`,
so execution inherits the full spine: allowlist, kill-switch, the human approval
queue (send/destructive steps are *held*, never auto-run), and the audit trail.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .broker import GovernanceBroker, Verdict
from .planner import Planner, Step
from .tools import ToolRegistry
from .validation import ValidationError, validate_tool_args

PENDING = "pending"
RUNNING = "running"
DONE = "done"
HELD = "held"
DENIED = "denied"
FAILED = "failed"
SKIPPED = "skipped"          # a prerequisite did not complete
REPLANNED = "replanned"      # this step failed but a replan was issued to recover
SUPERSEDED = "superseded"    # replaced by a replan, never run

_HARD_FAIL = {FAILED, DENIED}


@dataclass
class PlanStep:
    id: str
    intent: str
    tool: str
    args: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    status: str = PENDING
    output: str = ""
    approval_id: str = ""


@dataclass
class PlanEvent:
    type: str  # plan/step_done/step_held/step_denied/step_failed/step_skipped/replan/final
    data: dict = field(default_factory=dict)


@dataclass
class ExecutionReport:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = ""  # completed / needs_approval / partial / failed
    replans: int = 0
    events: list[PlanEvent] = field(default_factory=list)

    def held_approvals(self) -> list[str]:
        return [s.approval_id for s in self.steps if s.status == HELD and s.approval_id]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for s in self.steps:
            counts[s.status] = counts.get(s.status, 0) + 1
        parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"{self.status}: {len(self.steps)} steps ({parts}); replans={self.replans}"


# replan(goal, failed_step, reason, remaining) -> replacement Steps for the remainder
ReplanFn = Callable[[str, PlanStep, str, "list[PlanStep]"], "list[Step]"]


def to_plan_steps(steps: "list[Step]", *, linear: bool = True) -> list[PlanStep]:
    """Convert flat planner steps into PlanSteps. ``linear`` makes each step
    depend on the previous one (ordered execution where a failure/hold blocks the
    rest); set it False for a set of independent steps."""
    out: list[PlanStep] = []
    prev = ""
    for i, s in enumerate(steps):
        sid = f"s{i + 1}"
        out.append(PlanStep(id=sid, intent=s.intent, tool=s.tool, args=dict(s.args),
                            depends_on=[prev] if (linear and prev) else []))
        prev = sid
    return out


class PlanExecutor:
    """Execute a dependency graph of governed tool-steps with replanning."""

    MAX_STEPS = 50  # runaway guard on total steps executed (including replans)

    def __init__(self, registry: ToolRegistry, broker: GovernanceBroker, *,
                 planner: Planner | None = None, replan: ReplanFn | None = None,
                 max_replans: int = 1, actor: str = "planner",
                 store: Any = None) -> None:
        self.registry = registry
        self.broker = broker
        self.planner = planner or Planner(registry)
        self.replan = replan
        self.max_replans = max(0, max_replans)
        self.actor = actor
        self.store = store

    def execute(self, goal: str,
                steps: "list[PlanStep] | None" = None) -> ExecutionReport:
        if steps is None:
            steps = to_plan_steps(self.planner.plan(goal).steps)
        report = ExecutionReport(goal=goal, steps=list(steps))
        cycle_id = f"plan-{uuid.uuid4().hex[:10]}"
        self._emit(report, "plan",
                   {"goal": goal, "steps": [[s.id, s.tool] for s in report.steps]},
                   cycle_id)

        done: set[str] = set()
        blocked: set[str] = set()
        replans_left = self.max_replans
        executed = 0
        guard = 0
        while True:
            guard += 1
            if guard > self.MAX_STEPS * 4:  # absolute loop guard
                break
            skipped_any = False
            for s in report.steps:
                if s.status == PENDING and any(d in blocked for d in s.depends_on):
                    s.status = SKIPPED
                    blocked.add(s.id)
                    self._emit(report, "step_skipped",
                               {"id": s.id, "intent": s.intent}, cycle_id)
                    skipped_any = True
            ready = [s for s in report.steps if s.status == PENDING
                     and all(d in done for d in s.depends_on)]
            if not ready:
                if skipped_any:
                    continue
                break
            step = ready[0]
            executed += 1
            if executed > self.MAX_STEPS:
                step.status = FAILED
                step.output = "step budget exceeded"
                self._emit(report, "step_failed",
                           {"id": step.id, "reason": step.output}, cycle_id)
                blocked.add(step.id)
                continue
            outcome = self._run_step(step, report, cycle_id)
            if outcome == DONE:
                done.add(step.id)
            elif outcome == HELD:
                blocked.add(step.id)  # dependents can't run on an un-executed action
            else:  # DENIED or FAILED -> try to replan the remainder
                if replans_left > 0 and self.replan is not None:
                    remaining = [s for s in report.steps if s.status == PENDING]
                    replacement = self._safe_replan(goal, step, outcome, remaining)
                    if replacement:
                        replans_left -= 1
                        report.replans += 1
                        for s in remaining:
                            s.status = SUPERSEDED
                        step.status = REPLANNED
                        new_steps = self._ingest_replan(replacement, report.replans)
                        report.steps.extend(new_steps)
                        self._emit(report, "replan",
                                   {"after": step.id,
                                    "new": [[s.id, s.tool] for s in new_steps]},
                                   cycle_id)
                        continue
                blocked.add(step.id)

        report.status = self._final_status(report)
        self._emit(report, "final",
                   {"status": report.status, "summary": report.summary()}, cycle_id)
        return report

    # ----------------------------------------------------------------- helpers
    def _run_step(self, step: PlanStep, report: ExecutionReport,
                  cycle_id: str) -> str:
        step.status = RUNNING
        tool = self.registry.get(step.tool)
        if tool is None:
            step.status, step.output = FAILED, f"unknown tool '{step.tool}'"
            self._emit(report, "step_failed",
                       {"id": step.id, "tool": step.tool, "reason": step.output},
                       cycle_id)
            return FAILED
        try:
            validate_tool_args(tool, step.args)
        except ValidationError as exc:
            step.status, step.output = FAILED, f"schema: {exc}"
            self._emit(report, "step_failed",
                       {"id": step.id, "tool": step.tool, "reason": step.output},
                       cycle_id)
            return FAILED
        decision = self.broker.authorize(
            actor=self.actor, tool=step.tool, risk=tool.risk, args=step.args,
            preview=f"{step.tool}({step.args})", provenance="plan",
            cycle_id=cycle_id, rationale=f"Plan step: {step.intent}")
        if decision.verdict is Verdict.ALLOW:
            try:
                step.output = str(tool.run(**step.args))
            except Exception as exc:  # a tool failure is a step failure, not a crash
                step.status, step.output = FAILED, f"ERROR: {exc}"
                self._emit(report, "step_failed",
                           {"id": step.id, "tool": step.tool, "reason": step.output},
                           cycle_id)
                return FAILED
            step.status = DONE
            self._emit(report, "step_done",
                       {"id": step.id, "intent": step.intent, "tool": step.tool,
                        "preview": self.broker.redact(step.output)[:200]}, cycle_id)
            return DONE
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            step.status, step.approval_id = HELD, decision.approval_id or ""
            self._emit(report, "step_held",
                       {"id": step.id, "intent": step.intent, "tool": step.tool,
                        "approval_id": step.approval_id}, cycle_id)
            return HELD
        step.status, step.output = DENIED, self.broker.redact(decision.reason)
        self._emit(report, "step_denied",
                   {"id": step.id, "tool": step.tool, "reason": step.output}, cycle_id)
        return DENIED

    def _safe_replan(self, goal: str, failed: PlanStep, reason: str,
                     remaining: "list[PlanStep]") -> "list[Step]":
        if self.replan is None:
            return []
        try:
            out = self.replan(goal, failed, reason, remaining)
            return list(out) if out else []
        except Exception:
            return []

    @staticmethod
    def _ingest_replan(steps: "list[Step]", gen: int) -> list[PlanStep]:
        out: list[PlanStep] = []
        prev = ""
        for i, s in enumerate(steps):
            sid = f"r{gen}_{i + 1}"
            out.append(PlanStep(id=sid, intent=s.intent, tool=s.tool,
                                args=dict(s.args),
                                depends_on=[prev] if prev else []))
            prev = sid
        return out

    @staticmethod
    def _final_status(report: ExecutionReport) -> str:
        statuses = {s.status for s in report.steps}
        if statuses & _HARD_FAIL:
            return "failed"
        if HELD in statuses:
            return "needs_approval"
        if SKIPPED in statuses:
            return "partial"
        return "completed"

    def _emit(self, report: ExecutionReport, etype: str, data: dict,
              cycle_id: str) -> None:
        report.events.append(PlanEvent(etype, data))
        if self.store is not None:
            try:
                self.store.add_compliance_event(cycle_id, f"plan_{etype}", data)
            except Exception:
                pass
