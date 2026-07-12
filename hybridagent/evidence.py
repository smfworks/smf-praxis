"""Canonical, tenant-owned evidence sources and immutable source versions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .organizations import OrganizationDirectory
from .persistence import Store
from .workspaces import WorkspaceDirectory


class EvidenceError(ValueError):
    """An evidence identity, ownership, or immutability invariant was violated."""


@dataclass(frozen=True)
class EvidenceSource:
    source_id: str
    organization_id: str
    workspace_id: str
    canonical_uri: str
    publisher: str
    author: str
    publication_date: str
    revision_date: str
    jurisdiction: str
    authority_tier: str
    created_by: str
    created_ts: float


@dataclass(frozen=True)
class EvidenceVersion:
    version_id: str
    source_id: str
    organization_id: str
    workspace_id: str
    content_hash: str
    mime_type: str
    retrieved_ts: float
    parser: str
    parser_version: str
    parser_config: dict[str, Any]
    license: str
    original_object_path: str
    created_by: str
    created_ts: float
    supersedes_version_id: str
    superseded_by_version_id: str


class EvidenceRegistry:
    """Persistence boundary for canonical sources and immutable evidence versions."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.organizations = OrganizationDirectory(store)
        self.workspaces = WorkspaceDirectory(store)

    def create_source(
        self, organization_id: str, workspace_id: str, *, canonical_uri: str,
        publisher: str, created_by: str, author: str = "",
        publication_date: str = "", revision_date: str = "",
        jurisdiction: str = "", authority_tier: str = "",
    ) -> EvidenceSource:
        self._validate_scope_and_actor(organization_id, workspace_id, created_by)
        uri = canonical_uri.strip()
        clean_publisher = publisher.strip()
        parsed = urlsplit(uri)
        if not uri or not parsed.scheme:
            raise EvidenceError("canonical URI must be absolute")
        if not clean_publisher:
            raise EvidenceError("publisher is required")
        source_id = f"src-{uuid.uuid4().hex}"
        now = time.time()
        try:
            self.store._directory_execute(
                "INSERT INTO evidence_sources(source_id,organization_id,workspace_id,"
                "canonical_uri,publisher,author,publication_date,revision_date,"
                "jurisdiction,authority_tier,created_by,created_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, organization_id, workspace_id, uri, clean_publisher,
                 author.strip(), publication_date.strip(), revision_date.strip(),
                 jurisdiction.strip(), authority_tier.strip(), created_by, now))
        except sqlite3.IntegrityError as exc:
            raise EvidenceError("canonical source already exists in workspace") from exc
        result = self.get_source(organization_id, workspace_id, source_id)
        assert result is not None
        return result

    def get_source(self, organization_id: str, workspace_id: str,
                   source_id: str) -> EvidenceSource | None:
        row = self.store._directory_one(
            "SELECT * FROM evidence_sources WHERE organization_id=? "
            "AND workspace_id=? AND source_id=?",
            (organization_id, workspace_id, source_id))
        return self._source(row) if row else None

    def add_version(
        self, organization_id: str, workspace_id: str, source_id: str, *,
        content: bytes, mime_type: str, retrieved_ts: float, parser: str,
        parser_version: str, parser_config: dict[str, Any], license: str,
        original_object_path: str, created_by: str,
        supersedes_version_id: str = "",
    ) -> EvidenceVersion:
        self._validate_scope_and_actor(organization_id, workspace_id, created_by)
        if self.get_source(organization_id, workspace_id, source_id) is None:
            raise EvidenceError("source does not exist in workspace")
        if not isinstance(content, bytes):
            raise EvidenceError("evidence content must be bytes")
        required = {
            "MIME type": mime_type, "parser": parser,
            "parser version": parser_version, "license": license,
            "original object path": original_object_path,
        }
        missing = next((name for name, value in required.items() if not value.strip()), None)
        if missing:
            raise EvidenceError(f"{missing} is required")
        try:
            config_json = json.dumps(parser_config, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise EvidenceError("parser configuration must be JSON serializable") from exc
        if supersedes_version_id:
            prior = self._version_row(supersedes_version_id)
            if (prior is None or prior["organization_id"] != organization_id
                    or prior["workspace_id"] != workspace_id
                    or prior["source_id"] != source_id):
                raise EvidenceError("superseded version does not belong to source scope")
        version_id = f"ev-{uuid.uuid4().hex}"
        digest = hashlib.sha256(content).hexdigest()
        now = time.time()
        with self.store._lock:
            try:
                self.store._conn.execute("BEGIN IMMEDIATE")
                self.store._conn.execute(
                    "INSERT INTO evidence_source_versions(version_id,source_id,"
                    "organization_id,workspace_id,content_hash,mime_type,retrieved_ts,"
                    "parser,parser_version,parser_config_json,license,"
                    "original_object_path,created_by,created_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, source_id, organization_id, workspace_id, digest,
                     mime_type.strip(), float(retrieved_ts), parser.strip(),
                     parser_version.strip(), config_json, license.strip(),
                     original_object_path.strip(), created_by, now))
                if supersedes_version_id:
                    self.store._conn.execute(
                        "INSERT INTO evidence_version_supersessions("
                        "prior_version_id,next_version_id,created_ts) VALUES (?,?,?)",
                        (supersedes_version_id, version_id, now))
                self.store._conn.commit()
            except sqlite3.IntegrityError as exc:
                self.store._conn.rollback()
                raise EvidenceError(
                    "evidence version already exists or was already superseded") from exc
            except Exception:
                self.store._conn.rollback()
                raise
        result = self.get_version(organization_id, workspace_id, version_id)
        assert result is not None
        return result

    def get_version(self, organization_id: str, workspace_id: str,
                    version_id: str) -> EvidenceVersion | None:
        row = self._version_row(version_id, organization_id, workspace_id)
        return self._version(row) if row else None

    def list_versions(self, organization_id: str, workspace_id: str,
                      source_id: str) -> list[EvidenceVersion]:
        rows = self.store._directory_all(
            self._version_select() + " WHERE v.organization_id=? AND v.workspace_id=? "
            "AND v.source_id=? ORDER BY v.created_ts,v.rowid",
            (organization_id, workspace_id, source_id))
        return [self._version(row) for row in rows]

    def verify_content(self, organization_id: str, workspace_id: str,
                       version_id: str, content: bytes) -> bool:
        row = self._version_row(version_id, organization_id, workspace_id)
        if row is None or not isinstance(content, bytes):
            return False
        return hashlib.sha256(content).hexdigest() == row["content_hash"]

    def update_version(self, version_id: str, **changes: Any) -> None:
        del version_id, changes
        raise EvidenceError("evidence source versions are immutable")

    def _validate_scope_and_actor(self, organization_id: str, workspace_id: str,
                                  user_id: str) -> None:
        if self.workspaces.get(organization_id, workspace_id) is None:
            raise EvidenceError("workspace does not exist in organization")
        organization = self.organizations.organization(organization_id)
        user = self.organizations.user(user_id)
        membership = self.organizations.membership(organization_id, user_id)
        if (organization is None or organization.status != "active" or user is None
                or user.status != "active" or membership is None
                or membership.status != "active"):
            raise EvidenceError("creator must be an active organization member")

    def _version_row(self, version_id: str, organization_id: str | None = None,
                     workspace_id: str | None = None) -> dict[str, Any] | None:
        sql = self._version_select() + " WHERE v.version_id=?"
        params: list[Any] = [version_id]
        if organization_id is not None:
            sql += " AND v.organization_id=?"
            params.append(organization_id)
        if workspace_id is not None:
            sql += " AND v.workspace_id=?"
            params.append(workspace_id)
        return self.store._directory_one(sql, tuple(params))

    @staticmethod
    def _version_select() -> str:
        return (
            "SELECT v.*,prior.prior_version_id AS supersedes_version_id,"
            "next.next_version_id AS superseded_by_version_id "
            "FROM evidence_source_versions v "
            "LEFT JOIN evidence_version_supersessions prior "
            "ON prior.next_version_id=v.version_id "
            "LEFT JOIN evidence_version_supersessions next "
            "ON next.prior_version_id=v.version_id")

    @staticmethod
    def _source(row: dict[str, Any]) -> EvidenceSource:
        return EvidenceSource(
            source_id=row["source_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], canonical_uri=row["canonical_uri"],
            publisher=row["publisher"], author=row["author"],
            publication_date=row["publication_date"], revision_date=row["revision_date"],
            jurisdiction=row["jurisdiction"], authority_tier=row["authority_tier"],
            created_by=row["created_by"], created_ts=float(row["created_ts"]))

    @staticmethod
    def _version(row: dict[str, Any]) -> EvidenceVersion:
        return EvidenceVersion(
            version_id=row["version_id"], source_id=row["source_id"],
            organization_id=row["organization_id"], workspace_id=row["workspace_id"],
            content_hash=row["content_hash"], mime_type=row["mime_type"],
            retrieved_ts=float(row["retrieved_ts"]), parser=row["parser"],
            parser_version=row["parser_version"],
            parser_config=json.loads(row["parser_config_json"]), license=row["license"],
            original_object_path=row["original_object_path"],
            created_by=row["created_by"], created_ts=float(row["created_ts"]),
            supersedes_version_id=row["supersedes_version_id"] or "",
            superseded_by_version_id=row["superseded_by_version_id"] or "")
