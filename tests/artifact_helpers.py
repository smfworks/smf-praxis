"""Shared exact-scenario fixtures for Phase 5 Artifact Studio tests."""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from hybridagent.artifacts import (
    ArtifactDocument,
    Citation,
    DocumentMetadata,
    FigureBlock,
    ParagraphBlock,
    RevisionRecord,
    Section,
    SourceManifestEntry,
    TableBlock,
)
from hybridagent.checkpoints import CheckpointRegistry, WorkflowRun
from hybridagent.claims import ClaimLedger
from hybridagent.evidence import EvidenceRegistry
from hybridagent.extraction import ExtractionRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass
class ArtifactScope:
    store: Store
    organization_id: str
    workspace_id: str
    owner_id: str
    reviewer_id: str
    source_id: str
    source_version_id: str
    source_hash: str
    span_id: str
    claim_id: str
    run: WorkflowRun


def scope(path: Path) -> ArtifactScope:
    store = Store(path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, owner = organizations.bootstrap("Artifact Practice", "owner@example.com")
    reviewer = organizations.create_user("reviewer@example.com")
    organizations.add_membership(
        organization.organization_id, reviewer.user_id, roles=("reviewer",)
    )
    workspace = WorkspaceDirectory(store).create(
        organization.organization_id,
        "MAT-ART-1",
        "matter",
        "Artifact Matter",
        owner_user_id=owner.user_id,
    )
    evidence = EvidenceRegistry(store)
    source = evidence.create_source(
        organization.organization_id,
        workspace.workspace_id,
        canonical_uri="https://example.test/evidence/source-1",
        publisher="Example Publisher",
        created_by=owner.user_id,
        author="Example Author",
    )
    version = evidence.add_version(
        organization.organization_id,
        workspace.workspace_id,
        source.source_id,
        content=b"The exact supported finding.",
        mime_type="text/plain",
        retrieved_ts=time.time(),
        parser="fixture",
        parser_version="1",
        parser_config={},
        license="test",
        original_object_path="evidence/source-1.txt",
        created_by=owner.user_id,
    )
    span = ExtractionRegistry(store).add_span(
        organization.organization_id,
        workspace.workspace_id,
        version.version_id,
        locator_type="document",
        locator={"paragraph": 1},
        extracted_text="The exact supported finding.",
        created_by=owner.user_id,
    )
    claims = ClaimLedger(store)
    claim = claims.create(
        organization.organization_id,
        workspace.workspace_id,
        text="The finding is supported.",
        material=True,
        created_by=owner.user_id,
    )
    claims.link_evidence(
        organization.organization_id,
        workspace.workspace_id,
        claim.claim_id,
        span.span_id,
        relationship="supports",
        rationale="The exact span supports the material claim.",
        created_by=owner.user_id,
    )
    claims.set_status(
        organization.organization_id,
        workspace.workspace_id,
        claim.claim_id,
        status="supported",
        actor_id=owner.user_id,
    )
    run = CheckpointRegistry(store).create_run(
        organization.organization_id,
        workspace.workspace_id,
        kind="artifact_studio",
        created_by=owner.user_id,
        state={"artifact_id": "artifact-phase5", "stage": "validated"},
        schema_manifest={"name": "artifact-studio", "version": 1},
    )
    return ArtifactScope(
        store=store,
        organization_id=organization.organization_id,
        workspace_id=workspace.workspace_id,
        owner_id=owner.user_id,
        reviewer_id=reviewer.user_id,
        source_id=source.source_id,
        source_version_id=version.version_id,
        source_hash=version.content_hash,
        span_id=span.span_id,
        claim_id=claim.claim_id,
        run=run,
    )


def artifact_document(
    value: ArtifactScope,
    *,
    sequence: int = 1,
    parent_hash: str = "",
    paragraph: str = "The supported professional finding.",
) -> ArtifactDocument:
    revisions = [
        RevisionRecord(
            revision_id="revision-1",
            sequence=1,
            author_id=value.owner_id,
            created_at="2026-07-13T21:00:00Z",
            summary="Initial governed artifact",
        )
    ]
    if sequence == 2:
        revisions.append(
            RevisionRecord(
                revision_id="revision-2",
                sequence=2,
                author_id=value.owner_id,
                created_at="2026-07-13T22:00:00Z",
                summary="Reviewed update",
                parent_hash=parent_hash,
            )
        )
    return ArtifactDocument(
        artifact_id="artifact-phase5",
        metadata=DocumentMetadata(
            title="Governed Professional Opinion",
            language="en-US",
            document_type="expert_report",
            confidentiality="confidential",
            organization_id=value.organization_id,
            workspace_id=value.workspace_id,
            created_by=value.owner_id,
            created_at="2026-07-13T21:00:00Z",
            subject="Artifact Matter",
        ),
        sections=(
            Section(
                section_id="findings",
                title="Findings",
                level=1,
                blocks=(
                    ParagraphBlock("finding-1", paragraph, ("citation-1",)),
                    TableBlock(
                        "table-1",
                        ("Claim", "Status"),
                        (("Material finding", "Supported"),),
                        "Claim status",
                        ("citation-1",),
                    ),
                    FigureBlock(
                        "figure-1",
                        "figure-asset-1",
                        "image/png",
                        "A one-pixel governed test figure",
                        "Governed figure",
                        ("citation-1",),
                    ),
                ),
            ),
        ),
        citations=(
            Citation(
                "citation-1",
                value.source_id,
                (value.span_id,),
                (value.claim_id,),
                "paragraph 1",
            ),
        ),
        sources=(
            SourceManifestEntry(
                value.source_id,
                value.source_version_id,
                value.source_hash,
                "Exact evidence source",
                "https://example.test/evidence/source-1",
            ),
        ),
        revisions=tuple(revisions),
    )
