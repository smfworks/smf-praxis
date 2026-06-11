"""Perception — OpenClaw-style proactive sensing.

Gathers context signals (calendar, mail, files, prior memory) before planning,
and screens each signal for prompt-injection so retrieved content stays *data*,
never instruction. This is the "always-on, proactive" strength of OpenClaw,
made safe by the broker's injection boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from .broker import GovernanceBroker
from .memory import Memory
from .tools import ToolRegistry


@dataclass
class Signal:
    source: str
    content: str
    flagged_injection: bool = False


class Perception:
    def __init__(self, registry: ToolRegistry, broker: GovernanceBroker, memory: Memory) -> None:
        self.registry = registry
        self.broker = broker
        self.memory = memory

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

        # Fold in any relevant durable memory (preferences/decisions).
        for item in self.memory.recall(goal, k=3):
            signals.append(Signal(source=f"memory:{item.kind}", content=item.text))
        return signals
