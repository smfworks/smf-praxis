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
from typing import Generic, TypeVar

from .router import HARD

T = TypeVar("T")


@dataclass
class CascadeResult(Generic[T]):
    answer: T
    escalated: bool = False
    tier: str = "routed"          # "routed" | "strong"
    reason: str = "accepted"      # accepted | escalated | budget | unverified
    passes: int = 1


class AdaptiveCascade(Generic[T]):
    """Cheap-first, escalate-on-low-confidence, budget-gated completion.

    Generic over the answer type, so the same primitive serves grounded Q&A
    (``str``) and planning (a list of steps). ``can_escalate`` is an optional gate
    (e.g. a budget check); when it returns False the cheap answer is kept rather
    than spending more — cost control wins.
    """

    def __init__(self, can_escalate: "Callable[[], bool] | None" = None) -> None:
        self._can_escalate = can_escalate

    def run(self, solve: "Callable[[str | None], T]",
            accept: "Callable[[T], bool]") -> "CascadeResult[T]":
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
