"""Multi-tier memory with provenance and summarize-not-hoard.

Three layers:
    working   - current task state, in-process only, cleared each cycle
    episodic  - summaries of interactions/outcomes with provenance
    durable   - stable facts / preferences / decisions / learned skills

Consolidation distills working+episodic into durable memory and discards raw
working state, eliminating the "memory hoarding" failure mode. Full
private bodies are never stored durably — only concise summaries + provenance.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from .bm25 import BM25Index

if TYPE_CHECKING:
    from .persistence import Store


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
    id: int | None = None
    salience: float = 1.0
    access_count: int = 0
    last_access_ts: float | None = None
    expires_at: float | None = None
    ts: float = field(default_factory=time.time)


# Heuristic: durable memory should never swallow raw private bodies.
_MAX_DURABLE_CHARS = 280


class _ExtractLLM(Protocol):
    """Structural type for the optional extract-on-write LLM. Matches
    LLMClient.complete(). Using a Protocol keeps memory.py dependency-free
    (no import of llm.py at runtime)."""
    def complete(self, prompt: str, system: str | None = None,
                 role: str = "general", sensitivity: str = "normal",
                 difficulty: str | None = None) -> str: ...


class Memory:
    def __init__(self, store: "Store | None" = None,
                 llm: "_ExtractLLM | None" = None,
                 extract_metadata: bool = False) -> None:
        self.working: list[MemoryItem] = []
        self.episodic: list[MemoryItem] = []
        self.durable: list[MemoryItem] = []
        self.store = store
        self.llm = llm
        self.extract_metadata = extract_metadata
        if store is not None:
            self._hydrate(store)

    def _hydrate(self, store: "Store") -> None:
        """Load persisted episodic + durable memory from disk on startup."""
        for row in store.load_memory(Tier.EPISODIC.value):
            self.episodic.append(MemoryItem(
                text=row["text"], tier=Tier.EPISODIC,
                provenance=row["provenance"], kind=row["kind"], id=row.get("id"),
                salience=row.get("salience", 1.0),
                access_count=row.get("access_count", 0),
                last_access_ts=row.get("last_access_ts"),
                expires_at=row.get("expires_at"), ts=row["ts"]))
        for row in store.load_memory(Tier.DURABLE.value):
            self.durable.append(MemoryItem(
                text=row["text"], tier=Tier.DURABLE,
                provenance=row["provenance"], kind=row["kind"], id=row.get("id"),
                salience=row.get("salience", 1.0),
                access_count=row.get("access_count", 0),
                last_access_ts=row.get("last_access_ts"),
                expires_at=row.get("expires_at"), ts=row["ts"]))

    # ----------------------------------------------------------- write paths
    def note_working(self, text: str, provenance: str = "agent") -> MemoryItem:
        item = MemoryItem(text=text, tier=Tier.WORKING, provenance=provenance)
        self.working.append(item)
        return item

    def add_episodic(self, text: str, provenance: str) -> MemoryItem:
        item = MemoryItem(text=text, tier=Tier.EPISODIC, provenance=provenance,
                          salience=0.6)
        self.episodic.append(item)
        if self.store is not None:
            item.id = self.store.add_memory(
                Tier.EPISODIC.value, item.text, item.provenance, item.kind,
                item.ts, salience=item.salience, expires_at=item.expires_at)
            self._extract_metadata(item)
        return item

    def add_durable(self, text: str, kind: str, provenance: str,
                    salience: float = 1.0,
                    expires_at: float | None = None) -> MemoryItem:
        # Enforce summarize-not-hoard: durable entries are concise.
        clipped = text if len(text) <= _MAX_DURABLE_CHARS else text[:_MAX_DURABLE_CHARS] + "…"
        item = MemoryItem(text=clipped, tier=Tier.DURABLE, kind=kind,
                          provenance=provenance, salience=salience,
                          expires_at=expires_at)
        self.durable.append(item)
        if self.store is not None:
            item.id = self.store.add_memory(
                Tier.DURABLE.value, item.text, item.provenance, item.kind,
                item.ts, salience=item.salience, expires_at=item.expires_at)
            # Insights written by consolidation are themselves durable memories;
            # don't extract metadata on them (they're the output, not the input,
            # and extracting would compound LLM cost on every consolidation pass).
            if kind != "insight":
                self._extract_metadata(item)
        return item

    def _extract_metadata(self, item: MemoryItem) -> None:
        """Gap C write path: extract entities/topics on insert. Honest-fail —
        never blocks a memory write on extraction. No-op when no LLM, when
        extract_metadata is off, or when the item has no store id yet."""
        if not self.extract_metadata or self.llm is None or self.store is None:
            return
        if item.id is None:
            return
        try:
            prompt = (
                "Extract from this memory:\n"
                "- entities: key people, companies, products, concepts, locations (max 6)\n"
                "- topics: 2-4 short topic tags\n"
                "Return JSON: {\"entities\": [...], \"topics\": [...]}\n"
                "Only valid JSON, no prose.\n\nMemory:\n" + item.text
            )
            raw = self.llm.complete(prompt, role="consolidation")
            text = raw.strip()
            if text.startswith("```"):
                inner = text.split("```", 2)
                if len(inner) >= 2:
                    body = inner[1]
                    if body.lower().startswith("json"):
                        body = body[4:]
                    text = body.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end < start:
                return  # malformed -> honest-fail, leave defaults
            parsed = json.loads(text[start:end + 1])
            if not isinstance(parsed, dict):
                return
            entities = parsed.get("entities", [])
            topics = parsed.get("topics", [])
            if not isinstance(entities, list) or not isinstance(topics, list):
                return
            self.store.update_memory_metadata(item.id, entities, topics)
        except Exception:
            # Honest-fail: any LLM/JSON/IO error leaves the memory written with
            # default []/[] metadata. The consolidation pass (Slice 2) will
            # still extract on the read window as a fallback.
            return

    # --------------------------------------------------------------- reading
    def recall(self, query: str, k: int = 5) -> list[MemoryItem]:
        """Rank durable+episodic memory by **BM25** relevance to ``query``, then
        re-rank by salience, recency, and prior access.

        BM25 weights rare/discriminative terms over common ones and saturates
        repeated terms, so a short note that mentions the exact topic beats a
        long one that merely shares filler words. Expired items are skipped and
        every recalled item has its access recorded (for salience feedback).
        """
        now = time.time()
        pool = [it for it in (self.durable + self.episodic)
                if not (it.expires_at and it.expires_at <= now)]
        if not pool:
            return []
        index = BM25Index.build((str(i), it.text) for i, it in enumerate(pool))
        relevance = dict(index.search(query, k=len(pool)))
        candidates: list[tuple[float, int, MemoryItem]] = []
        for i, item in enumerate(pool):
            rel = relevance.get(str(i), 0.0)
            if rel <= 0.0:
                continue  # shares no query term -> not a match
            age_days = max(0.0, (now - item.ts) / 86400.0)
            freshness = 1.0 / (1.0 + age_days / 30.0)
            score = rel + item.salience + (0.1 * item.access_count) + freshness
            candidates.append((score, i, item))
        candidates.sort(key=lambda t: (-t[0], t[1]))
        recalled = [it for _score, _i, it in candidates[:k]]
        for item in recalled:
            item.access_count += 1
            item.last_access_ts = now
            if self.store is not None and item.id is not None:
                self.store.record_memory_access(item.id)
        return recalled

    def recall_context(self, query: str, k: int = 4,
                       max_chars: int = 800) -> str:
        """A compact, labelled block of the most relevant memories (or ``''``).

        Suitable for prepending to a chat system prompt so prior facts,
        decisions, and notes flow into the model's context automatically.
        """
        hits = self.recall(query, k=k)
        lines: list[str] = []
        used = 0
        for it in hits:
            entry = f"- ({it.kind}) {it.text}"
            if used + len(entry) > max_chars:
                break
            lines.append(entry)
            used += len(entry)
        if not lines:
            return ""
        return ("Relevant memory from earlier work (use if helpful):\n"
                + "\n".join(lines))

    def durable_of_kind(self, kind: str) -> list[MemoryItem]:
        return [it for it in self.durable if it.kind == kind]

    def stats(self) -> dict:
        return {
            "working": len(self.working),
            "episodic": len(self.episodic),
            "durable": len(self.durable),
            "skills": len(self.durable_of_kind("skill")),
        }

    # -------------------------------------------------------- retention policy
    def purge_expired(self) -> int:
        """Remove memory items past their explicit ``expires_at``.

        This is the GDPR/HIPAA-style purge hook: anything with a retention
        deadline gets dropped from both the in-memory tier list and the on-disk
        store. Returns the number of items removed.
        """
        now = time.time()
        removed = 0
        for tier_list in (self.episodic, self.durable):
            survivors = []
            for item in tier_list:
                if item.expires_at and item.expires_at <= now:
                    if self.store is not None and item.id is not None:
                        self.store.delete_memory(item.id)
                    removed += 1
                else:
                    survivors.append(item)
            tier_list[:] = survivors
        return removed

    def decay_episodic(self, max_age_days: float = 90.0,
                       salience_floor: float = 0.2) -> int:
        """Forget low-salience episodic entries older than ``max_age_days``.

        Keeps the episodic tier from growing unbounded while preserving
        high-salience records that the operator (or the recall ranker) has
        marked important. Returns the number of items dropped.
        """
        now = time.time()
        cutoff = now - max_age_days * 86400.0
        survivors = []
        removed = 0
        for item in self.episodic:
            if item.ts < cutoff and item.salience <= salience_floor:
                if self.store is not None and item.id is not None:
                    self.store.delete_memory(item.id)
                removed += 1
            else:
                survivors.append(item)
        self.episodic[:] = survivors
        return removed

    def forget_by_provenance(self, provenance_prefix: str) -> int:
        """Bulk delete memory rows whose ``provenance`` starts with the given
        prefix (e.g. ``"user:michael"``). Useful for right-to-be-forgotten
        requests and for revoking access to a specific data subject's traces.
        """
        removed = 0
        for tier_list in (self.episodic, self.durable):
            survivors = []
            for item in tier_list:
                if item.provenance.startswith(provenance_prefix):
                    if self.store is not None and item.id is not None:
                        self.store.delete_memory(item.id)
                    removed += 1
                else:
                    survivors.append(item)
            tier_list[:] = survivors
        return removed
