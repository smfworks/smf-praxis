"""Active Memory Consolidation — the "sleep" pass.

A background loop that periodically reads recent episodic + durable memory,
extracts structured metadata (entities/topics), finds cross-corpus
connections, synthesizes one cross-cutting insight, and re-rates salience.

This is the genuinely novel idea from the GCP "Always-On Memory Agent" research
(see research-always-on-memory-agent.md): instead of passive RAG (embed once,
retrieve later), an LLM periodically replays, connects, and compresses — the
"brain during sleep" metaphor.

Design (per praxis-consolidation-phase-plan.md §1):
- Reuse the agent stack: constructor-injected ``memory``, ``llm``, ``store``.
  No new agent class, no daemon dependency in this module.
- READ-risk: read memory, write local insight + connection rows, no external
  effect. The insights it writes are normal durable memories and inherit all
  existing memory governance (expiry, provenance, deletion).
- Reuse, don't reinvent retrieval: insights surface through the existing
  ``memory.recall()`` path as ``kind="insight"`` durable items. No new
  retrieval namespace, no new query path.
- Keep it reversible: all writes are additive; gating lives in the daemon
  (Slice 3), not here.

The LLM is a protocol-compatible duck of ``LLMClient`` — tests pass a fake
returning canned JSON; production passes the real ``PraxisAgent.llm``.
Malformed LLM JSON is skipped + logged, never raises (per risk register).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

_log = logging.getLogger(__name__)


# Reasoning-model CoT preambles we strip from insight text. Qwen3 / Kimi
# thinking / DeepSeek-R1-style models often leak "Thinking Process:\n\n1.
# **Deconstruct...**" into `content` before (or instead of) the actual answer.
# We strip these so the consolidator writes the synthesized insight, not the
# chain-of-thought. Dogfood finding (2026-07-16).
_REASONING_HEADERS = re.compile(
    r"^\s*(?:here(?:'s| is| are)? (?:a |the )?(?:thinking process|reasoning|"
    r"analysis|step(?:-by-step)?)|thinking process|let(?:'s| us| me) "
    r"(?:think|break this down|analyze)|reasoning|step \d+|analysis)\b[^:\n]*"
    r"[:.]?\s*\n",
    re.IGNORECASE,
)
# Markdown numbered-list reasoning steps: "1.  **Deconstruct the Input:**..." —
# the bullet that opens a reasoning block. We cut from the first such step to
# the end (the insight, if any, comes after the reasoning).
_REASONING_STEP = re.compile(
    r"^\s*\d+\.\s+\*\*[^*\n]+\*\*\s*:?\s*\n", re.MULTILINE,
)


def _strip_reasoning(text: str) -> str:
    """Remove reasoning-model chain-of-thought from an insight response.

    Strategy: if the text opens with a known reasoning header or numbered-list
    step, the model wrote its thinking into `content`. The actual insight
    (when present) is the final declarative sentence(s) after the reasoning.
    When we can't confidently separate them, return the text unchanged — a
    leaky insight is better than a discarded one, and the prompt tightening
    below is the primary defence.
    """
    if not text:
        return text
    # Cut a leading "Thinking Process:" / "Let me think:" style header + the
    # reasoning block that follows. Keep anything *after* the first blank line
    # that follows the header (the actual insight, if the model wrote one).
    m = _REASONING_HEADERS.match(text)
    if m:
        after = text[m.end():]
        # the insight is usually after the first blank line in the remainder;
        # if there's no clear split, prefer the last short declarative line.
        parts = [p.strip() for p in after.split("\n\n") if p.strip()]
        if len(parts) >= 2:
            # last chunk is the most likely insight (reasoning → conclusion)
            return parts[-1].split("\n")[-1].strip() or parts[-1]
        # only one chunk after header → it's all reasoning; return as-is so the
        # length check rejects it if it's too short, or keeps it if substantive.
        return after.strip()
    # Numbered-list reasoning opener (no header word) — same heuristic.
    m = _REASONING_STEP.match(text)
    if m:
        after = text[m.end():]
        parts = [p.strip() for p in after.split("\n\n") if p.strip()]
        if len(parts) >= 2:
            return parts[-1].split("\n")[-1].strip() or parts[-1]
        return after.strip()
    return text


class _LLMLike(Protocol):
    """Structural type for the LLM dependency. Matches LLMClient.complete()."""
    def complete(self, prompt: str, system: str | None = None,
                 role: str = "general", sensitivity: str = "normal",
                 difficulty: str | None = None,
                 max_tokens: int | None = None) -> str: ...


class _MemoryLike(Protocol):
    """Structural type for the Memory dependency. Matches Memory.add_durable()."""
    def add_durable(self, text: str, kind: str, provenance: str,
                    salience: float = 1.0,
                    expires_at: float | None = None) -> Any: ...


class _StoreLike(Protocol):
    """Structural type for the Store dependency. Matches the methods we added
    in Slice 1 + list_unconsolidated."""
    def list_unconsolidated(self, limit: int = 20,
                            re_consolidate_after: float | None = None,
                            workspace_id: str | None = None) -> list[dict]: ...
    def update_memory_metadata(self, memory_id: int, entities: list[str],
                               topics: list[str]) -> None: ...
    def add_memory_connection(self, from_id: int, to_id: int,
                              relationship: str, insight_id: int | None = None,
                              created_at: float | None = None) -> int | None: ...
    def update_memory_salience(self, memory_id: int, salience: float) -> None: ...
    def mark_consolidated(self, memory_ids: list[int],
                          ts: float | None = None) -> None: ...


@dataclass
class ConsolidationReport:
    """Outcome of one consolidation pass. Surfaced to the daemon event stream
    and CLI ``praxis consolidation status``."""
    items_reviewed: int = 0
    connections_made: int = 0
    insights_written: int = 0
    salience_rerated: int = 0
    skipped_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "items_reviewed": self.items_reviewed,
            "connections_made": self.connections_made,
            "insights_written": self.insights_written,
            "salience_rerated": self.salience_rerated,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class _Conn:
    """Parsed connection from LLM output."""
    from_id: int
    to_id: int
    relationship: str


class MemoryConsolidator:
    """The consolidation pass. One public method: ``run()``.

    Parameters mirror the plan's config (§2.4) but are passed explicitly here
    so tests can vary them without a config object. The daemon (Slice 3) reads
    them from ``agents.consolidation.*`` and forwards them.
    """

    def __init__(self, memory: _MemoryLike, llm: _LLMLike, store: _StoreLike,
                 *, window_size: int = 20,
                 min_items: int = 3,
                 max_connections: int = 5,
                 rerate_salience: bool = True,
                 extract_metadata: bool = True,
                 workspace_id: str | None = None) -> None:
        self.memory = memory
        self.llm = llm
        self.store = store
        self.window_size = window_size
        self.min_items = min_items
        self.max_connections = max_connections
        self.rerate_salience = rerate_salience
        self.extract_metadata = extract_metadata
        self.workspace_id = workspace_id

    # ----------------------------------------------------------- public API
    def run(self, re_consolidate_after: float | None = None) -> ConsolidationReport:
        """Run one consolidation pass. Never raises on LLM/JSON failure —
        returns a report with ``skipped_reason`` set and logs the error."""
        report = ConsolidationReport()
        window = self._select_window(re_consolidate_after)
        report.items_reviewed = len(window)
        if len(window) < self.min_items:
            report.skipped_reason = f"too few unconsolidated ({len(window)} < {self.min_items})"
            return report

        ts = time.time()
        ids = [w["id"] for w in window]

        if self.extract_metadata:
            self._extract_metadata(window)

        conns = self._find_connections(window)
        report.connections_made = len(conns)

        insight_id = self._synthesize_insight(window, conns, ts)
        if insight_id is not None:
            report.insights_written = 1

        if self.rerate_salience:
            report.salience_rerated = self._rerate(window, conns)

        self.store.mark_consolidated(ids, ts=ts)
        return report

    def stats(self) -> dict:
        """Cheap counts for the CLI/dashboard. Reads the store directly."""
        uncons = self.store.list_unconsolidated(limit=1)
        return {
            "pending": len(self.store.list_unconsolidated(limit=1000)),
            "has_unconsolidated": len(uncons) > 0,
        }

    # ----------------------------------------------------------- private
    def _select_window(self, re_consolidate_after: float | None
                       ) -> list[dict]:
        return self.store.list_unconsolidated(
            limit=self.window_size,
            re_consolidate_after=re_consolidate_after,
            workspace_id=self.workspace_id,
        )

    def _extract_metadata(self, window: list[dict]) -> None:
        """Gap C: extract entities/topics per item. Never blocks a pass on
        malformed JSON — skip the item and continue."""
        if not window:
            return
        prompt = self._metadata_prompt(window)
        try:
            raw = self.llm.complete(prompt, role="consolidation")
            parsed = self._parse_json_list(raw)
        except Exception as exc:  # network, timeout, parse
            _log.warning("consolidation metadata extract failed: %s", exc)
            return

        if not isinstance(parsed, list):
            _log.warning("consolidation metadata: expected list, got %r",
                         type(parsed).__name__)
            return
        by_id = {item.get("id"): item for item in parsed
                 if isinstance(item, dict) and "id" in item}
        for w in window:
            entry = by_id.get(w["id"])
            if not entry:
                continue
            entities = entry.get("entities", [])
            topics = entry.get("topics", [])
            if not isinstance(entities, list) or not isinstance(topics, list):
                continue
            self.store.update_memory_metadata(w["id"], entities, topics)

    def _find_connections(self, window: list[dict]) -> list[_Conn]:
        """Gap B: pairwise relationships across the window."""
        if len(window) < 2:
            return []
        # max_connections <= 0 means "no connections" (disable the feature).
        # Without this guard the append-then-break loop below would make 1
        # connection even when max_connections=0, which is wrong for both
        # interpretations of 0 (no-connections / no-limit). Caught by bug-hunt.
        if self.max_connections <= 0:
            return []
        prompt = self._connections_prompt(window)
        try:
            raw = self.llm.complete(prompt, role="consolidation")
            parsed = self._parse_json_list(raw)
        except Exception as exc:
            _log.warning("consolidation connection find failed: %s", exc)
            return []
        if not isinstance(parsed, list):
            return []

        valid_ids = {w["id"] for w in window}
        conns: list[_Conn] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                fid = int(item["from_id"])
                tid = int(item["to_id"])
                rel = str(item["relationship"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if not rel or fid not in valid_ids or tid not in valid_ids or fid == tid:
                continue
            conns.append(_Conn(fid, tid, rel))
            if len(conns) >= self.max_connections:
                break
        return conns

    def _synthesize_insight(self, window: list[dict], conns: list[_Conn],
                            ts: float) -> int | None:
        """Gap A: one cross-cutting insight written as a durable memory of
        kind='insight'. Returns the new memory id, or None on failure."""
        prompt = self._insight_prompt(window, conns)
        try:
            # Reasoning models (Qwen3, Kimi thinking) need a larger token budget
            # than the default 1024 to finish chain-of-thought and emit the
            # conclusion. The post-filter (_strip_reasoning) then extracts the
            # insight from the reasoning preamble. Dogfood finding.
            text = self.llm.complete(
                prompt, role="consolidation", max_tokens=4096
            ).strip()
        except Exception as exc:
            _log.warning("consolidation insight synthesis failed: %s", exc)
            return None
        # Reasoning models (Qwen3, Kimi thinking, DeepSeek-R1) often leak their
        # chain-of-thought into `content` before the actual answer. Strip the
        # reasoning preamble so we don't write CoT as an insight.
        text = _strip_reasoning(text)
        if not text or len(text) < 10:
            return None
        # summarize-not-hoard: Memory.add_durable clips to _MAX_DURABLE_CHARS.
        item = self.memory.add_durable(
            text, kind="insight", provenance=f"consolidation:{ts:.0f}",
            salience=0.8,
        )
        insight_id = getattr(item, "id", None)
        # Link the connections to the insight that produced them.
        if insight_id is not None:
            for c in conns:
                self.store.add_memory_connection(
                    c.from_id, c.to_id, c.relationship,
                    insight_id=insight_id, created_at=ts)
        return insight_id

    def _rerate(self, window: list[dict], conns: list[_Conn]) -> int:
        """Gap D: bounded salience feedback. +0.1 per connection received,
        +0.05 per access since last pass. Monotonic (never down-rates).
        Hard cap at 1.0 (Store.update_memory_salience clamps)."""
        bumped = 0
        # access-count deltas: bump any item that was recalled since last pass
        access_by_id: dict[int, int] = {w["id"]: 0 for w in window}
        for w in window:
            access_by_id[w["id"]] = int(w.get("access_count", 0))

        conn_by_id: dict[int, int] = {w["id"]: 0 for w in window}
        for c in conns:
            conn_by_id[c.from_id] = conn_by_id.get(c.from_id, 0) + 1
            conn_by_id[c.to_id] = conn_by_id.get(c.to_id, 0) + 1

        for w in window:
            mid = w["id"]
            old_salience = float(w.get("salience", 1.0))
            bump = 0.0
            if conn_by_id.get(mid, 0):
                bump += 0.1 * conn_by_id[mid]
            # access bump only if accessed since last consolidation pass
            last_consolidated = w.get("last_consolidated_at")
            if w.get("access_count", 0) > 0 and (
                    last_consolidated is None or
                    w.get("last_access_ts", 0) > last_consolidated):
                bump += 0.05
            if bump > 0:
                self.store.update_memory_salience(mid, old_salience + bump)
                bumped += 1
        return bumped

    # ----------------------------------------------------------- prompts
    def _metadata_prompt(self, window: list[dict]) -> str:
        lines = []
        for w in window:
            lines.append(f"[#{w['id']}] ({w['kind']}) {w['text']}")
        return (
            "For each memory below, extract:\n"
            "- entities: key people, companies, products, concepts, locations (max 6)\n"
            "- topics: 2-4 short topic tags\n"
            "Return JSON: [{\"id\": <int>, \"entities\": [...], \"topics\": [...]}]\n"
            "Only valid JSON, no prose.\n\nMemories:\n" + "\n".join(lines)
        )

    def _connections_prompt(self, window: list[dict]) -> str:
        lines = []
        for w in window:
            lines.append(f"[#{w['id']}] {w['text']}")
        return (
            "Find non-obvious connections between these memories. For each connection:\n"
            "- from_id, to_id\n"
            "- relationship: one short phrase (e.g. 'cost reduction enables scaling', "
            "'addresses reliability gap')\n"
            "Only return connections that reveal a pattern or cross-cutting theme.\n"
            "Skip trivial or identical connections. Max "
            f"{self.max_connections} connections.\n"
            "Return JSON: [{\"from_id\": <int>, \"to_id\": <int>, "
            "\"relationship\": \"...\"}]\n"
            "Only valid JSON, no prose.\n\nMemories:\n" + "\n".join(lines)
        )

    def _insight_prompt(self, window: list[dict], conns: list[_Conn]) -> str:
        lines = [f"[#{w['id']}] {w['text']}" for w in window]
        conn_lines = [f"- #{c.from_id} -> #{c.to_id}: {c.relationship}"
                      for c in conns] or ["(no connections found)"]
        return (
            "Given these memories and the connections found between them, "
            "synthesize ONE cross-cutting insight — a pattern, theme, or "
            "implication that no single memory states alone. Be concrete, "
            "not generic. Respond with ONLY the insight: 1-2 sentences, no "
            "reasoning, no thinking process, no numbered steps, no 'the "
            "insight is' preamble. Start directly with the insight.\n\n"
            "Memories:\n" + "\n".join(lines) + "\n\nConnections:\n"
            + "\n".join(conn_lines) + "\n\nInsight:"
        )

    # ----------------------------------------------------------- json safety
    @staticmethod
    def _parse_json_list(raw: str) -> Any:
        """Strict JSON parse with fence-stripping. Returns the parsed value,
        or raises ValueError (caller catches and skips)."""
        text = raw.strip()
        # strip ```json ... ``` fences if the model wrapped the output
        if text.startswith("```"):
            inner = text.split("```", 2)
            if len(inner) >= 2:
                body = inner[1]
                if body.lower().startswith("json"):
                    body = body[4:]
                text = body.strip()
        # find the first [ and the last ] — tolerate trailing prose
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise ValueError("no JSON array found in LLM response")
        return json.loads(text[start:end + 1])