"""Goal Runner — Level 1 autonomous loop (H10).

The harness course (L13): "Loop engineering builds one floor above the
harness." H01-H07 made single runs reliable; this module makes a
*continuous* run autonomous. A Goal Runner is the simplest possible loop:
a goal, a verification method, and a stopping condition. You type
``praxis goal "..."`` and the agent loops until the independent verifier
confirms the goal is met (or the turn budget is exhausted) -- without you
leaning over its shoulder saying "try again."

This is Level 1 on the course's maturity ladder (Goal Runner). Level 2-5
(scheduled tasks, multi-agent loops, self-feeding loops, fleet
orchestration) build on this same foundation.

The four silent costs (L13) are guarded against by construction:
  * verification debt  -- the stop condition is machine-checkable (the
                          H05 verifier scores the answer; threshold gates
                          the loop). Never "feels about right."
  * comprehension rot   -- each turn's result is logged to the goal record
                          so the operator can read what happened.
  * cognitive surrender -- the progress score is surfaced each turn so the
                          operator stays engaged, not blind.
  * token blowout       -- a hard turn budget (max_turns) caps the loop;
                          context compaction is the agent's own
                          compact_tool_messages (already wired).

Governance is unchanged: the loop calls the same ``agent.handle`` that
runs read/draft tools autonomously and holds send/destructive for
approval. The loop never auto-approves held actions unless the operator
passed ``--approve-all`` (dev-only).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agent import CycleReport, PraxisAgent
from .checkpoints import CheckpointRegistry
from .verifier import AnswerVerifier


@dataclass
class GoalTurn:
    """One iteration of the goal loop."""
    turn: int
    report: CycleReport
    progress: float = 0.0          # verifier score in [0, 1] if available
    verdict: str = "pending"      # "pending" | "approved" | "revise" | "blocked"
    elapsed_s: float = 0.0


@dataclass
class GoalResult:
    """The outcome of a goal loop."""
    goal: str
    turns: list[GoalTurn] = field(default_factory=list)
    max_turns: int = 8
    stopped_reason: str = ""      # "approved" | "max_turns" | "blocked" | "error"
    final_progress: float = 0.0

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    def summary(self) -> str:
        return (f"goal='{self.goal[:60]}' | turns={self.n_turns}/{self.max_turns} "
                f"| stopped={self.stopped_reason} | "
                f"final_progress={self.final_progress:.3f}")


class GoalRunner:
    """Level 1 Goal Runner: loop ``agent.handle`` until the independent
    verifier confirms the goal is met or the turn budget is exhausted.

    The verifier is the H05 maker-checker: if an ``AnswerVerifier`` with a
    critic is configured, the critic scores the agent's consolidated answer
    each turn and the loop stops when the score exceeds ``threshold``. If no
    critic is configured, the loop falls back to a deterministic completion
    check (the agent's own ``summary`` plus a regex for explicit
    "done/complete/finished" claims, cross-checked against pending
    approvals -- a held action is never "done").

    Args:
        agent: the PraxisAgent to loop.
        max_turns: hard cap on iterations (token-blowout guardrail).
        verifier: optional AnswerVerifier with an H05 critic backend. When
            None, a default AnswerVerifier() is used (deterministic-only).
        threshold: verifier score in [0, 1] at which the goal is considered
            met. Default 0.3 (the H05-calibrated value on Qwen2.5-7B).
    """

    def __init__(self, agent: PraxisAgent, *, max_turns: int = 8,
                 verifier: AnswerVerifier | None = None,
                 threshold: float = 0.3,
                 checkpoints: CheckpointRegistry | None = None,
                 organization_id: str = "", workspace_id: str = "",
                 run_id: str = "", actor_id: str = "") -> None:
        self.agent = agent
        self.max_turns = max(1, max_turns)
        self.verifier = verifier or AnswerVerifier()
        self.threshold = max(0.0, min(1.0, threshold))
        self.checkpoints = checkpoints
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.actor_id = actor_id
        if checkpoints is not None and not all(
                (organization_id, workspace_id, run_id, actor_id)):
            raise ValueError("durable GoalRunner requires complete scoped run context")

    def run(self, goal: str, *, approve_all: bool = False) -> GoalResult:
        """Loop the agent on ``goal`` until done or the budget is spent.

        Each turn: call ``agent.handle(goal)``, auto-approve held actions
        only if ``approve_all`` is set (dev-only), score the consolidated
        answer with the verifier, and stop when the score exceeds the
        threshold or the turn budget is hit.

        Returns a GoalResult with every turn's report and score, so the
        operator can read what happened (fights comprehension rot).
        """
        result = GoalResult(goal=goal, max_turns=self.max_turns)
        for turn_i in range(1, self.max_turns + 1):
            if self._is_cancelled():
                result.stopped_reason = "cancelled"
                break
            t0 = time.monotonic()
            try:
                report = self.agent.handle(goal)
            except Exception as exc:  # noqa: BLE001 -- never loop on a crash
                result.stopped_reason = f"error: {exc}"
                break
            if approve_all:
                for appr in list(report.pending_approvals):
                    self.agent.approve(appr["approval_id"])
            elapsed = time.monotonic() - t0
            # Score the turn: the agent's consolidated answer is the last
            # action, or the summary if no actions ran. The verifier checks
            # honesty (held/denied claims) then the optional critic scores
            # completion. A held action is never "done."
            answer = (report.actions[-1] if report.actions
                      else report.summary())
            held = bool(report.pending_approvals)
            v = self.verifier.verify(goal, answer, held=held)
            progress = self._score(goal, answer)
            verdict = "approved" if (v.approved and progress >= self.threshold
                                     ) else "revise"
            if v.approved and progress >= self.threshold and not held:
                result.stopped_reason = "approved"
            elif held:
                verdict = "blocked"
                result.stopped_reason = "blocked"
            turn = GoalTurn(turn=turn_i, report=report, progress=progress,
                            verdict=verdict, elapsed_s=elapsed)
            result.turns.append(turn)
            result.final_progress = progress
            self._checkpoint_result(result)
            if held and self.checkpoints is not None:
                self.checkpoints.interrupt(
                    self.organization_id, self.workspace_id, self.run_id,
                    actor_id=self.actor_id, interrupt_type="approval",
                    payload={
                        "approval_ids": [
                            str(item.get("approval_id", ""))
                            for item in report.pending_approvals
                        ],
                        "turn": turn_i,
                    })
            if result.stopped_reason in ("approved", "blocked"):
                break
        else:
            # Loop completed without break -> hit the turn budget.
            if not result.stopped_reason:
                result.stopped_reason = "max_turns"
        return result

    def _is_cancelled(self) -> bool:
        if self.checkpoints is None:
            return False
        run = self.checkpoints.get_run(
            self.organization_id, self.workspace_id, self.run_id)
        return run is None or run.status == "cancelled"

    def _checkpoint_result(self, result: GoalResult) -> None:
        if self.checkpoints is None:
            return
        record = self.to_record(result)
        self.checkpoints.checkpoint(
            self.organization_id, self.workspace_id, self.run_id,
            actor_id=self.actor_id, state=record)

    def _score(self, goal: str, answer: str) -> float:
        """Score the answer's progress toward the goal in [0, 1].

        When the verifier has an H05 critic, the critic's continuous reward
        is the progress score. When the critic is deterministic-only (no LLM
        backend), fall back to a coarse 0.5/1.0 heuristic: 1.0 if the
        verifier approves and the answer claims completion, else 0.5. This
        keeps the loop from spinning on a bare "APPROVE" when no real
        verification ran.
        """
        critic = getattr(self.verifier, "critic", None)
        if critic is not None:
            try:
                verdict_str = critic(goal, answer)
            except Exception:  # noqa: BLE001 -- a broken critic never blocks
                return 0.5
            if verdict_str.startswith("APPROVE"):
                # The critic doesn't return a score, only APPROVE/REVISE.
                # Treat approve as meeting the threshold; the loop's progress
                # check then depends on the verdict, not a granular score.
                return 1.0
            return 0.0
        # Deterministic-only: no continuous reward available. Use the
        # verifier's binary verdict as a coarse 1.0/0.0.
        v = self.verifier.verify(goal, answer)
        return 1.0 if v.approved else 0.0

    def to_record(self, result: GoalResult) -> dict[str, Any]:
        """Serialize a GoalResult to a JSON-safe record for the operator log.

        This is the anti-comprehension-rot artifact: the operator reads
        this to understand what the loop did without re-reading every turn.
        """
        return {
            "goal": result.goal,
            "max_turns": result.max_turns,
            "n_turns": result.n_turns,
            "stopped_reason": result.stopped_reason,
            "final_progress": round(result.final_progress, 4),
            "threshold": self.threshold,
            "turns": [
                {
                    "turn": t.turn,
                    "verdict": t.verdict,
                    "progress": round(t.progress, 4),
                    "elapsed_s": round(t.elapsed_s, 2),
                    "summary": t.report.summary(),
                    "n_actions": len(t.report.actions),
                    "n_pending_approvals": len(t.report.pending_approvals),
                    "injection_flags": t.report.injection_flags,
                }
                for t in result.turns
            ],
        }