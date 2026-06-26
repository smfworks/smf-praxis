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

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .logging_util import get_logger

if TYPE_CHECKING:
    from .persistence import Store


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
    decision_id: str = ""
    policy_rule: str = ""


# Multiple injection patterns. Single-pattern regexes are easily paraphrased
# around; this set covers common jailbreak shapes (instruction overrides,
# role-swaps, exfil prompts, system-prompt extraction, encoded delimiters) so
# that retrieved content carrying any of them gets flagged as data, not policy.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ignore (all |the )?(previous |prior )?(instructions|prompt|rules)",
        r"disregard (the |all )?(system|policy|previous|prior)",
        r"do not tell (?:anyone|the user|michael)",
        r"send this (file|message|note) to (everyone|all|the team)",
        r"delete the (original|email|file|record)",
        r"approve (this|the) (request|action) (immediately|silently|without)",
        r"reveal (your )?(system )?(prompt|instructions|context)",
        r"override (the )?(safety|policy|approval)",
        r"you are now (a |an )?[a-z]+ (assistant|agent|model) (?:that|who)",
        r"switch (to )?(developer|debug|jailbreak|admin) mode",
        r"<\|.*?system.*?\|>|<system>",
        r"begin (a |an )?new (system|policy) prompt",
        r"forget (everything|prior|previous|the )?",
    )
]

_SECRET_RE = re.compile(r"(?i)(api[_-]?key|password|token|secret)\s*[:=]\s*\S+")


@dataclass
class AuditEntry:
    actor: str
    tool: str
    risk: str
    verdict: str
    detail: str
    decision_id: str = ""
    cycle_id: str = ""
    policy_rule: str = ""
    approval_id: str = ""
    args_hash: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class PendingApproval:
    approval_id: str
    tool: str
    args: dict
    preview: str
    provenance: str
    cycle_id: str = ""
    decision_id: str = ""
    rationale: str = ""
    evidence: list[dict] = field(default_factory=list)
    expires_at: float | None = None
    required_approvals: int = 1
    approvals: list[dict] = field(default_factory=list)

    @property
    def fully_approved(self) -> bool:
        return len(self.approvals) >= self.required_approvals


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
    approval_ttl_seconds: float | None = 3600.0
    # Risk classes that run autonomously (no human approval). Per the framework
    # principle "autonomy for preparation, approval for consequence", both READ
    # and DRAFT are autonomous: a draft only prepares content and never sends.
    # SEND/DESTRUCTIVE are always held for approval. Mirrors the AUTONOMOUS set.
    autonomous_risks: set[RiskClass] = field(default_factory=lambda: {RiskClass.READ, RiskClass.DRAFT})
    # Risk classes that require two distinct approvers (four-eyes principle).
    dual_approval_risks: set[RiskClass] = field(
        default_factory=lambda: {RiskClass.DESTRUCTIVE})


