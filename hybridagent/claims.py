"""Workspace-scoped material claim ledger with fail-closed release verification."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceError, EvidenceRegistry
from .extraction import ExtractionRegistry
from .persistence import Store

STATUSES = frozenset({"supported", "contradicted", "unresolved", "abstained"})
RELATIONSHIPS = frozenset({"supports", "contradicts"})


class ClaimError(ValueError):
    """A claim ownership, support, or release invariant was violated."""


@dataclass(frozen=True)
class Claim:
    claim_id: str
    organization_id: str
    workspace_id: str
    text: str
    material: bool
    status: str
    created_by: str
    created_ts: float
    updated_ts: float


@dataclass(frozen=True)
class ClaimEvidenceLink:
    link_id: str
    organization_id: str
    workspace_id: str
    claim_id: str
    span_id: str
    relationship: str
    rationale: str
    created_by: str
    created_ts: float


class ClaimLedger:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.evidence = EvidenceRegistry(store)
        self.extractions = ExtractionRegistry(store)

    def create(self, organization_id: str, workspace_id: str, *, text: str,
               material: bool, created_by: str) -> Claim:
        self._validate(organization_id, workspace_id, created_by)
        clean = text.strip()
        if not clean:
            raise ClaimError("claim text is required")
        claim_id = f"claim-{uuid.uuid4().hex}"
        now = time.time()
        self.store._directory_execute(
            "INSERT INTO professional_claims(claim_id,organization_id,workspace_id,"
            "text,material,status,created_by,created_ts,updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (claim_id, organization_id, workspace_id, clean, int(material),
             "unresolved", created_by, now, now))
        result = self.get(organization_id, workspace_id, claim_id)
        assert result is not None
        return result

    def get(self, organization_id: str, workspace_id: str,
            claim_id: str) -> Claim | None:
        row = self.store._directory_one(
            "SELECT * FROM professional_claims WHERE organization_id=? "
            "AND workspace_id=? AND claim_id=?", (organization_id, workspace_id, claim_id))
        return self._claim(row) if row else None

    def link_evidence(self, organization_id: str, workspace_id: str, claim_id: str,
                      span_id: str, *, relationship: str, rationale: str,
                      created_by: str) -> ClaimEvidenceLink:
        self._validate(organization_id, workspace_id, created_by)
        if self.get(organization_id, workspace_id, claim_id) is None:
            raise ClaimError("claim does not exist in workspace")
        if self.extractions.get_span(organization_id, workspace_id, span_id) is None:
            raise ClaimError("evidence span does not exist in workspace")
        if relationship not in RELATIONSHIPS:
            raise ClaimError(f"unknown evidence relationship: {relationship}")
        if not rationale.strip():
            raise ClaimError("evidence-link rationale is required")
        link_id = f"claim-link-{uuid.uuid4().hex}"
        now = time.time()
        self.store._directory_execute(
            "INSERT INTO claim_evidence_links(link_id,organization_id,workspace_id,"
            "claim_id,span_id,relationship,rationale,created_by,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (link_id, organization_id, workspace_id, claim_id, span_id,
             relationship, rationale.strip(), created_by, now))
        row = self.store._directory_one(
            "SELECT * FROM claim_evidence_links WHERE link_id=?", (link_id,))
        assert row is not None
        return self._link(row)

    def set_status(self, organization_id: str, workspace_id: str, claim_id: str, *,
                   status: str, actor_id: str) -> Claim:
        self._validate(organization_id, workspace_id, actor_id)
        if status not in STATUSES:
            raise ClaimError(f"unknown claim status: {status}")
        claim = self.get(organization_id, workspace_id, claim_id)
        if claim is None:
            raise ClaimError("claim does not exist in workspace")
        if status == "supported":
            support = self.store._directory_one(
                "SELECT 1 FROM claim_evidence_links WHERE organization_id=? "
                "AND workspace_id=? AND claim_id=? AND relationship='supports' LIMIT 1",
                (organization_id, workspace_id, claim_id))
            if support is None:
                raise ClaimError("supporting evidence is required")
        self.store._directory_execute(
            "UPDATE professional_claims SET status=?,updated_ts=? WHERE organization_id=? "
            "AND workspace_id=? AND claim_id=?",
            (status, time.time(), organization_id, workspace_id, claim_id))
        result = self.get(organization_id, workspace_id, claim_id)
        assert result is not None
        return result

    def release_ready(self, organization_id: str, workspace_id: str) -> bool:
        workspace = self.store._directory_one(
            "SELECT 1 FROM professional_workspaces WHERE organization_id=? "
            "AND workspace_id=?", (organization_id, workspace_id))
        if workspace is None:
            return False
        row = self.store._directory_one(
            "SELECT COUNT(*) AS blocked FROM professional_claims WHERE organization_id=? "
            "AND workspace_id=? AND material=1 AND status<>'supported'",
            (organization_id, workspace_id))
        return bool(row is not None and int(row["blocked"]) == 0)

    def _validate(self, organization_id: str, workspace_id: str, actor_id: str) -> None:
        try:
            self.evidence._validate_scope_and_actor(
                organization_id, workspace_id, actor_id)
        except EvidenceError as exc:
            raise ClaimError(str(exc)) from exc

    @staticmethod
    def _claim(row: dict[str, Any]) -> Claim:
        return Claim(
            claim_id=row["claim_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], text=row["text"],
            material=bool(row["material"]), status=row["status"],
            created_by=row["created_by"], created_ts=float(row["created_ts"]),
            updated_ts=float(row["updated_ts"]))

    @staticmethod
    def _link(row: dict[str, Any]) -> ClaimEvidenceLink:
        return ClaimEvidenceLink(
            link_id=row["link_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], claim_id=row["claim_id"],
            span_id=row["span_id"], relationship=row["relationship"],
            rationale=row["rationale"], created_by=row["created_by"],
            created_ts=float(row["created_ts"]))
