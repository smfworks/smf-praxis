"""Adaptive cascade inference — try the routed (cheaper) tier first and escalate
to the strong tier only when the first answer is rejected *and* the budget allows.

This is the runtime counterpart to a-priori difficulty routing: instead of guessing
up front that a goal is hard, we measure the cheap attempt and spend more compute
*only* when it falls short — modern "hybrid inference", kept under the governance
spine's budget. The primitive is deliberately provider- and verifier-agnostic: the
caller supplies a ``solve(difficulty)`` and an ``accept(answer)`` predicate, so the
same cascade works for grounded Q&A (escalate an abstaining answer), a planner, or
any other completion site.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .router import HARD

# solve(difficulty: str | None) -> answer text. None routes at the auto/cheap tier;
# HARD forces the strongest configured tier.
SolveFn = Callable[["str | None"], str]
# accept(answer) -> True when the answer is good enough (no escalation needed).
AcceptFn = Callable[[str], bool]


@dataclass
class CascadeResult:
    answer: str
    escalated: bool = False
    tier: str = "routed"          # "routed" | "strong"
    reason: str = "accepted"      # accepted | escalated | budget | unverified
    passes: int = 1


class AdaptiveCascade:
    """Cheap-first, escalate-on-low-confidence, budget-gated completion.

    ``can_escalate`` is an optional gate (e.g. a budget check); when it returns
    False the cheap answer is kept rather than spending more — cost control wins.
    """

    def __init__(self, can_escalate: "Callable[[], bool] | None" = None) -> None:
        self._can_escalate = can_escalate

    def run(self, solve: SolveFn, accept: AcceptFn) -> CascadeResult:
        first = solve(None)                        # routed (auto difficulty) tier
        if accept(first):
            return CascadeResult(first, reason="accepted")
        if self._can_escalate is not None and not self._can_escalate():
            return CascadeResult(first, reason="budget")
        second = solve(HARD)                       # force the strong tier
        if accept(second):
            return CascadeResult(second, escalated=True, tier="strong",
                                 reason="escalated", passes=2)
        # Still not accepted — keep the higher-effort attempt and say so.
        return CascadeResult(second, escalated=True, tier="strong",
                             reason="unverified", passes=2)
