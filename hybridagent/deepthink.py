"""Deep-think — difficulty-gated, multi-round deliberation for hard goals.

A single model pass is cheap but shallow. Deep-think spends more compute *only*
where it pays off: when the request is classified **hard** (or explicitly forced),
it runs a multi-solver debate, and if the solvers do not reach consensus it
deliberates again with each solver now able to see the others' attempts — true
iterative debate rather than a single round of sampling. The result is judged for
self-consistency and screened by the verifier.

It composes the reasoning frontiers already in the spine — difficulty routing
(:func:`~hybridagent.router.classify_difficulty`), best-of-N debate
(:class:`~hybridagent.debate.DebatePanel`), and answer verification
(:class:`~hybridagent.verifier.AnswerVerifier`) — into one high-effort mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .debate import Candidate, DebatePanel, SolverFn
from .router import HARD, classify_difficulty
from .verifier import AnswerVerifier


@dataclass
class DeepThinkResult:
    answer: str
    engaged: bool = True       # whether deliberation actually ran
    rounds: int = 0            # debate rounds used
    votes: int = 0             # agreement of the winning cluster in the final round
    candidates: list[Candidate] = field(default_factory=list)
    approved: bool = True      # passed the verifier
    rationale: str = ""


class DeepThink:
    """Orchestrate difficulty-gated multi-round deliberation."""

    def __init__(self, solver: SolverFn, *, rounds: int = 2,
                 panel: DebatePanel | None = None,
                 verifier: AnswerVerifier | None = None,
                 similarity: float = 0.6) -> None:
        self.solver = solver
        self.rounds = max(1, rounds)
        self.panel = panel or DebatePanel(solver, similarity=similarity)
        self.verifier = verifier or AnswerVerifier()

    def should_engage(self, goal: str, force: bool = False) -> bool:
        return force or classify_difficulty(goal) == HARD

    def solve(self, goal: str, system: str | None = None,
              force: bool = False) -> DeepThinkResult:
        """Deliberate on hard goals; otherwise take a single fast pass."""
        if not self.should_engage(goal, force):
            try:
                answer = str(self.solver(goal, system or "")).strip()
            except Exception:
                answer = ""
            return DeepThinkResult(answer=answer, engaged=False, rounds=0,
                                   rationale="single pass (not classified hard)")
        return self.think(goal, system)

    def think(self, goal: str, system: str | None = None) -> DeepThinkResult:
        result = self.panel.debate(goal, system=system)
        used = 1
        # No consensus (every candidate stands alone) -> deliberate again with
        # each solver now able to see the others' attempts.
        while used < self.rounds and result.votes <= 1 and len(result.candidates) > 1:
            perspectives = "\n".join(f"- {c.answer}" for c in result.candidates
                                     if c.answer)
            context = ((system + "\n\n") if system else "") + (
                "Other attempts at this exact question:\n" + perspectives
                + "\n\nWeigh them, resolve disagreements, and give your single best "
                "corrected answer.")
            result = self.panel.debate(goal, system=context)
            used += 1
        verdict = self.verifier.verify(goal, result.answer)
        return DeepThinkResult(
            answer=result.answer, engaged=True, rounds=used, votes=result.votes,
            candidates=result.candidates, approved=verdict.approved,
            rationale=result.rationale)
