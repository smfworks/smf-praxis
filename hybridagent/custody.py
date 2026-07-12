"""Transactionally sequenced, cryptographically chained evidence custody ledger."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceError, EvidenceRegistry
from .persistence import Store

EVENT_TYPES = frozenset({
    "acquisition", "transfer", "copy", "transformation", "analysis",
    "verification", "disposition",
})


class CustodyError(ValueError):
    """A custody ownership, event, or chain invariant was violated."""


@dataclass(frozen=True)
class CustodyEvent:
    event_id: str
    organization_id: str
    workspace_id: str
    version_id: str
    sequence: int
    event_type: str
    actor_id: str
    tool_id: str
    occurred_ts: float
    details: dict[str, Any]
    previous_event_hash: str
    event_hash: str
    created_ts: float


class CustodyLedger:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.evidence = EvidenceRegistry(store)

    def record(self, organization_id: str, workspace_id: str, version_id: str, *,
               event_type: str, actor_id: str, tool_id: str, occurred_ts: float,
               details: dict[str, Any]) -> CustodyEvent:
        try:
            self.evidence._validate_scope_and_actor(
                organization_id, workspace_id, actor_id)
        except EvidenceError as exc:
            raise CustodyError(str(exc)) from exc
        if self.evidence.get_version(organization_id, workspace_id, version_id) is None:
            raise CustodyError("evidence version does not exist in workspace")
        if event_type not in EVENT_TYPES:
            raise CustodyError(f"unknown custody event: {event_type}")
        if not tool_id.strip():
            raise CustodyError("tool identity is required")
        try:
            details_json = json.dumps(details, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CustodyError("custody details must be JSON serializable") from exc
        event_id = f"custody-{uuid.uuid4().hex}"
        created_ts = time.time()
        with self.store._lock:
            try:
                self.store._conn.execute("BEGIN IMMEDIATE")
                prior = self.store._conn.execute(
                    "SELECT sequence,event_hash FROM evidence_custody_events "
                    "WHERE version_id=? ORDER BY sequence DESC LIMIT 1", (version_id,)).fetchone()
                sequence = int(prior["sequence"]) + 1 if prior else 1
                previous_hash = str(prior["event_hash"]) if prior else ""
                event_hash = self._hash(
                    organization_id, workspace_id, version_id, sequence, event_type,
                    actor_id, tool_id.strip(), float(occurred_ts), details_json, previous_hash)
                self.store._conn.execute(
                    "INSERT INTO evidence_custody_events(event_id,organization_id,"
                    "workspace_id,version_id,sequence,event_type,actor_id,tool_id,"
                    "occurred_ts,details_json,previous_event_hash,event_hash,created_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, organization_id, workspace_id, version_id, sequence,
                     event_type, actor_id, tool_id.strip(), float(occurred_ts),
                     details_json, previous_hash, event_hash, created_ts))
                self.store._conn.commit()
            except Exception:
                self.store._conn.rollback()
                raise
        result = self.get(organization_id, workspace_id, event_id)
        assert result is not None
        return result

    def get(self, organization_id: str, workspace_id: str,
            event_id: str) -> CustodyEvent | None:
        row = self.store._directory_one(
            "SELECT * FROM evidence_custody_events WHERE organization_id=? "
            "AND workspace_id=? AND event_id=?", (organization_id, workspace_id, event_id))
        return self._event(row) if row else None

    def list_for(self, organization_id: str, workspace_id: str,
                 version_id: str) -> list[CustodyEvent]:
        rows = self.store._directory_all(
            "SELECT * FROM evidence_custody_events WHERE organization_id=? "
            "AND workspace_id=? AND version_id=? ORDER BY sequence",
            (organization_id, workspace_id, version_id))
        return [self._event(row) for row in rows]

    def verify_chain(self, organization_id: str, workspace_id: str,
                     version_id: str) -> bool:
        previous = ""
        for expected, event in enumerate(
                self.list_for(organization_id, workspace_id, version_id), start=1):
            details_json = json.dumps(event.details, sort_keys=True, separators=(",", ":"))
            calculated = self._hash(
                event.organization_id, event.workspace_id, event.version_id,
                event.sequence, event.event_type, event.actor_id, event.tool_id,
                event.occurred_ts, details_json, previous)
            if (event.sequence != expected or event.previous_event_hash != previous
                    or event.event_hash != calculated):
                return False
            previous = event.event_hash
        return True

    @staticmethod
    def _hash(organization_id: str, workspace_id: str, version_id: str,
              sequence: int, event_type: str, actor_id: str, tool_id: str,
              occurred_ts: float, details_json: str, previous_hash: str) -> str:
        canonical = json.dumps({
            "actor_id": actor_id, "details": json.loads(details_json),
            "event_type": event_type, "occurred_ts": occurred_ts,
            "organization_id": organization_id, "previous_event_hash": previous_hash,
            "sequence": sequence, "tool_id": tool_id, "version_id": version_id,
            "workspace_id": workspace_id,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _event(row: dict[str, Any]) -> CustodyEvent:
        return CustodyEvent(
            event_id=row["event_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], version_id=row["version_id"],
            sequence=int(row["sequence"]), event_type=row["event_type"],
            actor_id=row["actor_id"], tool_id=row["tool_id"],
            occurred_ts=float(row["occurred_ts"]), details=json.loads(row["details_json"]),
            previous_event_hash=row["previous_event_hash"], event_hash=row["event_hash"],
            created_ts=float(row["created_ts"]))
