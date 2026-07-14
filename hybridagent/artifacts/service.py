"""Tenant-scoped Artifact Studio persistence, versioning, signing, and release service."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from hybridagent.artifacts.bundles import (
    ArtifactBundleError,
    build_release_bundle,
    canonical_json_bytes,
    verify_release_bundle,
)
from hybridagent.artifacts.models import ArtifactDocument, FigureBlock
from hybridagent.artifacts.render_common import checked_assets
from hybridagent.artifacts.renderers import render_artifact, supported_formats
from hybridagent.artifacts.validation import validate_document, validate_or_raise
from hybridagent.artifacts.versions import (
    ArtifactAsset,
    ArtifactDiff,
    ArtifactRelease,
    ArtifactSignature,
    ArtifactVersion,
    compare_documents,
)
from hybridagent.persistence import Store


class ArtifactServiceError(ValueError):
    """A tenant, version, review, signature, or release invariant failed."""


class ArtifactStudio:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def new_version_id() -> str:
        return f"artifact-version-{uuid.uuid4().hex}"

    def create_version(
        self,
        organization_id: str,
        workspace_id: str,
        document: ArtifactDocument,
        *,
        created_by: str,
        assets: dict[str, bytes] | None = None,
        expected_parent_version_id: str = "",
        version_id: str = "",
    ) -> ArtifactVersion:
        validate_or_raise(document)
        if (
            document.metadata.organization_id != organization_id
            or document.metadata.workspace_id != workspace_id
        ):
            raise ArtifactServiceError("document metadata does not match the tenant scope")
        if document.metadata.created_by != created_by:
            raise ArtifactServiceError("document creator does not match the version actor")
        asset_media = self._figure_media(document)
        resolved_assets = checked_assets(document, assets)
        if set(resolved_assets) != set(asset_media):
            raise ArtifactServiceError("artifact assets must exactly match document figures")
        if type(expected_parent_version_id) is not str:
            raise ArtifactServiceError("expected parent version must be exact text")
        if version_id:
            self._valid_generated_id(version_id, "artifact-version-")
        else:
            version_id = self.new_version_id()
        now = time.time()
        document_hash = document.content_hash()
        document_json = document.canonical_json()
        try:
            with self.store._lock:
                conn = self.store._conn
                conn.execute("BEGIN IMMEDIATE")
                self._actor_locked(conn, organization_id, workspace_id, created_by)
                self._external_references_locked(conn, organization_id, workspace_id, document)
                head = conn.execute(
                    "SELECT * FROM artifact_documents WHERE artifact_id=? "
                    "AND organization_id=? AND workspace_id=?",
                    (document.artifact_id, organization_id, workspace_id),
                ).fetchone()
                prior: sqlite3.Row | None = None
                if head is None:
                    if expected_parent_version_id:
                        raise ArtifactServiceError("initial artifact version cannot have a parent")
                    sequence = 1
                    parent_hash = ""
                    conn.execute(
                        "INSERT INTO artifact_documents(artifact_id,organization_id,workspace_id,"
                        "document_type,title,created_by,created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                        (document.artifact_id, organization_id, workspace_id,
                         document.metadata.document_type, document.metadata.title,
                         created_by, now, now),
                    )
                else:
                    if not expected_parent_version_id or head["head_version_id"] != expected_parent_version_id:
                        raise ArtifactServiceError("stale or missing expected artifact head")
                    prior = conn.execute(
                        "SELECT sequence,document_hash,document_json FROM artifact_versions "
                        "WHERE version_id=? AND artifact_id=? AND organization_id=? "
                        "AND workspace_id=?",
                        (expected_parent_version_id, document.artifact_id,
                         organization_id, workspace_id),
                    ).fetchone()
                    if prior is None:
                        raise ArtifactServiceError("expected parent version does not exist in scope")
                    sequence = int(prior["sequence"]) + 1
                    parent_hash = str(prior["document_hash"])
                if len(document.revisions) != sequence:
                    raise ArtifactServiceError("document revision history must match artifact sequence")
                if prior is not None:
                    prior_document = ArtifactDocument.from_json(prior["document_json"])
                    if document.revisions[:-1] != prior_document.revisions:
                        raise ArtifactServiceError(
                            "document revision history prefix must match the immutable parent"
                        )
                latest_revision = document.revisions[-1] if document.revisions else None
                if latest_revision is None or latest_revision.sequence != sequence:
                    raise ArtifactServiceError("document requires an exact latest revision record")
                if latest_revision.author_id != created_by:
                    raise ArtifactServiceError("latest revision author must match the version actor")
                if latest_revision.parent_hash != parent_hash:
                    raise ArtifactServiceError("document revision parent hash does not match the artifact head")
                conn.execute(
                    "INSERT INTO artifact_versions(version_id,artifact_id,organization_id,"
                    "workspace_id,sequence,parent_version_id,document_hash,document_json,"
                    "created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (version_id, document.artifact_id, organization_id, workspace_id,
                     sequence, expected_parent_version_id, document_hash, document_json,
                     created_by, now),
                )
                for asset_id in sorted(resolved_assets):
                    payload = resolved_assets[asset_id]
                    conn.execute(
                        "INSERT INTO artifact_version_assets(version_id,artifact_id,"
                        "organization_id,workspace_id,asset_id,media_type,content_hash,payload,"
                        "size_bytes) VALUES (?,?,?,?,?,?,?,?,?)",
                        (version_id, document.artifact_id, organization_id, workspace_id,
                         asset_id, asset_media[asset_id], hashlib.sha256(payload).hexdigest(),
                         payload, len(payload)),
                    )
                conn.execute(
                    "UPDATE artifact_documents SET head_version_id=?,updated_ts=? "
                    "WHERE artifact_id=? AND organization_id=? AND workspace_id=?",
                    (version_id, now, document.artifact_id, organization_id, workspace_id),
                )
                conn.commit()
        except ArtifactServiceError:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise ArtifactServiceError("artifact version violates durable storage invariants") from exc
        except BaseException:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise
        result = self.get_version(organization_id, workspace_id, version_id)
        assert result is not None
        return result

    def get_version(
        self, organization_id: str, workspace_id: str, version_id: str
    ) -> ArtifactVersion | None:
        row = self.store._directory_one(
            "SELECT * FROM artifact_versions WHERE organization_id=? AND workspace_id=? "
            "AND version_id=?",
            (organization_id, workspace_id, version_id),
        )
        if row is None:
            return None
        assets = self.store._directory_all(
            "SELECT asset_id,media_type,content_hash,size_bytes FROM artifact_version_assets "
            "WHERE organization_id=? AND workspace_id=? AND version_id=? ORDER BY asset_id",
            (organization_id, workspace_id, version_id),
        )
        return self._version(row, assets)

    def list_versions(
        self, organization_id: str, workspace_id: str, artifact_id: str
    ) -> list[ArtifactVersion]:
        rows = self.store._directory_all(
            "SELECT * FROM artifact_versions WHERE organization_id=? AND workspace_id=? "
            "AND artifact_id=? ORDER BY sequence",
            (organization_id, workspace_id, artifact_id),
        )
        result: list[ArtifactVersion] = []
        for row in rows:
            version = self.get_version(
                organization_id, workspace_id, row["version_id"]
            )
            if version is None:
                raise ArtifactServiceError("artifact version index is inconsistent")
            result.append(version)
        return result

    def compare(
        self,
        organization_id: str,
        workspace_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> ArtifactDiff:
        before = self.get_version(organization_id, workspace_id, from_version_id)
        after = self.get_version(organization_id, workspace_id, to_version_id)
        if before is None or after is None or before.artifact_id != after.artifact_id:
            raise ArtifactServiceError("artifact versions cannot be compared in this scope")
        return compare_documents(from_version_id, before.document, to_version_id, after.document)

    def render_version(
        self,
        organization_id: str,
        workspace_id: str,
        version_id: str,
        format_name: str,
    ) -> bytes:
        version = self.get_version(organization_id, workspace_id, version_id)
        if version is None:
            raise ArtifactServiceError("artifact version does not exist in scope")
        assets = self._asset_payloads(organization_id, workspace_id, version_id)
        return render_artifact(version.document, format_name, assets)

    def sign_version(
        self,
        organization_id: str,
        workspace_id: str,
        version_id: str,
        *,
        review_id: str,
        signer_user_id: str,
        role: str,
        meaning: str,
    ) -> ArtifactSignature:
        clean_role = role.strip()
        clean_meaning = meaning.strip()
        if not clean_role or not clean_meaning:
            raise ArtifactServiceError("signature role and meaning are required")
        signature_id = f"artifact-signature-{uuid.uuid4().hex}"
        now = time.time()
        try:
            with self.store._lock:
                conn = self.store._conn
                conn.execute("BEGIN IMMEDIATE")
                roles = self._actor_locked(conn, organization_id, workspace_id, signer_user_id)
                if clean_role not in roles:
                    raise ArtifactServiceError("signature role is not held by the signer")
                version = conn.execute(
                    "SELECT * FROM artifact_versions WHERE organization_id=? AND workspace_id=? "
                    "AND version_id=?",
                    (organization_id, workspace_id, version_id),
                ).fetchone()
                if version is None:
                    raise ArtifactServiceError("artifact version does not exist in scope")
                review = conn.execute(
                    "SELECT * FROM professional_reviews WHERE organization_id=? AND workspace_id=? "
                    "AND review_id=? AND review_type='professional_release' AND status='decided' "
                    "AND decision='approved' AND reviewer_user_id=?",
                    (organization_id, workspace_id, review_id, signer_user_id),
                ).fetchone()
                if review is None or not self._review_matches(review, version):
                    raise ArtifactServiceError("signature review does not approve the exact artifact version")
                conn.execute(
                    "INSERT INTO artifact_signatures(signature_id,artifact_id,version_id,"
                    "organization_id,workspace_id,document_hash,review_id,signer_user_id,"
                    "role,meaning,signed_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (signature_id, version["artifact_id"], version_id, organization_id,
                     workspace_id, version["document_hash"], review_id, signer_user_id,
                     clean_role, clean_meaning, now),
                )
                conn.commit()
        except ArtifactServiceError:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise ArtifactServiceError("artifact signature violates durable binding") from exc
        row = self.store._directory_one(
            "SELECT * FROM artifact_signatures WHERE signature_id=?", (signature_id,)
        )
        assert row is not None
        return self._signature(row)

    def release_version(
        self,
        organization_id: str,
        workspace_id: str,
        version_id: str,
        *,
        formats: tuple[str, ...],
        run_id: str,
        checkpoint_id: str,
        created_by: str,
        idempotency_key: str,
    ) -> ArtifactRelease:
        self._validate_formats(formats)
        self._validate_idempotency_key(idempotency_key)
        formats = tuple(sorted(formats))
        version = self.get_version(organization_id, workspace_id, version_id)
        if version is None:
            raise ArtifactServiceError("artifact version does not exist in scope")
        self._actor(organization_id, workspace_id, created_by)
        request_hash = hashlib.sha256(canonical_json_bytes({
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "version_id": version_id,
            "document_sha256": version.document_hash,
            "formats": list(formats),
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "created_by": created_by,
        })).hexdigest()
        existing = self._release_for_idempotency(
            organization_id, workspace_id, idempotency_key
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ArtifactServiceError("release idempotency key conflicts with another request")
            return existing
        validation = validate_document(version.document)
        if not validation.valid:
            raise ArtifactServiceError("artifact document does not pass validation")
        if not self._claim_release_ready(organization_id, workspace_id):
            raise ArtifactServiceError("workspace has unresolved material claims")
        reviews = self._approved_reviews(organization_id, workspace_id, version)
        signatures = self._signatures(organization_id, workspace_id, version_id)
        if not reviews or not signatures:
            raise ArtifactServiceError("approved exact review and reviewer signature are required")
        review_ids = {row["review_id"] for row in reviews}
        if any(row["review_id"] not in review_ids for row in signatures):
            raise ArtifactServiceError("artifact signature is not bound to an approved release review")
        review_id_list = tuple(sorted(review_ids))
        signature_id_list = tuple(sorted(row["signature_id"] for row in signatures))
        run_payload = self._run_payload(
            organization_id, workspace_id, run_id, checkpoint_id
        )
        assets = self._asset_payloads(organization_id, workspace_id, version_id)
        asset_media = {asset.asset_id: asset.media_type for asset in version.assets}
        renders = {name: render_artifact(version.document, name, assets) for name in formats}
        release_id = f"artifact-release-{uuid.uuid4().hex}"
        now = time.time()
        context = {
            "release_id": release_id,
            "artifact_id": version.artifact_id,
            "version_id": version.version_id,
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "document_sha256": version.document_hash,
            "formats": list(formats),
            "review_ids": list(review_id_list),
            "signature_ids": list(signature_id_list),
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "idempotency_key": idempotency_key,
            "request_sha256": request_hash,
            "created_by": created_by,
            "created_ts": now,
        }
        governance = {
            "claims": self._claims_payload(organization_id, workspace_id),
            "evidence": self._evidence_payload(organization_id, workspace_id, version.document),
            "reviews": [self._review_payload(row) for row in reviews],
            "signatures": [dict(row) for row in signatures],
            "run": run_payload,
        }
        try:
            bundle, manifest = build_release_bundle(
                release_context=context,
                document=version.document,
                renders=renders,
                assets=assets,
                asset_media=asset_media,
                governance=governance,
                validation_report=validation.to_dict(),
            )
        except ArtifactBundleError as exc:
            raise ArtifactServiceError(str(exc)) from exc
        bundle_hash = hashlib.sha256(bundle).hexdigest()
        try:
            with self.store._lock:
                conn = self.store._conn
                conn.execute("BEGIN IMMEDIATE")
                self._actor_locked(conn, organization_id, workspace_id, created_by)
                prior_release = conn.execute(
                    "SELECT * FROM artifact_releases WHERE organization_id=? "
                    "AND workspace_id=? AND idempotency_key=?",
                    (organization_id, workspace_id, idempotency_key),
                ).fetchone()
                if prior_release is not None:
                    if prior_release["request_hash"] != request_hash:
                        raise ArtifactServiceError(
                            "release idempotency key conflicts with another request"
                        )
                    conn.rollback()
                    existing = self._release(dict(prior_release))
                    self.verify_release(existing)
                    return existing
                head = conn.execute(
                    "SELECT head_version_id FROM artifact_documents WHERE artifact_id=? "
                    "AND organization_id=? AND workspace_id=?",
                    (version.artifact_id, organization_id, workspace_id),
                ).fetchone()
                if head is None or head["head_version_id"] != version_id:
                    raise ArtifactServiceError("only the exact current artifact head can be released")
                blocked = conn.execute(
                    "SELECT COUNT(*) AS n FROM professional_claims WHERE organization_id=? "
                    "AND workspace_id=? AND material=1 AND status<>'supported'",
                    (organization_id, workspace_id),
                ).fetchone()
                if blocked is None or int(blocked["n"]) != 0:
                    raise ArtifactServiceError("material claim state changed before release")
                for review_id in review_id_list:
                    row = conn.execute(
                        "SELECT * FROM professional_reviews WHERE review_id=? AND organization_id=? "
                        "AND workspace_id=? AND status='decided' AND decision='approved'",
                        (review_id, organization_id, workspace_id),
                    ).fetchone()
                    if row is None or not self._review_matches(row, dict(
                        artifact_id=version.artifact_id, version_id=version.version_id,
                        document_hash=version.document_hash,
                    )):
                        raise ArtifactServiceError("release review changed before commit")
                for signature_id in signature_id_list:
                    signer = conn.execute(
                        "SELECT 1 FROM artifact_signatures s "
                        "JOIN professional_reviews r ON r.review_id=s.review_id "
                        "JOIN organization_memberships m ON m.organization_id=s.organization_id "
                        "AND m.user_id=s.signer_user_id AND m.status='active' "
                        "JOIN organization_users u ON u.user_id=s.signer_user_id AND u.status='active' "
                        "JOIN organizations o ON o.organization_id=s.organization_id AND o.status='active' "
                        "JOIN professional_workspaces w ON w.organization_id=s.organization_id "
                        "AND w.workspace_id=s.workspace_id AND w.status='active' "
                        "WHERE s.signature_id=? AND s.organization_id=? AND s.workspace_id=? "
                        "AND s.version_id=? AND s.document_hash=? AND r.status='decided' "
                        "AND r.decision='approved' AND r.reviewer_user_id=s.signer_user_id "
                        "AND EXISTS (SELECT 1 FROM json_each(m.roles_json) WHERE value=s.role)",
                        (signature_id, organization_id, workspace_id,
                         version_id, version.document_hash),
                    ).fetchone()
                    if signer is None:
                        raise ArtifactServiceError(
                            "release requires an active signer holding the signed role"
                        )
                self._run_payload_locked(conn, organization_id, workspace_id, run_id, checkpoint_id)
                conn.execute(
                    "INSERT INTO artifact_releases(release_id,artifact_id,version_id,"
                    "organization_id,workspace_id,document_hash,formats_json,manifest_json,"
                    "validation_report_json,review_ids_json,signature_ids_json,"
                    "idempotency_key,request_hash,run_id,checkpoint_id,bundle_hash,bundle,"
                    "created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (release_id, version.artifact_id, version_id, organization_id,
                     workspace_id, version.document_hash,
                     canonical_json_bytes(list(formats)).decode(),
                     canonical_json_bytes(manifest).decode(),
                     canonical_json_bytes(validation.to_dict()).decode(),
                     canonical_json_bytes(list(review_id_list)).decode(),
                     canonical_json_bytes(list(signature_id_list)).decode(),
                     idempotency_key, request_hash, run_id, checkpoint_id,
                     bundle_hash, bundle, created_by, now),
                )
                conn.commit()
        except ArtifactServiceError:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            if self.store._conn.in_transaction:
                self.store._conn.rollback()
            raise ArtifactServiceError("artifact release violates durable binding") from exc
        release = self.get_release(organization_id, workspace_id, release_id)
        assert release is not None
        return release

    def _release_for_idempotency(
        self, organization_id: str, workspace_id: str, idempotency_key: str
    ) -> ArtifactRelease | None:
        row = self.store._directory_one(
            "SELECT * FROM artifact_releases WHERE organization_id=? AND workspace_id=? "
            "AND idempotency_key=?",
            (organization_id, workspace_id, idempotency_key),
        )
        if row is None:
            return None
        release = self._release(row)
        self.verify_release(release)
        return release

    def get_release(
        self, organization_id: str, workspace_id: str, release_id: str
    ) -> ArtifactRelease | None:
        row = self.store._directory_one(
            "SELECT * FROM artifact_releases WHERE organization_id=? AND workspace_id=? "
            "AND release_id=?",
            (organization_id, workspace_id, release_id),
        )
        if row is None:
            return None
        release = self._release(row)
        self.verify_release(release)
        return release

    @staticmethod
    def verify_release(release: ArtifactRelease) -> dict[str, Any]:
        if type(release) is not ArtifactRelease:
            raise ArtifactServiceError("release must be an exact ArtifactRelease")
        if hashlib.sha256(release.bundle).hexdigest() != release.bundle_hash:
            raise ArtifactServiceError("persisted release bundle hash is invalid")
        try:
            manifest = verify_release_bundle(release.bundle)
        except ArtifactBundleError as exc:
            raise ArtifactServiceError(str(exc)) from exc
        if manifest != release.manifest:
            raise ArtifactServiceError("persisted release manifest differs from its bundle")
        context = manifest.get("release")
        expected = {
            "release_id": release.release_id,
            "artifact_id": release.artifact_id,
            "version_id": release.version_id,
            "organization_id": release.organization_id,
            "workspace_id": release.workspace_id,
            "document_sha256": release.document_hash,
            "formats": list(release.formats),
            "review_ids": list(release.review_ids),
            "signature_ids": list(release.signature_ids),
            "run_id": release.run_id,
            "checkpoint_id": release.checkpoint_id,
            "idempotency_key": release.idempotency_key,
            "request_sha256": release.request_hash,
            "created_by": release.created_by,
            "created_ts": release.created_ts,
        }
        if context != expected:
            raise ArtifactServiceError("persisted release identity differs from its bundle")
        return manifest

    def _actor(self, organization_id: str, workspace_id: str, user_id: str) -> set[str]:
        with self.store._lock:
            return self._actor_locked(self.store._conn, organization_id, workspace_id, user_id)

    @staticmethod
    def _actor_locked(
        conn: sqlite3.Connection, organization_id: str, workspace_id: str, user_id: str
    ) -> set[str]:
        row = conn.execute(
            "SELECT m.roles_json FROM professional_workspaces w JOIN organizations o ON "
            "o.organization_id=w.organization_id JOIN organization_memberships m ON "
            "m.organization_id=w.organization_id JOIN organization_users u ON u.user_id=m.user_id "
            "WHERE w.organization_id=? AND w.workspace_id=? AND w.status='active' "
            "AND o.status='active' AND m.user_id=? AND m.status='active' AND u.status='active'",
            (organization_id, workspace_id, user_id),
        ).fetchone()
        if row is None:
            raise ArtifactServiceError("active workspace membership is required")
        roles = json.loads(row["roles_json"])
        if type(roles) is not list or any(type(item) is not str for item in roles):
            raise ArtifactServiceError("persisted membership roles are invalid")
        return set(roles)

    @staticmethod
    def _external_references_locked(
        conn: sqlite3.Connection,
        organization_id: str,
        workspace_id: str,
        document: ArtifactDocument,
    ) -> None:
        versions: dict[str, str] = {}
        for source in document.sources:
            row = conn.execute(
                "SELECT content_hash FROM evidence_source_versions WHERE organization_id=? "
                "AND workspace_id=? AND source_id=? AND version_id=?",
                (organization_id, workspace_id, source.source_id, source.source_version_id),
            ).fetchone()
            if row is None or row["content_hash"] != source.content_hash:
                raise ArtifactServiceError("source manifest does not match immutable evidence")
            versions[source.source_id] = source.source_version_id
        for citation in document.citations:
            for span_id in citation.span_ids:
                span = conn.execute(
                    "SELECT version_id FROM evidence_spans WHERE organization_id=? "
                    "AND workspace_id=? AND span_id=?",
                    (organization_id, workspace_id, span_id),
                ).fetchone()
                if span is None or span["version_id"] != versions.get(citation.source_id):
                    raise ArtifactServiceError("citation span does not match its exact source version")
            for claim_id in citation.claim_ids:
                claim = conn.execute(
                    "SELECT 1 FROM professional_claims WHERE organization_id=? AND workspace_id=? "
                    "AND claim_id=?",
                    (organization_id, workspace_id, claim_id),
                ).fetchone()
                if claim is None:
                    raise ArtifactServiceError("citation claim does not exist in scope")
                linked = conn.execute(
                    "SELECT 1 FROM claim_evidence_links WHERE organization_id=? AND workspace_id=? "
                    "AND claim_id=? AND span_id IN ("
                    + ",".join("?" for _ in citation.span_ids)
                    + ") LIMIT 1",
                    (organization_id, workspace_id, claim_id, *citation.span_ids),
                ).fetchone() if citation.span_ids else None
                if linked is None:
                    raise ArtifactServiceError("citation claim is not linked to its evidence span")

    @staticmethod
    def _figure_media(document: ArtifactDocument) -> dict[str, str]:
        result: dict[str, str] = {}
        for section in (*document.sections, *document.appendices):
            for block in section.blocks:
                if type(block) is FigureBlock:
                    previous = result.get(block.asset_id)
                    if previous is not None and previous != block.media_type:
                        raise ArtifactServiceError("figure asset has conflicting media types")
                    result[block.asset_id] = block.media_type
        return result

    def _asset_payloads(
        self, organization_id: str, workspace_id: str, version_id: str
    ) -> dict[str, bytes]:
        rows = self.store._directory_all(
            "SELECT asset_id,content_hash,payload,size_bytes FROM artifact_version_assets "
            "WHERE organization_id=? AND workspace_id=? AND version_id=? ORDER BY asset_id",
            (organization_id, workspace_id, version_id),
        )
        result: dict[str, bytes] = {}
        for row in rows:
            payload = bytes(row["payload"])
            if len(payload) != row["size_bytes"] or hashlib.sha256(payload).hexdigest() != row["content_hash"]:
                raise ArtifactServiceError("persisted artifact asset failed integrity verification")
            result[row["asset_id"]] = payload
        return result

    def _approved_reviews(
        self, organization_id: str, workspace_id: str, version: ArtifactVersion
    ) -> list[dict[str, Any]]:
        rows = self.store._directory_all(
            "SELECT * FROM professional_reviews WHERE organization_id=? AND workspace_id=? "
            "AND review_type='professional_release' AND status='decided' AND decision='approved' "
            "ORDER BY reviewed_ts,review_id",
            (organization_id, workspace_id),
        )
        version_row = {
            "artifact_id": version.artifact_id,
            "version_id": version.version_id,
            "document_hash": version.document_hash,
        }
        return [row for row in rows if self._review_matches(row, version_row)]

    def _signatures(
        self, organization_id: str, workspace_id: str, version_id: str
    ) -> list[dict[str, Any]]:
        return self.store._directory_all(
            "SELECT * FROM artifact_signatures WHERE organization_id=? AND workspace_id=? "
            "AND version_id=? ORDER BY signed_ts,signature_id",
            (organization_id, workspace_id, version_id),
        )

    @staticmethod
    def _review_matches(review: Any, version: Any) -> bool:
        try:
            subject = json.loads(review["subject_json"])
        except (TypeError, ValueError, KeyError):
            return False
        return (
            type(subject) is dict
            and subject.get("artifact_id") == version["artifact_id"]
            and subject.get("version_id") == version["version_id"]
            and subject.get("document_sha256") == version["document_hash"]
        )

    def _claim_release_ready(self, organization_id: str, workspace_id: str) -> bool:
        row = self.store._directory_one(
            "SELECT COUNT(*) AS n FROM professional_claims WHERE organization_id=? "
            "AND workspace_id=? AND material=1 AND status<>'supported'",
            (organization_id, workspace_id),
        )
        return row is not None and int(row["n"]) == 0

    def _claims_payload(self, organization_id: str, workspace_id: str) -> dict[str, Any]:
        claims = self.store._directory_all(
            "SELECT * FROM professional_claims WHERE organization_id=? AND workspace_id=? "
            "ORDER BY created_ts,claim_id",
            (organization_id, workspace_id),
        )
        links = self.store._directory_all(
            "SELECT * FROM claim_evidence_links WHERE organization_id=? AND workspace_id=? "
            "ORDER BY created_ts,link_id",
            (organization_id, workspace_id),
        )
        return {"claims": claims, "evidence_links": links}

    def _evidence_payload(
        self, organization_id: str, workspace_id: str, document: ArtifactDocument
    ) -> dict[str, Any]:
        version_ids = [item.source_version_id for item in document.sources]
        span_ids = sorted({span for citation in document.citations for span in citation.span_ids})
        versions: list[dict[str, Any]] = []
        spans: list[dict[str, Any]] = []
        for version_id in version_ids:
            row = self.store._directory_one(
                "SELECT * FROM evidence_source_versions WHERE organization_id=? "
                "AND workspace_id=? AND version_id=?",
                (organization_id, workspace_id, version_id),
            )
            if row is not None:
                row = dict(row)
                row["parser_config"] = json.loads(row.pop("parser_config_json"))
                versions.append(row)
        for span_id in span_ids:
            row = self.store._directory_one(
                "SELECT * FROM evidence_spans WHERE organization_id=? AND workspace_id=? "
                "AND span_id=?",
                (organization_id, workspace_id, span_id),
            )
            if row is not None:
                row = dict(row)
                row["locator"] = json.loads(row.pop("locator_json"))
                spans.append(row)
        return {"source_manifest": document.to_dict()["sources"], "versions": versions, "spans": spans}

    def _run_payload(
        self, organization_id: str, workspace_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, Any]:
        with self.store._lock:
            return self._run_payload_locked(
                self.store._conn, organization_id, workspace_id, run_id, checkpoint_id
            )

    @staticmethod
    def _run_payload_locked(
        conn: sqlite3.Connection,
        organization_id: str,
        workspace_id: str,
        run_id: str,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        run = conn.execute(
            "SELECT * FROM professional_runs WHERE organization_id=? AND workspace_id=? "
            "AND run_id=?",
            (organization_id, workspace_id, run_id),
        ).fetchone()
        checkpoint = conn.execute(
            "SELECT * FROM run_checkpoints WHERE organization_id=? AND workspace_id=? "
            "AND run_id=? AND checkpoint_id=?",
            (organization_id, workspace_id, run_id, checkpoint_id),
        ).fetchone()
        if run is None or checkpoint is None or run["status"] in {"cancelled", "failed"}:
            raise ArtifactServiceError("release requires a valid non-failed durable run checkpoint")
        run_value = dict(run)
        checkpoint_value = dict(checkpoint)
        for value in (run_value, checkpoint_value):
            for key in ("schema_manifest_json", "state_json", "interrupt_payload_json"):
                if key in value:
                    raw = value.pop(key)
                    value[key.removesuffix("_json")] = json.loads(raw) if raw else {}
        return {"run": run_value, "checkpoint": checkpoint_value}

    @staticmethod
    def _review_payload(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["subject"] = json.loads(result.pop("subject_json"))
        result["decision_payload"] = json.loads(result.pop("decision_payload_json"))
        return result

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if (
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            or len(value) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        ):
            raise ArtifactServiceError(
                "release idempotency key must be 1-200 visible characters"
            )

    @staticmethod
    def _validate_formats(formats: tuple[str, ...]) -> None:
        if (
            type(formats) is not tuple
            or not formats
            or any(type(item) is not str for item in formats)
            or len(formats) != len(set(formats))
            or any(item not in supported_formats() for item in formats)
            or not {"json", "markdown"} <= set(formats)
        ):
            raise ArtifactServiceError(
                "release formats must be a distinct supported tuple including json and markdown"
            )

    @staticmethod
    def _valid_generated_id(value: str, prefix: str) -> None:
        suffix = value.removeprefix(prefix)
        if not value.startswith(prefix) or len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix):
            raise ArtifactServiceError("artifact generated ID is invalid")

    @staticmethod
    def _version(row: dict[str, Any], assets: list[dict[str, Any]]) -> ArtifactVersion:
        document = ArtifactDocument.from_json(row["document_json"])
        validate_or_raise(document)
        if document.content_hash() != row["document_hash"]:
            raise ArtifactServiceError("persisted artifact version hash is invalid")
        return ArtifactVersion(
            version_id=row["version_id"], artifact_id=row["artifact_id"],
            organization_id=row["organization_id"], workspace_id=row["workspace_id"],
            sequence=int(row["sequence"]), parent_version_id=row["parent_version_id"],
            document_hash=row["document_hash"], document=document,
            assets=tuple(ArtifactAsset(asset_id=item["asset_id"], media_type=item["media_type"],
                                       content_hash=item["content_hash"],
                                       size_bytes=int(item["size_bytes"])) for item in assets),
            created_by=row["created_by"], created_ts=float(row["created_ts"]),
        )

    @staticmethod
    def _signature(row: dict[str, Any]) -> ArtifactSignature:
        return ArtifactSignature(
            signature_id=row["signature_id"], artifact_id=row["artifact_id"],
            version_id=row["version_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], document_hash=row["document_hash"],
            review_id=row["review_id"], signer_user_id=row["signer_user_id"],
            role=row["role"], meaning=row["meaning"], signed_ts=float(row["signed_ts"]),
        )

    @staticmethod
    def _release(row: dict[str, Any]) -> ArtifactRelease:
        return ArtifactRelease(
            release_id=row["release_id"], artifact_id=row["artifact_id"],
            version_id=row["version_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], document_hash=row["document_hash"],
            formats=tuple(json.loads(row["formats_json"])),
            manifest=json.loads(row["manifest_json"]),
            validation_report=json.loads(row["validation_report_json"]),
            review_ids=tuple(json.loads(row["review_ids_json"])),
            signature_ids=tuple(json.loads(row["signature_ids_json"])),
            idempotency_key=row["idempotency_key"], request_hash=row["request_hash"],
            run_id=row["run_id"], checkpoint_id=row["checkpoint_id"],
            bundle_hash=row["bundle_hash"], bundle=bytes(row["bundle"]),
            created_by=row["created_by"], created_ts=float(row["created_ts"]),
        )
