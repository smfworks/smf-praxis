"""Self-improvement + consolidation.

After a cycle, distills working/episodic memory into durable facts and reusable
skills with provenance, then clears working memory (summarize-not-hoard). This
is what turns one-off actions into compounding capability.
"""
from __future__ import annotations

from dataclasses import dataclass

from .llm import LLMClient
from .memory import Memory


@dataclass
class ReflectionResult:
    episodic_added: int
    durable_added: int
    skill: str | None


class Reflector:
    def __init__(self, memory: Memory, llm: LLMClient | None = None) -> None:
        self.memory = memory
        self.llm = llm or LLMClient()

    def reflect(self, goal: str, outcomes: list[str]) -> ReflectionResult:
        # 1. Episodic: one provenance-tagged summary of this cycle's outcome.
        summary = self.llm.summarize(f"Goal: {goal}\nOutcomes:\n" + "\n".join(outcomes))
        self.memory.add_episodic(summary, provenance=f"cycle:{goal[:40]}")

        durable_added = 0
        skill_text = None

        # 2. Durable skill: if the cycle completed multiple successful actions,
        #    promote the pattern as a reusable skill (concise, with provenance).
        successes = [o for o in outcomes if "halted" not in o and "denied" not in o]
        if len(successes) >= 2:
            skill_text = (
                f"Skill: for goals like '{goal[:48]}', perceive calendar+mail, "
                "then draft (autonomy) and route sends through approval."
            )
            self.memory.add_durable(skill_text, kind="skill", provenance=f"reflect:{goal[:40]}")
            durable_added += 1

        # 3. Summarize-not-hoard: clear raw working memory after distillation.
        self.memory.working.clear()
        return ReflectionResult(
            episodic_added=1, durable_added=durable_added, skill=skill_text
        )
