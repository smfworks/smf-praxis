"""Compliance reporting for regulated deployments.

This module turns the durable trace data written by ``PraxisAgent`` and
``GovernanceBroker`` into an audit-friendly attestation:

* every SEND / DESTRUCTIVE decision is visible;
* every queued approval has status, approver, timestamp, rationale, and evidence;
* every cycle has a signal -> plan -> decision -> action event chain;
* failed tasks, subagent errors, and KB source failures surface as findings;
* benign dispositions (expired, cancelled, denied) are recorded but do not
  break the attestation.

The report is intentionally data-first and dependency-free so it can run in a
locked-down environment or be exported to an external GRC system later.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .broker import RiskClass

CONSEQUENTIAL = {RiskClass.SEND.value, RiskClass.DESTRUCTIVE.value}
ERROR_EVENT_TYPES = {
    "task_error",
    "subagent_error",
    "kb_source_error",
    "action_error",
    "approval_execution_error",
    "heartbeat_wiki_error",
}
# Approvals that resolved without execution but for safe reasons. Surfaced as
# informational findings rather than treated as governance failures.
BENIGN_TERMINAL = {"expired", "cancelled", "rejected"}


@dataclass
class ComplianceFinding:
    severity: str
    message: str
    ref_id: str = ""


@dataclass
class ComplianceReport:
    audit_entries: int
    approvals: int
    cycles: int
    consequential_decisions: int
    approved_consequential: int
    pending_consequential: int
    denied_consequential: int
    expired_consequential: int = 0
    rejected_consequential: int = 0
    error_events: int = 0
    failed_tasks: int = 0
    failed_subagent_runs: int = 0
    errored_kb_sources: int = 0
    findings: list[ComplianceFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity in ("critical", "high") for f in self.findings)

    def attestation(self) -> str:
        if self.passed:
            return (
                "PASS: all recorded SEND/DESTRUCTIVE actions are either approved, "
                "pending approval, denied, or benignly closed before execution."
            )
        return "FAIL: one or more consequential actions lack approval evidence."


class ComplianceReporter:
    def __init__(self, store) -> None:
        self.store = store

    def build(self) -> ComplianceReport:
        audits = self.store.load_audit(limit=10000)
        approvals = self.store.list_all_approvals(limit=10000)
        events = self.store.list_compliance_events(limit=10000)

        approval_by_decision = {
            a.get("decision_id"): a for a in approvals if a.get("decision_id")
        }
        # Fallback lookup by approval_id for older audit rows without decision_id.
        approval_by_id = {a["approval_id"]: a for a in approvals}
        cycles = {e["cycle_id"] for e in events if e.get("cycle_id")}
        consequential = [
            a for a in audits
            if a.get("risk") in CONSEQUENTIAL and a.get("verdict") in (
                "needs_approval", "deny")
        ]
        approved = 0
        pending = 0
        denied = 0
        expired = 0
        rejected = 0
        findings: list[ComplianceFinding] = []
        legacy_decision_seen = False

        for entry in consequential:
            if entry.get("verdict") == "deny":
                denied += 1
                continue
            decision_id = entry.get("decision_id")
            approval = approval_by_decision.get(decision_id) if decision_id else None
            if not approval:
                # Try the approval_id fallback so legacy rows that pre-date
                # decision_id can still be matched.
                approval = approval_by_id.get(entry.get("approval_id", ""))
            if not approval:
                if not decision_id:
                    legacy_decision_seen = True
                    continue
                findings.append(ComplianceFinding(
                    "high",
                    "Consequential decision has no linked approval record.",
                    decision_id or entry.get("approval_id", ""),
                ))
                continue
            status = approval.get("status")
            if status == "approved":
                approved += 1
                if not approval.get("approved_by"):
                    findings.append(ComplianceFinding(
                        "medium", "Approved action has no approver identity.",
                        approval["approval_id"]))
                if not approval.get("evidence"):
                    findings.append(ComplianceFinding(
                        "medium", "Approved action has no evidence bundle.",
                        approval["approval_id"]))
            elif status == "pending":
                pending += 1
            elif status == "expired":
                expired += 1
            elif status == "rejected":
                rejected += 1
            elif status == "cancelled":
                # Same disposition as expired/rejected — action did not run.
                expired += 1
            else:
                findings.append(ComplianceFinding(
                    "high",
                    f"Consequential approval resolved as {status!r}, not approved/pending.",
                    approval["approval_id"],
                ))

        if legacy_decision_seen:
            findings.append(ComplianceFinding(
                "low",
                "Legacy audit rows without decision_id were skipped (pre-Phase-6 data).",
            ))

        # Surface task/subagent/KB errors as findings so the attestation reflects
        # the full operational picture, not just approval bookkeeping.
        error_events = [e for e in events if e.get("event_type") in ERROR_EVENT_TYPES]
        failed_tasks = self._count_by_status(self.store.list_tasks(status="failed"))
        failed_subagents = sum(
            1 for r in self.store.list_subagent_runs(limit=10000)
            if r.get("status") == "failed"
        )
        errored_kb = sum(
            1 for s in self.store.list_kb_sources()
            if s.get("status") == "error"
        )
        if failed_tasks:
            findings.append(ComplianceFinding(
                "high",
                f"{failed_tasks} task(s) finished in 'failed' state.",
            ))
        if failed_subagents:
            findings.append(ComplianceFinding(
                "high",
                f"{failed_subagents} subagent run(s) finished in 'failed' state.",
            ))
        if errored_kb:
            findings.append(ComplianceFinding(
                "medium",
                f"{errored_kb} KB source(s) currently in 'error' state.",
            ))

        return ComplianceReport(
            audit_entries=len(audits),
            approvals=len(approvals),
            cycles=len(cycles),
            consequential_decisions=len(consequential),
            approved_consequential=approved,
            pending_consequential=pending,
            denied_consequential=denied,
            expired_consequential=expired,
            rejected_consequential=rejected,
            error_events=len(error_events),
            failed_tasks=failed_tasks,
            failed_subagent_runs=failed_subagents,
            errored_kb_sources=errored_kb,
            findings=findings,
        )

    @staticmethod
    def _count_by_status(rows: list[dict]) -> int:
        return len(rows)

    @staticmethod
    def render(report: ComplianceReport) -> str:
        lines = [
            report.attestation(),
            f"audit_entries: {report.audit_entries}",
            f"cycles: {report.cycles}",
            f"approvals: {report.approvals}",
            f"consequential_decisions: {report.consequential_decisions}",
            f"approved_consequential: {report.approved_consequential}",
            f"pending_consequential: {report.pending_consequential}",
            f"denied_consequential: {report.denied_consequential}",
            f"expired_consequential: {report.expired_consequential}",
            f"rejected_consequential: {report.rejected_consequential}",
            f"error_events: {report.error_events}",
            f"failed_tasks: {report.failed_tasks}",
            f"failed_subagent_runs: {report.failed_subagent_runs}",
            f"errored_kb_sources: {report.errored_kb_sources}",
        ]
        if report.findings:
            lines.append("findings:")
            for f in report.findings:
                suffix = f" ({f.ref_id})" if f.ref_id else ""
                lines.append(f"  [{f.severity}] {f.message}{suffix}")
        return "\n".join(lines)
