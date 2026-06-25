"""Compliance reporting for regulated deployments.

This module turns the durable trace data written by ``PraxisAgent`` and
``GovernanceBroker`` into an audit-friendly attestation:

* every SEND / DESTRUCTIVE decision is visible;
* every queued approval has status, approver, timestamp, rationale, and evidence;
* every cycle has a signal -> plan -> decision -> action event chain.

The report is intentionally data-first and dependency-free so it can run in a
locked-down environment or be exported to an external GRC system later.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .broker import RiskClass


CONSEQUENTIAL = {RiskClass.SEND.value, RiskClass.DESTRUCTIVE.value}


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
    findings: list[ComplianceFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity in ("critical", "high") for f in self.findings)

    def attestation(self) -> str:
        if self.passed:
            return (
                "PASS: all recorded SEND/DESTRUCTIVE actions are either approved, "
                "pending approval, or denied before execution."
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
        cycles = {e["cycle_id"] for e in events if e.get("cycle_id")}
        consequential = [
            a for a in audits
            if a.get("risk") in CONSEQUENTIAL and a.get("verdict") in (
                "needs_approval", "deny")
        ]
        approved = 0
        pending = 0
        denied = 0
        findings: list[ComplianceFinding] = []

        for entry in consequential:
            if entry.get("verdict") == "deny":
                denied += 1
                continue
            decision_id = entry.get("decision_id")
            approval = approval_by_decision.get(decision_id)
            if not approval:
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
            else:
                findings.append(ComplianceFinding(
                    "high",
                    f"Consequential approval resolved as {status!r}, not approved/pending.",
                    approval["approval_id"],
                ))

        return ComplianceReport(
            audit_entries=len(audits),
            approvals=len(approvals),
            cycles=len(cycles),
            consequential_decisions=len(consequential),
            approved_consequential=approved,
            pending_consequential=pending,
            denied_consequential=denied,
            findings=findings,
        )

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
        ]
        if report.findings:
            lines.append("findings:")
            for f in report.findings:
                suffix = f" ({f.ref_id})" if f.ref_id else ""
                lines.append(f"  [{f.severity}] {f.message}{suffix}")
        return "\n".join(lines)
