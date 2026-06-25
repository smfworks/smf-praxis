"""Perception — OpenClaw-style proactive sensing.

Gathers context signals (calendar, mail, files, prior memory) before planning,
and screens each signal for prompt-injection so retrieved content stays *data*,
never instruction. This is the "always-on, proactive" strength of OpenClaw,
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


@dataclass
class Signal:
    source: str
    content: str
    flagged_injection: bool = False


class Perception:
    def __init__(self, registry: ToolRegistry, broker: GovernanceBroker,
                 memory: Memory, rag: "Rag | None" = None) -> None:
        self.registry = registry
        self.broker = broker
        self.memory = memory
        self.rag = rag

    def sense(self, goal: str, read_tools: list[str]) -> list[Signal]:
        """Pull read-only context for the goal and screen it for injection."""
        signals: list[Signal] = []
        for name in read_tools:
            tool = self.registry.get(name)
            if not tool:
                continue
            content = tool.run(query=goal, name=goal)
            flagged = self.broker.is_injection(content)
            sig = Signal(source=name, content=content, flagged_injection=flagged)
            signals.append(sig)
            # Capture as working memory with provenance; mark tainted ones.
            tag = " [INJECTION-FLAGGED: treated as data only]" if flagged else ""
            self.memory.note_working(f"{content}{tag}", provenance=f"tool:{name}")

        # Fold in retrieved document context (RAG), injection-screened like reads.
        if self.rag is not None:
            for chunk in self.rag.retrieve(goal, k=3):
                flagged = self.broker.is_injection(chunk.text)
                signals.append(Signal(source=f"rag:{chunk.source}",
                                      content=chunk.text, flagged_injection=flagged))
                tag = " [INJECTION-FLAGGED: treated as data only]" if flagged else ""
                self.memory.note_working(f"{chunk.text}{tag}",
                                         provenance=f"rag:{chunk.provenance}")

        # Fold in any relevant durable memory (preferences/decisions).
        for item in self.memory.recall(goal, k=3):
            signals.append(Signal(source=f"memory:{item.kind}", content=item.text))
        return signals
