"""Multi-agent debate — best-of-N self-consistency with a judge.

A single model answer is a single sample. This panel takes several **independent
solver attempts** — diversified by distinct *stances* injected into the system
prompt (concise / step-by-step / skeptical) — and a **judge** selects the best
one. The default judge is deterministic and offline-safe: it clusters the
candidates by answer similarity and picks the **largest agreeing cluster**
(majority-vote self-consistency), after filtering out candidates that fail
verification. An optional LLM judge breaks ties when there is no clear majority.

This is solver-agnostic: a ``solver(task, stance) -> answer`` callable can be a
plain governed LLM call or a full governed agent turn, so debate composes with
the rest of the spine rather than replacing it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .verifier import AnswerVerifier

# solver(task, stance_directive) -> answer text
SolverFn = Callable[[str, str], str]
# judge(task, [answers]) -> winning index
JudgeFn = Callable[[str, "list[str]"], int]

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Candidate:
    answer: str
    stance: str
    approved: bool = True  # passed verification


@dataclass
class DebateResult:
    answer: str
    candidates: list[Candidate] = field(default_factory=list)
    rationale: str = ""
    votes: int = 0  # size of the winning agreement cluster


class DebatePanel:
    """Run N stance-diverse solvers and judge the best answer."""

    DEFAULT_STANCES: tuple[str, ...] = (
        "Answer directly and concisely.",
        "Reason step by step, then state the answer.",
        "Be skeptical: check the facts and edge cases, then answer.",
    )

    def __init__(self, solver: SolverFn, *,
                 stances: "tuple[str, ...] | list[str] | None" = None,
                 verifier: AnswerVerifier | None = None,
                 judge: JudgeFn | None = None,
                 similarity: float = 0.6) -> None:
        self.solver = solver
        self.stances = tuple(stances) if stances else self.DEFAULT_STANCES
        self.verifier = verifier or AnswerVerifier()
        self.judge = judge
        self.similarity = similarity

    def _gather(self, task: str, system: str | None) -> list[Candidate]:
        cands: list[Candidate] = []
        for stance in self.stances:
            directive = (system + "\n\n" + stance) if system else stance
            try:
                answer = str(self.solver(task, directive)).strip()
            except Exception:
                answer = ""
            approved = self.verifier.verify(task, answer).approved
            cands.append(Candidate(answer=answer, stance=stance, approved=approved))
        return cands

    def debate(self, task: str, system: str | None = None) -> DebateResult:
        cands = self._gather(task, system)
        if not cands:
            return DebateResult(answer="", rationale="no solvers configured.")

        # Cluster candidates by answer similarity (greedy, in stance order).
        toksets = [_tokens(c.answer) for c in cands]
        clusters: list[list[int]] = []
        for i in range(len(cands)):
            for cluster in clusters:
                if _jaccard(toksets[i], toksets[cluster[0]]) >= self.similarity:
                    cluster.append(i)
                    break
            else:
                clusters.append([i])

        # Winning cluster: the largest agreement, ties broken by earliest stance.
        clusters.sort(key=lambda cl: (-len(cl), cl[0]))
        winning = clusters[0]
        # Representative: the first *verified* answer in the cluster, else first.
        rep_idx = next((i for i in winning if cands[i].approved), winning[0])

        # With no clear majority (all singletons), defer to an LLM judge if given.
        if self.judge is not None and len(winning) == 1 and len(cands) > 1:
            try:
                choice = int(self.judge(task, [c.answer for c in cands]))
            except Exception:
                choice = -1
            if 0 <= choice < len(cands):
                rep_idx = choice

        approved_n = sum(1 for c in cands if c.approved)
        rationale = (
            f"{len(cands)} solvers; {len(winning)} agreed on the selected answer; "
            f"{approved_n} passed verification.")
        return DebateResult(answer=cands[rep_idx].answer, candidates=cands,
                            rationale=rationale, votes=len(winning))
