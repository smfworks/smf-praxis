"""Deterministic authority, freshness, and applicability filtering."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorityPolicy:
    vertical: str
    jurisdiction: str
    accepted_tiers: tuple[str, ...]
    max_age_days: int
    population: str = ""

    def __post_init__(self) -> None:
        if not self.vertical or not self.jurisdiction or not self.accepted_tiers:
            raise ValueError("vertical, jurisdiction, and authority tiers are required")
        if self.max_age_days < 0:
            raise ValueError("maximum age cannot be negative")


@dataclass(frozen=True)
class AuthorityCandidate:
    source_id: str
    authority_tier: str
    jurisdiction: str
    age_days: int
    similarity: float
    population: str = ""
    retracted: bool = False
    superseded: bool = False


@dataclass(frozen=True)
class AuthorityDecision:
    candidate: AuthorityCandidate
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityResult:
    accepted: tuple[AuthorityDecision, ...]
    rejected: tuple[AuthorityDecision, ...]


def filter_authority(policy: AuthorityPolicy,
                     candidates: list[AuthorityCandidate]) -> AuthorityResult:
    """Filter applicability first, then rank accepted sources by authority/similarity."""
    accepted: list[AuthorityDecision] = []
    rejected: list[AuthorityDecision] = []
    for candidate in candidates:
        reasons: list[str] = []
        if candidate.retracted:
            reasons.append("retracted")
        if candidate.superseded:
            reasons.append("superseded")
        if candidate.jurisdiction != policy.jurisdiction:
            reasons.append("jurisdiction_mismatch")
        if candidate.authority_tier not in policy.accepted_tiers:
            reasons.append("authority_tier_rejected")
        if candidate.age_days > policy.max_age_days:
            reasons.append("stale")
        if policy.population and candidate.population != policy.population:
            reasons.append("population_mismatch")
        decision = AuthorityDecision(
            candidate=candidate, accepted=not reasons,
            reasons=tuple(reasons) if reasons else ("applicable",))
        (accepted if decision.accepted else rejected).append(decision)
    tier_rank = {tier: index for index, tier in enumerate(policy.accepted_tiers)}
    accepted.sort(key=lambda item: (
        tier_rank[item.candidate.authority_tier], -item.candidate.similarity,
        item.candidate.source_id))
    rejected.sort(key=lambda item: item.candidate.source_id)
    return AuthorityResult(tuple(accepted), tuple(rejected))
