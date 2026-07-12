"""Professional data classification, retention, export, and egress policy."""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class Classification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PRIVILEGED = "privileged"
    PHI = "phi"
    EDUCATION_RECORD = "education_record"
    EVIDENCE = "evidence"


class DataPolicyError(ValueError):
    """A requested data operation violates policy."""


@dataclass(frozen=True)
class RetentionRule:
    days: int

    def __post_init__(self) -> None:
        if self.days < 0:
            raise DataPolicyError("retention days cannot be negative")


@dataclass(frozen=True)
class DataDecision:
    allowed: bool
    reason: str


_DEFAULT_RETENTION = {
    Classification.PUBLIC: RetentionRule(365),
    Classification.INTERNAL: RetentionRule(365),
    Classification.CONFIDENTIAL: RetentionRule(365),
    Classification.PRIVILEGED: RetentionRule(2555),
    Classification.PHI: RetentionRule(2190),
    Classification.EDUCATION_RECORD: RetentionRule(1825),
    Classification.EVIDENCE: RetentionRule(2555),
}

_APPROVED_EGRESS = {
    Classification.PUBLIC: frozenset({"public_web", "approved_business_system"}),
    Classification.INTERNAL: frozenset({"approved_business_system"}),
    Classification.CONFIDENTIAL: frozenset({"approved_business_system"}),
    Classification.PRIVILEGED: frozenset({"approved_legal_system"}),
    Classification.PHI: frozenset({"approved_healthcare_system"}),
    Classification.EDUCATION_RECORD: frozenset({"approved_education_system"}),
    Classification.EVIDENCE: frozenset({"approved_evidence_system"}),
}

_SENSITIVE = frozenset({
    Classification.CONFIDENTIAL, Classification.PRIVILEGED, Classification.PHI,
    Classification.EDUCATION_RECORD, Classification.EVIDENCE,
})


class DataPolicy:
    """Deterministic deny-by-default policy for professional records."""

    def __init__(self, rules: dict[Classification, RetentionRule] | None = None) -> None:
        self.rules = dict(_DEFAULT_RETENTION)
        if rules:
            self.rules.update(rules)

    def disposition(self, classification: Classification, *, created_ts: float,
                    legal_hold: bool = False, now: float | None = None) -> str:
        if legal_hold:
            return "hold"
        current = time.time() if now is None else now
        expires = created_ts + self.rules[classification].days * 86400
        return "delete" if current >= expires else "retain"

    def authorize_delete(self, classification: Classification, *, created_ts: float,
                         legal_hold: bool = False, now: float | None = None) -> DataDecision:
        disposition = self.disposition(
            classification, created_ts=created_ts, legal_hold=legal_hold, now=now)
        if disposition == "hold":
            raise DataPolicyError("record is under legal hold")
        if disposition != "delete":
            return DataDecision(False, "retention_period_active")
        return DataDecision(True, "retention_period_expired")

    def allow_egress(self, classification: Classification, connector: str) -> bool:
        return connector in _APPROVED_EGRESS[classification]

    def export_decision(self, classification: Classification, *,
                        redacted: bool) -> DataDecision:
        if classification in _SENSITIVE and not redacted:
            return DataDecision(False, "redaction_required")
        return DataDecision(True, "export_allowed")