class GovernanceBroker:
    def __init__(self, policy: GovernancePolicy | None = None,
                 store: "Store | None" = None) -> None:
        self.policy = policy or GovernancePolicy()
        self.kill = KillSwitch()
        self.audit: list[AuditEntry] = []
        self.pending: dict[str, PendingApproval] = {}
        self.store = store
        self.log = get_logger("praxis.broker")
        if store is not None:
            self._hydrate(store)

    def _hydrate(self, store: "Store") -> None:
        for row in store.list_approvals():
            self.pending[row["approval_id"]] = PendingApproval(
                approval_id=row["approval_id"], tool=row["tool"],
                args=row["args"], preview=row["preview"],
                provenance=row["provenance"], cycle_id=row.get("cycle_id", ""),
                decision_id=row.get("decision_id", ""),
                rationale=row.get("rationale", ""), evidence=row.get("evidence", []),
                expires_at=row["expires_at"],
                required_approvals=row.get("required_approvals", 1),
                approvals=row.get("signatures", []))
        for row in store.load_audit():
            self.audit.append(AuditEntry(
                actor=row["actor"], tool=row["tool"], risk=row["risk"],
                verdict=row["verdict"], detail=row["detail"],
                decision_id=row.get("decision_id", ""),
                cycle_id=row.get("cycle_id", ""),
                policy_rule=row.get("policy_rule", ""),
                approval_id=row.get("approval_id", ""),
                args_hash=row.get("args_hash", ""), ts=row["ts"]))

    # ---------------------------------------------------------- authorization
    def authorize(self, actor: str, tool: str, risk: RiskClass, args: dict,
                  preview: str = "", provenance: str = "agent",
                  cycle_id: str = "", evidence: list[dict] | None = None,
                  rationale: str = "") -> Decision:
        decision_id = f"dec-{uuid.uuid4().hex[:12]}"
        args_hash = self._hash_args(args)
        if tool not in self.policy.allowed_tools:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      "tool not in allowlist", decision_id=decision_id,
                                      cycle_id=cycle_id, policy_rule="allowlist_denied",
                                      args_hash=args_hash)
        if risk in CONSEQUENTIAL and self.kill.tripped:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      "kill-switch engaged", decision_id=decision_id,
                                      cycle_id=cycle_id, policy_rule="kill_switch_denied",
                                      args_hash=args_hash)
        if risk in self.policy.autonomous_risks:
            return self._log_decision(actor, tool, risk, Verdict.ALLOW,
                                      "autonomous (read/draft)", decision_id=decision_id,
                                      cycle_id=cycle_id, policy_rule="autonomous_allow",
                                      args_hash=args_hash)
        # Consequential -> hold for human approval (draft-before-send).
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        ttl = self.policy.approval_ttl_seconds
        expires_at = time.time() + ttl if ttl else None
        required = 2 if risk in self.policy.dual_approval_risks else 1
        why = rationale or (
            f"{risk.value} tool '{tool}' is consequential and requires "
            f"{required} human approval(s)."
        )
        self.pending[approval_id] = PendingApproval(
            approval_id=approval_id, tool=tool, args=args,
            preview=preview, provenance=provenance, cycle_id=cycle_id,
            decision_id=decision_id, rationale=why, evidence=evidence or [],
            expires_at=expires_at, required_approvals=required,
        )
        if self.store is not None:
            self.store.upsert_approval(approval_id, tool, args, preview,
                                       provenance, expires_at, cycle_id=cycle_id,
                                       decision_id=decision_id, rationale=why,
                                       evidence=evidence,
                                       required_approvals=required)
        return self._log_decision(actor, tool, risk, Verdict.NEEDS_APPROVAL,
                                  f"queued {approval_id} (needs {required})",
                                  approval_id,
                                  decision_id=decision_id, cycle_id=cycle_id,
                                  policy_rule=("dual_approval_required"
                                               if required > 1
                                               else "human_approval_required"),
                                  args_hash=args_hash)

    def approve(self, approval_id: str, approved_by: str = "",
                approval_notes: str = "") -> PendingApproval | None:
        pending = self.pending.get(approval_id)
        if pending is None:
            return None
        if pending.expires_at and pending.expires_at < time.time():
            self.pending.pop(approval_id, None)
            if self.store is not None:
                self.store.resolve_approval(approval_id, "expired")
            self.log.info("approval %s expired", approval_id)
            return None
        # Four-eyes principle: an approver who already signed THIS approval
        # cannot sign it a second time to satisfy the dual-approval requirement.
        if approved_by and any(a.get("approved_by") == approved_by
                               for a in pending.approvals):
            self.log.info("approval %s: %s already signed; second approver required",
                          approval_id, approved_by)
            return None
        pending.approvals.append({
            "approved_by": approved_by, "notes": approval_notes,
            "ts": time.time(),
        })
        if self.store is not None:
            self.store.add_approval_signature(
                approval_id, approved_by, approval_notes)
        if not pending.fully_approved:
            self.log.info("approval %s: %d/%d signatures collected",
                          approval_id, len(pending.approvals),
                          pending.required_approvals)
            return None
        # All signatures collected -> atomically claim execution.
        if self.store is not None and not self.store.resolve_approval(
                approval_id, "approved", approved_by=approved_by,
                approval_notes=approval_notes):
            self.pending.pop(approval_id, None)        # already resolved elsewhere
            self.log.info("approval %s already resolved; refusing", approval_id)
            return None
        self.pending.pop(approval_id, None)
        return pending

    def reject(self, approval_id: str) -> None:
        self.pending.pop(approval_id, None)
        if self.store is not None:
            self.store.resolve_approval(approval_id, "rejected")

    # ------------------------------------------------------------- screening
    def is_injection(self, text: str) -> bool:
        if not (self.policy.injection_check and text):
            return False
        return any(pat.search(text) for pat in _INJECTION_PATTERNS)

    @staticmethod
    def redact(text: str) -> str:
        return _SECRET_RE.sub(r"\1: [REDACTED]", text or "")

    @staticmethod
    def _hash_args(args: dict) -> str:
        blob = json.dumps(args or {}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    # ----------------------------------------------------------------- audit
    def _log_decision(self, actor: str, tool: str, risk: RiskClass,
                      verdict: Verdict, reason: str,
                      approval_id: str | None = None,
                      decision_id: str = "", cycle_id: str = "",
                      policy_rule: str = "", args_hash: str = "") -> Decision:
        entry = AuditEntry(actor=actor, tool=tool, risk=risk.value,
                           verdict=verdict.value, detail=self.redact(reason),
                           decision_id=decision_id, cycle_id=cycle_id,
                           policy_rule=policy_rule,
                           approval_id=approval_id or "", args_hash=args_hash)
        self.audit.append(entry)
        if self.store is not None:
            self.store.add_audit(entry.actor, entry.tool, entry.risk,
                                 entry.verdict, entry.detail, entry.ts,
                                 decision_id=entry.decision_id,
                                 cycle_id=entry.cycle_id,
                                 policy_rule=entry.policy_rule,
                                 approval_id=entry.approval_id,
                                 args_hash=entry.args_hash)
        self.log.debug("decision actor=%s tool=%s risk=%s verdict=%s",
                       actor, tool, risk.value, verdict.value)
        return Decision(verdict=verdict, reason=reason, approval_id=approval_id,
                        decision_id=decision_id, policy_rule=policy_rule)
