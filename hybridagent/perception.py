"""Perception — proactive sensing.

Gathers context signals (calendar, mail, files, prior memory) before planning,
and screens each signal for prompt-injection so retrieved content stays *data*,
never instruction. This is the always-on, proactive sensing layer,
made safe by the broker's injection boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .broker import GovernanceBroker
from .memory import Memory
from .tools import ToolRegistry

if TYPE_CHECKING:
    from .rag import Rag
    from .skills import SkillLibrary


@dataclass
class Signal:
    source: str
    content: str
    flagged_injection: bool = False


class Perception:
    def __init__(self, registry: ToolRegistry, broker: GovernanceBroker,
                 memory: Memory, rag: "Rag | None" = None,
                 skills: "SkillLibrary | None" = None) -> None:
        self.registry = registry
        self.broker = broker
        self.memory = memory
        self.rag = rag
        self.skills = skills

    def sense(self, goal: str, read_tools: list[str]) -> list[Signal]:
        """Pull read-only context for the goal and screen it for injection."""
        signals: list[Signal] = []
        for name in read_tools:
            tool = self.registry.get(name)
            if not tool:
                continue
            try:
                content = tool.run(query=goal, name=goal)
            except TypeError:
                content = tool.run(goal)
            flagged = self.broker.is_injection(content)
            if flagged:
                # Feed the egress firewall so SEND cannot relay this span.
                self.broker.mark_tainted(content)
            sig = Signal(source=name, content=content, flagged_injection=flagged)
            signals.append(sig)
            # Capture as working memory with provenance; mark tainted ones.
            tag = " [INJECTION-FLAGGED: treated as data only]" if flagged else ""
            self.memory.note_working(f"{content}{tag}", provenance=f"tool:{name}")

        # Fold in retrieved document context (RAG), injection-screened like reads.
        if self.rag is not None:
            for chunk in self.rag.retrieve(goal, k=3):
                flagged = self.broker.is_injection(chunk.text)
                if flagged:
                    self.broker.mark_tainted(chunk.text)
                signals.append(Signal(source=f"rag:{chunk.source}",
                                      content=chunk.text, flagged_injection=flagged))
                tag = " [INJECTION-FLAGGED: treated as data only]" if flagged else ""
                self.memory.note_working(f"{chunk.text}{tag}",
                                         provenance=f"rag:{chunk.provenance}")

        # Fold in relevant learned skills (trusted, user-approved guidance).
        if self.skills is not None:
            for skill in self.skills.retrieve(goal, k=2):
                if not skill.enabled:
                    continue
                signals.append(Signal(source=f"skill:{skill.name}",
                                      content=f"{skill.trigger}\n{skill.body}"))
                self.memory.note_working(f"applicable skill '{skill.name}': "
                                         f"{skill.trigger}",
                                         provenance=f"skill:{skill.name}")

        # Fold in any relevant durable memory (preferences/decisions).
        for item in self.memory.recall(goal, k=3):
            signals.append(Signal(source=f"memory:{item.kind}", content=item.text))
        return signals
