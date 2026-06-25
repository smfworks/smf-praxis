"""Inter-subagent scratchpad — a durable, scoped shared context channel.

Subagents under the same store can leave structured notes for sibling agents
without violating governance: every write is namespaced, attributed to a
``agent_id``, and persisted with TTL + provenance, so the broker's audit chain
can trace which subagent surfaced which fact.

This is intentionally a key/value store on top of the existing memory tier
(scope ``scratchpad``) rather than an ad-hoc table — every fact is governed by
the same retention/forget policies as durable memory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


SCRATCHPAD_KIND = "scratchpad"


@dataclass
class ScratchpadEntry:
    key: str
    value: str
    written_by: str
    ns: str = "default"
    ts: float = 0.0


class Scratchpad:
    def __init__(self, store) -> None:
        self.store = store

    def _provenance(self, ns: str, key: str, written_by: str) -> str:
        return f"scratchpad:{ns}:{key}:{written_by}"

    def write(self, key: str, value: str, written_by: str,
              ns: str = "default",
              ttl_seconds: float | None = 3600.0) -> None:
        expires_at = (time.time() + ttl_seconds) if ttl_seconds else None
        prov = self._provenance(ns, key, written_by)
        # Episodic so the scratchpad doesn't pollute durable memory; the
        # standard retention/decay knobs still apply.
        self.store.add_memory("episodic", value, prov, SCRATCHPAD_KIND,
                              expires_at=expires_at)

    def read(self, key: str, ns: str = "default") -> list[ScratchpadEntry]:
        now = time.time()
        out: list[ScratchpadEntry] = []
        prefix = f"scratchpad:{ns}:{key}:"
        for row in self.store.load_memory("episodic"):
            if row["kind"] != SCRATCHPAD_KIND:
                continue
            if not row["provenance"].startswith(prefix):
                continue
            if row.get("expires_at") and row["expires_at"] <= now:
                continue
            written_by = row["provenance"][len(prefix):]
            out.append(ScratchpadEntry(
                key=key, value=row["text"], written_by=written_by, ns=ns,
                ts=row["ts"]))
        out.sort(key=lambda e: e.ts, reverse=True)
        return out

    def latest(self, key: str, ns: str = "default") -> ScratchpadEntry | None:
        rows = self.read(key, ns=ns)
        return rows[0] if rows else None
