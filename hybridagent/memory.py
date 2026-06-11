"""Hermes-style multi-tier memory with provenance and summarize-not-hoard.

Three layers (per the Hermes integration guide):
    working   - current task state, in-process only, cleared each cycle
    episodic  - summaries of interactions/outcomes with provenance
    durable   - stable facts / preferences / decisions / learned skills

Consolidation distills working+episodic into durable memory and discards raw
working state, eliminating OpenClaw's "memory hoarding" failure mode. Full
private bodies are never stored durably — only concise summaries + provenance.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    DURABLE = "durable"


@dataclass
class MemoryItem:
    text: str
    tier: Tier
    provenance: str = "agent"          # where this came from (source attribution)
    kind: str = "note"                  # note | fact | preference | decision | skill
    ts: float = field(default_factory=time.time)


# Heuristic: durable memory should never swallow raw private bodies.
_MAX_DURABLE_CHARS = 280


class Memory:
    def __init__(self) -> None:
        self.working: list[MemoryItem] = []
        self.episodic: list[MemoryItem] = []
        self.durable: list[MemoryItem] = []

    # ----------------------------------------------------------- write paths
    def note_working(self, text: str, provenance: str = "agent") -> MemoryItem:
        item = MemoryItem(text=text, tier=Tier.WORKING, provenance=provenance)
        self.working.append(item)
        return item

    def add_episodic(self, text: str, provenance: str) -> MemoryItem:
        item = MemoryItem(text=text, tier=Tier.EPISODIC, provenance=provenance)
        self.episodic.append(item)
        return item

    def add_durable(self, text: str, kind: str, provenance: str) -> MemoryItem:
        # Enforce summarize-not-hoard: durable entries are concise.
        clipped = text if len(text) <= _MAX_DURABLE_CHARS else text[:_MAX_DURABLE_CHARS] + "…"
        item = MemoryItem(text=clipped, tier=Tier.DURABLE, kind=kind, provenance=provenance)
        self.durable.append(item)
        return item

    # --------------------------------------------------------------- reading
    def recall(self, query: str, k: int = 5) -> list[MemoryItem]:
        q = set(query.lower().split())
        pool = self.durable + self.episodic
        scored = sorted(
            pool,
            key=lambda it: len(q & set(it.text.lower().split())),
            reverse=True,
        )
        return [it for it in scored if q & set(it.text.lower().split())][:k]

    def durable_of_kind(self, kind: str) -> list[MemoryItem]:
        return [it for it in self.durable if it.kind == kind]

    def stats(self) -> dict:
        return {
            "working": len(self.working),
            "episodic": len(self.episodic),
            "durable": len(self.durable),
            "skills": len(self.durable_of_kind("skill")),
        }
