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

When durable scoped checkpoints are enabled, consequential actions follow a
provider-idempotent at-least-once protocol, not exactly-once network execution:
Praxis checkpoints the intent/outbox before execution and records an immutable
effect receipt after the provider call returns. A crash in that gap can leave an
external effect without a local receipt, so providers must expose idempotency
keys when available and callers should supply them via the tool contract.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .broker import GovernanceBroker, Verdict
from .checkpoints import CheckpointRegistry
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
_STEP_STATUSES = frozenset(
    {
        PENDING,
        RUNNING,
        DONE,
        HELD,
        DENIED,
        FAILED,
        SKIPPED,
        REPLANNED,
        SUPERSEDED,
    }
)


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

    def __init__(
        self,
        registry: ToolRegistry,
        broker: GovernanceBroker,
        *,
        planner: Planner | None = None,
        replan: ReplanFn | None = None,
        max_replans: int = 1,
        actor: str = "planner",
        store: Any = None,
        on_event: "Callable[[str, dict], None] | None" = None,
        checkpoints: CheckpointRegistry | None = None,
        organization_id: str = "",
        workspace_id: str = "",
        run_id: str = "",
        actor_id: str = "",
    ) -> None:
        self.registry = registry
        self.broker = broker
        self.planner = planner or Planner(registry)
        self.replan = replan
        self.max_replans = max(0, max_replans)
        self.actor = actor
        self.store = store
        self.on_event = on_event
        self.checkpoints = checkpoints
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.actor_id = actor_id
        if checkpoints is not None and not all((organization_id, workspace_id, run_id, actor_id)):
            raise ValueError("durable PlanExecutor requires complete scoped run context")

    def execute(self, goal: str, steps: "list[PlanStep] | None" = None) -> ExecutionReport:
        steps = self._initial_steps(goal, steps)
        report = ExecutionReport(goal=goal, steps=list(steps))
        cycle_id = f"plan-{uuid.uuid4().hex[:10]}"
        durable_state = self._load_durable_state(goal, report)
        report.replans = durable_state["replans"]
        self._emit(
            report,
            "plan",
            {
                "goal": goal,
                "steps": [[s.id, s.tool] for s in report.steps],
                "nodes": [
                    {
                        "id": s.id,
                        "tool": s.tool,
                        "intent": s.intent,
                        "depends_on": list(s.depends_on),
                    }
                    for s in report.steps
                ],
            },
            cycle_id,
        )
        self._checkpoint_report(goal, report, durable_state)

        done: set[str] = set()
        blocked: set[str] = set()
        held_step: PlanStep | None = None
        for step in report.steps:
            if step.status == DONE:
                done.add(step.id)
            elif step.status == HELD:
                blocked.add(step.id)
                if held_step is None:
                    held_step = step
            elif step.status in (DENIED, FAILED, SKIPPED, REPLANNED, SUPERSEDED):
                blocked.add(step.id)
        replans_left = max(0, self.max_replans - report.replans)
        executed = len(done)
        guard = 0
        while True:
            stopping_status = self._stopping_status()
            if stopping_status:
                report.status = stopping_status
                self._emit(
                    report,
                    "final",
                    {"status": report.status, "summary": report.summary()},
                    cycle_id,
                )
                return report
            guard += 1
            if guard > self.MAX_STEPS * 4:  # absolute loop guard
                break
            skipped_any = False
            for s in report.steps:
                if s.status == PENDING and any(d in blocked for d in s.depends_on):
                    s.status = SKIPPED
                    blocked.add(s.id)
                    self._emit(report, "step_skipped", {"id": s.id, "intent": s.intent}, cycle_id)
                    self._checkpoint_report(goal, report, durable_state)
                    skipped_any = True
            ready = [
                s
                for s in report.steps
                if s.status == PENDING and all(d in done for d in s.depends_on)
            ]
            if not ready:
                if skipped_any:
                    continue
                # Deadlock: the remaining steps have an unsatisfiable or cyclic
                # dependency (a dep id that never completes and was never blocked).
                # Fail them explicitly so the plan can't report a false "completed".
                for s in report.steps:
                    if s.status in (PENDING, RUNNING):
                        s.status = FAILED
                        s.output = "unsatisfiable or cyclic dependency"
                        self._emit(
                            report, "step_failed", {"id": s.id, "reason": s.output}, cycle_id
                        )
                        self._checkpoint_report(goal, report, durable_state)
                break
            step = ready[0]
            stopping_status = self._stopping_status()
            if stopping_status:
                report.status = stopping_status
                self._emit(
                    report,
                    "final",
                    {"status": report.status, "summary": report.summary()},
                    cycle_id,
                )
                return report
            executed += 1
            if executed > self.MAX_STEPS:
                step.status = FAILED
                step.output = "step budget exceeded"
                self._emit(report, "step_failed", {"id": step.id, "reason": step.output}, cycle_id)
                blocked.add(step.id)
                self._checkpoint_report(goal, report, durable_state)
                continue
            outcome = self._run_step(step, report, cycle_id, goal, durable_state)
            if outcome == DONE:
                done.add(step.id)
            elif outcome == HELD:
                blocked.add(step.id)  # dependents can't run on an un-executed action
                held_step = step
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
                        self._emit(
                            report,
                            "replan",
                            {"after": step.id, "new": [[s.id, s.tool] for s in new_steps]},
                            cycle_id,
                        )
                        self._checkpoint_report(goal, report, durable_state)
                        continue
                blocked.add(step.id)

        report.status = self._final_status(report)
        self._emit(
            report, "final", {"status": report.status, "summary": report.summary()}, cycle_id
        )
        self._checkpoint_report(goal, report, durable_state)
        if report.status == "needs_approval" and held_step is not None:
            self._interrupt_for_approval(held_step, durable_state)
        self._finalize_run(report)
        return report

    # ----------------------------------------------------------------- helpers
    def _run_step(
        self,
        step: PlanStep,
        report: ExecutionReport,
        cycle_id: str,
        goal: str,
        durable_state: dict[str, Any],
    ) -> str:
        step.status = RUNNING
        tool = self.registry.get(step.tool)
        if tool is None:
            step.status, step.output = FAILED, f"unknown tool '{step.tool}'"
            self._emit(
                report,
                "step_failed",
                {"id": step.id, "tool": step.tool, "reason": step.output},
                cycle_id,
            )
            self._checkpoint_report(goal, report, durable_state)
            return FAILED
        try:
            validate_tool_args(tool, step.args)
        except ValidationError as exc:
            step.status, step.output = FAILED, f"schema: {exc}"
            self._emit(
                report,
                "step_failed",
                {"id": step.id, "tool": step.tool, "reason": step.output},
                cycle_id,
            )
            self._checkpoint_report(goal, report, durable_state)
            return FAILED
        decision = self.broker.authorize(
            actor=self.actor,
            tool=step.tool,
            risk=tool.risk,
            args=step.args,
            preview=f"{step.tool}({step.args})",
            provenance="plan",
            cycle_id=cycle_id,
            rationale=f"Plan step: {step.intent}",
            organization_id=(self.organization_id if self.checkpoints is not None else ""),
        )
        if decision.verdict is Verdict.ALLOW:
            if not self._prepare_consequential_allow(step, tool, report, goal, durable_state):
                return FAILED
            if tool.risk.value not in {"send", "destructive"}:
                self._checkpoint_report(goal, report, durable_state)
            try:
                step.output = str(tool.run(**step.args))
            except Exception as exc:  # a tool failure is a step failure, not a crash
                step.status, step.output = FAILED, f"ERROR: {exc}"
                self._emit(
                    report,
                    "step_failed",
                    {"id": step.id, "tool": step.tool, "reason": step.output},
                    cycle_id,
                )
                self._checkpoint_report(goal, report, durable_state)
                return FAILED
            self._record_effect_receipt(step, tool, durable_state)
            # Untrusted tool output: taint injection-flagged spans for egress.
            if self.broker.is_injection(step.output):
                self.broker.mark_tainted(step.output)
            step.status = DONE
            self._emit(
                report,
                "step_done",
                {
                    "id": step.id,
                    "intent": step.intent,
                    "tool": step.tool,
                    "preview": self.broker.redact(step.output)[:200],
                },
                cycle_id,
            )
            self._checkpoint_report(goal, report, durable_state)
            return DONE
        if decision.verdict is Verdict.NEEDS_APPROVAL:
            step.status, step.approval_id = HELD, decision.approval_id or ""
            self._enqueue_approval_outbox(step, tool, durable_state)
            self._emit(
                report,
                "step_held",
                {
                    "id": step.id,
                    "intent": step.intent,
                    "tool": step.tool,
                    "approval_id": step.approval_id,
                },
                cycle_id,
            )
            self._checkpoint_report(goal, report, durable_state)
            return HELD
        step.status, step.output = DENIED, self.broker.redact(decision.reason)
        self._emit(
            report,
            "step_denied",
            {"id": step.id, "tool": step.tool, "reason": step.output},
            cycle_id,
        )
        self._checkpoint_report(goal, report, durable_state)
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
        if statuses & {PENDING, RUNNING}:  # a step never reached a terminal state
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
        if self.on_event is not None:
            try:
                self.on_event(etype, data)
            except Exception:  # UI/trace plumbing must never break execution
                pass
    def _initial_steps(self, goal: str, steps: "list[PlanStep] | None") -> list[PlanStep]:
        if self.checkpoints is not None:
            latest = self.checkpoints.latest(self.organization_id, self.workspace_id, self.run_id)
            if latest is not None:
                persisted_steps = latest.state.get("steps")
                if isinstance(persisted_steps, list) and (
                    persisted_steps or "status" in latest.state
                ):
                    restored = [self._deserialize_step(item) for item in persisted_steps]
                    self._reconcile_restored_steps(restored, latest.state)
                    return restored
        if steps is not None:
            return steps
        return to_plan_steps(self.planner.plan(goal).steps)

    def _reconcile_restored_steps(self, steps: list[PlanStep], state: dict[str, Any]) -> None:
        outbox = {
            item.get("step_id"): item
            for item in state.get("outbox", [])
            if isinstance(item, dict) and isinstance(item.get("step_id"), str)
        }
        approval_store = self.store or getattr(self.broker, "store", None)
        for step in steps:
            entry = outbox.get(step.id, {})
            approval = None
            if step.approval_id and approval_store is not None:
                approval = approval_store.get_approval(step.approval_id)
            approved_action = bool(
                approval
                and approval.get("status") == "approved"
                and approval.get("organization_id") == self.organization_id
                and approval.get("tool") == step.tool
                and approval.get("args") == step.args
            )
            if step.status == HELD and approved_action:
                self.broker.allow_tool_once(step.tool)
                step.status = PENDING
            elif (
                step.status == HELD
                and approval
                and approval.get("status") in {"rejected", "expired"}
            ):
                step.status = DENIED
                step.output = f"approval {approval['status']}"
            elif step.status == HELD and approval and approval.get("status") == "approved":
                step.status = FAILED
                step.output = "approved action no longer matches persisted step"
            if step.status == PENDING and entry.get("status") == "pending_approval":
                pending_tool = self.registry.get(step.tool)
                pending_effect_type = (
                    step.tool
                    if pending_tool is None
                    else (getattr(pending_tool, "effect_type", "") or step.tool)
                )
                if (
                    approved_action
                    and entry.get("tool") == step.tool
                    and entry.get("effect_type") == pending_effect_type
                    and entry.get("args") == step.args
                ):
                    self.broker.allow_tool_once(step.tool)
            if step.status != RUNNING:
                continue
            tool = self.registry.get(step.tool)
            effect_type = (
                step.tool if tool is None else (getattr(tool, "effect_type", "") or step.tool)
            )
            entry_matches = bool(
                entry
                and entry.get("tool") == step.tool
                and entry.get("effect_type") == effect_type
                and entry.get("args") == step.args
            )
            idempotency_key = entry.get("idempotency_key")
            if isinstance(idempotency_key, str) and idempotency_key:
                assert self.checkpoints is not None
                receipt = self.checkpoints.get_effect(
                    self.organization_id, self.workspace_id, self.run_id, idempotency_key
                )
                if receipt is not None:
                    expected_fingerprint = CheckpointRegistry.effect_fingerprint(
                        effect_type, step.args
                    )
                    if not entry_matches or receipt.fingerprint != expected_fingerprint:
                        step.status = FAILED
                        step.output = "idempotency key conflicts with the persisted action"
                    else:
                        step.status = DONE
                        step.output = str(receipt.result.get("output", ""))
                    continue
            entry_status = entry.get("status")
            if entry_status == "pending_approval":
                if approved_action and entry_matches:
                    self.broker.allow_tool_once(step.tool)
                    step.status = PENDING
                elif approval and approval.get("status") in {"rejected", "expired"}:
                    step.status = DENIED
                    step.output = f"approval {approval['status']}"
                elif approval and approval.get("status") == "approved":
                    step.status = FAILED
                    step.output = "approved action no longer matches persisted step"
                else:
                    step.status = HELD
                continue
            provider_retry = (
                entry_status == "pending_execution"
                and entry_matches
                and entry.get("requires_provider_idempotency") is True
                and isinstance(idempotency_key, str)
                and bool(idempotency_key)
            )
            if provider_retry and approved_action:
                self.broker.allow_tool_once(step.tool)
                step.status = PENDING
            elif provider_retry:
                step.status = FAILED
                step.output = "manual reconciliation requires the durable approved action"
            elif tool is not None and tool.risk.value in {"read", "draft"}:
                step.status = PENDING
            elif not entry and tool is not None and tool.risk.value in {"send", "destructive"}:
                step.status = PENDING
            else:
                step.status = FAILED
                step.output = "manual reconciliation required after interrupted effect"
        status_by_id = {step.id: step.status for step in steps}
        changed = True
        while changed:
            changed = False
            for step in steps:
                if (
                    step.status == SKIPPED
                    and step.depends_on
                    and all(status_by_id.get(dep) in {DONE, PENDING} for dep in step.depends_on)
                ):
                    step.status = PENDING
                    step.output = ""
                    status_by_id[step.id] = PENDING
                    changed = True

    def _load_durable_state(self, goal: str, report: ExecutionReport) -> dict[str, Any]:
        state: dict[str, Any] = {
            "goal": goal,
            "outbox": [],
            "effect_receipts": [],
            "delivery_semantics": {},
            "replans": 0,
        }
        if self.checkpoints is None:
            return state
        latest = self.checkpoints.latest(self.organization_id, self.workspace_id, self.run_id)
        if latest is None:
            return state
        if isinstance(latest.state.get("goal"), str):
            state["goal"] = latest.state["goal"]
        outbox = latest.state.get("outbox")
        if isinstance(outbox, list):
            state["outbox"] = list(outbox)
        receipts = latest.state.get("effect_receipts")
        if isinstance(receipts, list):
            state["effect_receipts"] = list(receipts)
        semantics = latest.state.get("delivery_semantics")
        if isinstance(semantics, dict):
            state["delivery_semantics"] = dict(semantics)
        replans = latest.state.get("replans", 0)
        if type(replans) is not int or replans < 0:
            raise ValueError("persisted replan count is invalid")
        state["replans"] = replans
        steps_by_id = {step.id: step for step in report.steps}
        for item in list(state["outbox"]):
            if not isinstance(item, dict):
                continue
            key = item.get("idempotency_key")
            step_id = item.get("step_id")
            if not isinstance(key, str) or not key or not isinstance(step_id, str):
                continue
            receipt = self.checkpoints.get_effect(
                self.organization_id, self.workspace_id, self.run_id, key
            )
            if receipt is None:
                continue
            step = steps_by_id.get(step_id)
            tool = None if step is None else self.registry.get(step.tool)
            effect_type = "" if step is None else (
                step.tool if tool is None else (getattr(tool, "effect_type", "") or step.tool)
            )
            if (
                step is None
                or item.get("tool") != step.tool
                or item.get("effect_type") != effect_type
                or item.get("args") != step.args
                or receipt.fingerprint
                != CheckpointRegistry.effect_fingerprint(effect_type, step.args)
            ):
                continue
            completed = dict(item)
            completed["status"] = "completed"
            completed["receipt_id"] = receipt.receipt_id
            self._replace_outbox_entry(state, completed)
            state["effect_receipts"] = [
                entry
                for entry in state["effect_receipts"]
                if isinstance(entry, dict) and entry.get("step_id") != step_id
            ]
            state["effect_receipts"].append(
                {
                    "step_id": step_id,
                    "receipt_id": receipt.receipt_id,
                    "idempotency_key": receipt.idempotency_key,
                    "effect_type": receipt.effect_type,
                }
            )
        return state

    def _serialize_step(self, step: PlanStep) -> dict[str, Any]:
        return {
            "id": step.id,
            "intent": step.intent,
            "tool": step.tool,
            "args": dict(step.args),
            "depends_on": list(step.depends_on),
            "status": step.status,
            "output": step.output,
            "approval_id": step.approval_id,
        }

    @staticmethod
    def _deserialize_step(data: dict[str, Any]) -> PlanStep:
        if type(data) is not dict:
            raise ValueError("persisted plan step must be a JSON object")
        exact_strings = ("id", "intent", "tool", "status", "output", "approval_id")
        if any(type(data.get(name)) is not str for name in exact_strings):
            raise ValueError("persisted plan step string fields are invalid")
        args = data.get("args")
        depends_on = data.get("depends_on")
        if (
            type(args) is not dict
            or not CheckpointRegistry._is_exact_json(args)
            or type(depends_on) is not list
            or not all(type(item) is str for item in depends_on)
        ):
            raise ValueError("persisted plan step arguments or dependencies are invalid")
        status = data["status"]
        if status not in _STEP_STATUSES:
            raise ValueError(f"unknown persisted plan step status: {status}")
        return PlanStep(
            id=data["id"],
            intent=data["intent"],
            tool=data["tool"],
            args=dict(args),
            depends_on=list(depends_on),
            status=status,
            output=data["output"],
            approval_id=data["approval_id"],
        )

    def _checkpoint_report(
        self, goal: str, report: ExecutionReport, durable_state: dict[str, Any]
    ) -> None:
        if self.checkpoints is None:
            return
        run = self.checkpoints.get_run(self.organization_id, self.workspace_id, self.run_id)
        if run is None or run.status != "running":
            return
        payload = {
            "goal": durable_state.get("goal", goal),
            "steps": [self._serialize_step(step) for step in report.steps],
            "outbox": list(durable_state.get("outbox", [])),
            "effect_receipts": list(durable_state.get("effect_receipts", [])),
            "replans": report.replans,
        }
        if durable_state.get("delivery_semantics"):
            payload["delivery_semantics"] = dict(durable_state["delivery_semantics"])
        if report.status:
            payload["status"] = report.status
        self.checkpoints.checkpoint(
            self.organization_id,
            self.workspace_id,
            self.run_id,
            actor_id=self.actor_id,
            state=payload,
        )

    def _stopping_status(self) -> str:
        if self.checkpoints is None:
            return ""
        run = self.checkpoints.get_run(self.organization_id, self.workspace_id, self.run_id)
        if run is None:
            return "failed"
        if run.status == "running":
            return ""
        return {
            "interrupted": "needs_approval",
            "cancelled": "cancelled",
            "completed": "completed",
            "failed": "failed",
        }.get(run.status, "failed")

    def _interrupt_for_approval(self, step: PlanStep, durable_state: dict[str, Any]) -> None:
        if self.checkpoints is None:
            return
        run = self.checkpoints.get_run(self.organization_id, self.workspace_id, self.run_id)
        if run is None or run.status != "running":
            return
        tool = self.registry.get(step.tool)
        effect_type = step.tool if tool is None else (tool.effect_type or step.tool)
        self.checkpoints.interrupt(
            self.organization_id,
            self.workspace_id,
            self.run_id,
            actor_id=self.actor_id,
            interrupt_type="approval",
            payload={
                "approval_id": step.approval_id,
                "step_id": step.id,
                "tool": step.tool,
                "effect_type": effect_type,
            },
        )

    def _prepare_consequential_allow(
        self,
        step: PlanStep,
        tool: Any,
        report: ExecutionReport,
        goal: str,
        durable_state: dict[str, Any],
    ) -> bool:
        if self.checkpoints is None or tool.risk.value not in {"send", "destructive"}:
            return True
        idempotency_arg = getattr(tool, "idempotency_key_arg", "") or ""
        effect_type = getattr(tool, "effect_type", "") or step.tool
        if idempotency_arg:
            key = step.args.get(idempotency_arg)
            if not isinstance(key, str) or not key.strip():
                step.status = FAILED
                step.output = f"provider idempotency key required: {idempotency_arg}"
                durable_state["delivery_semantics"] = {
                    "consequential_side_effects": "provider_idempotent_at_least_once",
                    "exactly_once_network_execution": False,
                }
                self._checkpoint_report(goal, report, durable_state)
                return False
        durable_state["delivery_semantics"] = {
            "consequential_side_effects": "provider_idempotent_at_least_once",
            "exactly_once_network_execution": False,
        }
        entry = {
            "step_id": step.id,
            "intent": step.intent,
            "tool": step.tool,
            "effect_type": effect_type,
            "args": dict(step.args),
            "status": "pending_execution",
            "requires_provider_idempotency": bool(idempotency_arg),
        }
        if idempotency_arg:
            entry["idempotency_key"] = step.args[idempotency_arg]
        if step.approval_id:
            entry["approval_id"] = step.approval_id
        self._replace_outbox_entry(durable_state, entry)
        self._checkpoint_report(goal, report, durable_state)
        return True

    def _enqueue_approval_outbox(
        self, step: PlanStep, tool: Any, durable_state: dict[str, Any]
    ) -> None:
        if self.checkpoints is None:
            return
        self._replace_outbox_entry(
            durable_state,
            {
                "approval_id": step.approval_id,
                "args": dict(step.args),
                "effect_type": getattr(tool, "effect_type", "") or step.tool,
                "intent": step.intent,
                "requires_provider_idempotency": bool(
                    getattr(tool, "idempotency_key_arg", "") or ""
                ),
                "status": "pending_approval",
                "step_id": step.id,
                "tool": step.tool,
            },
        )

    def _record_effect_receipt(
        self, step: PlanStep, tool: Any, durable_state: dict[str, Any]
    ) -> None:
        if self.checkpoints is None or tool.risk.value not in {"send", "destructive"}:
            return
        idempotency_arg = getattr(tool, "idempotency_key_arg", "") or ""
        if not idempotency_arg:
            return
        receipt, _ = self.checkpoints.record_effect(
            self.organization_id,
            self.workspace_id,
            self.run_id,
            actor_id=self.actor_id,
            idempotency_key=step.args[idempotency_arg],
            effect_type=getattr(tool, "effect_type", "") or step.tool,
            request=dict(step.args),
            result={"output": step.output},
        )
        durable_state["effect_receipts"] = [
            entry
            for entry in durable_state.get("effect_receipts", [])
            if entry.get("step_id") != step.id
        ]
        durable_state["effect_receipts"].append(
            {
                "step_id": step.id,
                "receipt_id": receipt.receipt_id,
                "idempotency_key": receipt.idempotency_key,
                "effect_type": receipt.effect_type,
            }
        )
        completed_entry = {
            "step_id": step.id,
            "intent": step.intent,
            "tool": step.tool,
            "effect_type": receipt.effect_type,
            "args": dict(step.args),
            "status": "completed",
            "requires_provider_idempotency": True,
            "idempotency_key": receipt.idempotency_key,
            "receipt_id": receipt.receipt_id,
        }
        if step.approval_id:
            completed_entry["approval_id"] = step.approval_id
        self._replace_outbox_entry(durable_state, completed_entry)

    @staticmethod
    def _replace_outbox_entry(durable_state: dict[str, Any], entry: dict[str, Any]) -> None:
        outbox = [
            item
            for item in durable_state.get("outbox", [])
            if item.get("step_id") != entry.get("step_id")
        ]
        outbox.append(entry)
        durable_state["outbox"] = outbox

    def _finalize_run(self, report: ExecutionReport) -> None:
        if self.checkpoints is None:
            return
        run = self.checkpoints.get_run(self.organization_id, self.workspace_id, self.run_id)
        if run is None or run.status != "running":
            return
        if report.status == "completed":
            self.checkpoints.complete(
                self.organization_id, self.workspace_id, self.run_id, actor_id=self.actor_id
            )
        elif report.status in {"failed", "partial"}:
            self.checkpoints.fail(
                self.organization_id,
                self.workspace_id,
                self.run_id,
                actor_id=self.actor_id,
                reason=report.status,
            )
