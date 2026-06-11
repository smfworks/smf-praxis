"""Governance broker — the control plane both source guides converge on.

Eliminates OpenClaw's "permissionless local autonomy" and "prompt-injection"
weaknesses, and supplies the broker Hermes assumes:

* tool allowlist + least privilege
* risk classification: read/draft are autonomous; send/destructive need approval
* draft-before-send: consequential actions are held in an approval queue
* prompt-injection boundary: retrieved content is data, never instruction
* audit trail (attributable) + redaction
* kill-switch that disables all consequential tools
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class RiskClass(str, Enum):
    READ = "read"             # autonomous
    DRAFT = "draft"           # autonomous (never sends)
    SEND = "send"             # requires approval
    DESTRUCTIVE = "destructive"  # requires approval


AUTONOMOUS = {RiskClass.READ, RiskClass.DRAFT}
CONSEQUENTIAL = {RiskClass.SEND, RiskClass.DESTRUCTIVE}


class Verdict(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    approval_id: str | None = None


# Lines that look like embedded instructions inside retrieved content.
_INJECTION_RE = re.compile(
    r"ignore (all |the )?(previous |prior )?instructions|"
    r"do not tell|send this (file|message) to everyone|"
    r"delete the original|approve this (request )?immediately|"
    r"reveal (your )?(system )?prompt|disregard (the )?(system|policy)",
    re.IGNORECASE,
)

_SECRET_RE = re.compile(r"(?i)(api[_-]?key|password|token|secret)\s*[:=]\s*\S+")


@dataclass
class AuditEntry:
    actor: str
    tool: str
    risk: str
    verdict: str
    detail: str
    ts: float = field(default_factory=time.time)


@dataclass
class PendingApproval:
    approval_id: str
    tool: str
    args: dict
    preview: str
    provenance: str


class KillSwitch:
    def __init__(self) -> None:
        self._tripped = False

    def trip(self) -> None:
        self._tripped = True

    def reset(self) -> None:
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped


@dataclass
class GovernancePolicy:
    allowed_tools: set[str] = field(default_factory=set)
    injection_check: bool = True


class GovernanceBroker:
    def __init__(self, policy: GovernancePolicy | None = None) -> None:
        self.policy = policy or GovernancePolicy()
        self.kill = KillSwitch()
        self.audit: list[AuditEntry] = []
        self.pending: dict[str, PendingApproval] = {}

    # ---------------------------------------------------------- authorization
    def authorize(self, actor: str, tool: str, risk: RiskClass, args: dict,
                  preview: str = "", provenance: str = "agent") -> Decision:
        if tool not in self.policy.allowed_tools:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      "tool not in allowlist")
        if risk in CONSEQUENTIAL and self.kill.tripped:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      "kill-switch engaged")
        if risk in AUTONOMOUS:
            return self._log_decision(actor, tool, risk, Verdict.ALLOW,
                                      "autonomous (read/draft)")
        # Consequential -> hold for human approval (draft-before-send).
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        self.pending[approval_id] = PendingApproval(
            approval_id=approval_id, tool=tool, args=args,
            preview=preview, provenance=provenance,
        )
        return self._log_decision(actor, tool, risk, Verdict.NEEDS_APPROVAL,
                                  f"queued {approval_id}", approval_id)

    def approve(self, approval_id: str) -> PendingApproval | None:
        return self.pending.pop(approval_id, None)

    def reject(self, approval_id: str) -> None:
        self.pending.pop(approval_id, None)

    # ------------------------------------------------------------- screening
    def is_injection(self, text: str) -> bool:
        return bool(self.policy.injection_check and _INJECTION_RE.search(text or ""))

    @staticmethod
    def redact(text: str) -> str:
        return _SECRET_RE.sub(r"\1: [REDACTED]", text or "")

    # ----------------------------------------------------------------- audit
    def _log_decision(self, actor: str, tool: str, risk: RiskClass,
                      verdict: Verdict, reason: str,
                      approval_id: str | None = None) -> Decision:
        self.audit.append(AuditEntry(actor=actor, tool=tool, risk=risk.value,
                                     verdict=verdict.value,
                                     detail=self.redact(reason)))
        return Decision(verdict=verdict, reason=reason, approval_id=approval_id)
