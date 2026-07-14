"""Phase 5 append-only artifact versioning, CAS, diff, and tenant contracts."""
from __future__ import annotations

import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from hybridagent.artifacts import ArtifactDocument, Section, SourceManifestEntry
from hybridagent.artifacts.render_common import ArtifactRenderError
from hybridagent.artifacts.service import ArtifactServiceError, ArtifactStudio
from hybridagent.artifacts.versions import compare_documents
from hybridagent.claims import ClaimLedger
from hybridagent.evidence import EvidenceRegistry
from hybridagent.extraction import ExtractionRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory
from tests.artifact_helpers import PNG, ArtifactScope, artifact_document, scope


def _race_artifact_version(
    database: str,
    organization_id: str,
    workspace_id: str,
    owner_id: str,
    document_json: str,
    parent_version_id: str,
    barrier,
    results,
) -> None:
    store = Store(Path(database))
    studio = ArtifactStudio(store)
    document = ArtifactDocument.from_json(document_json)
    barrier.wait()
    try:
        version = studio.create_version(
            organization_id,
            workspace_id,
            document,
            created_by=owner_id,
            assets={"figure-asset-1": PNG},
            expected_parent_version_id=parent_version_id,
        )
    except ArtifactServiceError as exc:
        results.put(("rejected", str(exc)))
    else:
        results.put(("created", version.version_id))
    finally:
        store.close()


def _second_workspace_scope(value: ArtifactScope) -> ArtifactScope:
    workspace = WorkspaceDirectory(value.store).create(
        value.organization_id,
        "MAT-ART-2",
        "matter",
        "Second Artifact Matter",
        owner_user_id=value.owner_id,
    )
    evidence = EvidenceRegistry(value.store)
    source = evidence.create_source(
        value.organization_id,
        workspace.workspace_id,
        canonical_uri="https://example.test/evidence/source-2",
        publisher="Example Publisher",
        created_by=value.owner_id,
    )
    source_version = evidence.add_version(
        value.organization_id,
        workspace.workspace_id,
        source.source_id,
        content=b"The second exact supported finding.",
        mime_type="text/plain",
        retrieved_ts=time.time(),
        parser="fixture",
        parser_version="1",
        parser_config={},
        license="test",
        original_object_path="evidence/source-2.txt",
        created_by=value.owner_id,
    )
    span = ExtractionRegistry(value.store).add_span(
        value.organization_id,
        workspace.workspace_id,
        source_version.version_id,
        locator_type="document",
        locator={"paragraph": 1},
        extracted_text="The second exact supported finding.",
        created_by=value.owner_id,
    )
    claims = ClaimLedger(value.store)
    claim = claims.create(
        value.organization_id,
        workspace.workspace_id,
        text="The second finding is supported.",
        material=True,
        created_by=value.owner_id,
    )
    claims.link_evidence(
        value.organization_id,
        workspace.workspace_id,
        claim.claim_id,
        span.span_id,
        relationship="supports",
        rationale="The second exact span supports the claim.",
        created_by=value.owner_id,
    )
    claims.set_status(
        value.organization_id,
        workspace.workspace_id,
        claim.claim_id,
        status="supported",
        actor_id=value.owner_id,
    )
    return ArtifactScope(
        store=value.store,
        organization_id=value.organization_id,
        workspace_id=workspace.workspace_id,
        owner_id=value.owner_id,
        reviewer_id=value.reviewer_id,
        source_id=source.source_id,
        source_version_id=source_version.version_id,
        source_hash=source_version.content_hash,
        span_id=span.span_id,
        claim_id=claim.claim_id,
        run=value.run,
    )


def test_create_get_render_and_reopen_canonical_artifact_version(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    document = artifact_document(value)
    version = studio.create_version(
        value.organization_id,
        value.workspace_id,
        document,
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
    )
    assert version.sequence == 1
    assert version.parent_version_id == ""
    assert version.document_hash == document.content_hash()
    assert version.document == document
    assert version.assets[0].asset_id == "figure-asset-1"
    assert studio.render_version(
        value.organization_id, value.workspace_id, version.version_id, "json"
    ) == document.canonical_bytes()
    assert b"Governed Professional Opinion" in studio.render_version(
        value.organization_id, value.workspace_id, version.version_id, "markdown"
    )

    value.store.close()
    reopened = Store(tmp_path / "praxis.db")
    durable = ArtifactStudio(reopened).get_version(
        value.organization_id, value.workspace_id, version.version_id
    )
    assert durable is not None and durable.document_hash == version.document_hash
    reopened.close()


def test_version_head_cas_revision_chain_and_semantic_diff(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    first = studio.create_version(
        value.organization_id, value.workspace_id, artifact_document(value),
        created_by=value.owner_id, assets={"figure-asset-1": PNG},
    )
    second_document = artifact_document(
        value,
        sequence=2,
        parent_hash=first.document_hash,
        paragraph="The supported professional finding was independently reviewed.",
    )
    second = studio.create_version(
        value.organization_id,
        value.workspace_id,
        second_document,
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
        expected_parent_version_id=first.version_id,
    )
    assert second.sequence == 2
    assert second.parent_version_id == first.version_id
    diff = studio.compare(
        value.organization_id, value.workspace_id, first.version_id, second.version_id
    )
    assert diff.changed is True
    assert diff.changed_blocks == ("finding-1",)
    assert diff.governance_fields == ("revisions",)
    with pytest.raises(ArtifactServiceError, match="stale"):
        studio.create_version(
            value.organization_id,
            value.workspace_id,
            second_document,
            created_by=value.owner_id,
            assets={"figure-asset-1": PNG},
            expected_parent_version_id=first.version_id,
        )


def test_version_append_preserves_revision_prefix_and_head_moves_forward_only(
    tmp_path: Path,
) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    first = studio.create_version(
        value.organization_id,
        value.workspace_id,
        artifact_document(value),
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
    )
    candidate = artifact_document(
        value, sequence=2, parent_hash=first.document_hash, paragraph="Second version"
    )
    forged = replace(
        candidate,
        revisions=(
            replace(candidate.revisions[0], summary="Fabricated historical summary"),
            candidate.revisions[1],
        ),
    )
    with pytest.raises(ArtifactServiceError, match="revision history prefix"):
        studio.create_version(
            value.organization_id,
            value.workspace_id,
            forged,
            created_by=value.owner_id,
            assets={"figure-asset-1": PNG},
            expected_parent_version_id=first.version_id,
        )

    second = studio.create_version(
        value.organization_id,
        value.workspace_id,
        candidate,
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
        expected_parent_version_id=first.version_id,
    )
    assert second.sequence == 2
    with pytest.raises(sqlite3.IntegrityError, match="forward"):
        value.store._directory_execute(
            "UPDATE artifact_documents SET head_version_id=?,updated_ts=? "
            "WHERE artifact_id=? AND organization_id=? AND workspace_id=?",
            (
                first.version_id,
                time.time(),
                first.artifact_id,
                value.organization_id,
                value.workspace_id,
            ),
        )


def test_semantic_diff_detects_identity_order_and_section_container_changes(
    tmp_path: Path,
) -> None:
    value = scope(tmp_path)
    original = artifact_document(value)

    identity_diff = compare_documents(
        "before",
        original,
        "after",
        replace(original, artifact_id="artifact-renamed"),
    )
    assert identity_diff.changed is True
    assert identity_diff.document_fields == ("artifact_id",)

    findings = original.sections[0]
    methods = Section("methods", "Methods", 1, ())
    ordered = replace(original, sections=(findings, methods))
    reordered = replace(ordered, sections=(methods, findings))
    order_diff = compare_documents("before", ordered, "after", reordered)
    assert order_diff.changed is True
    assert order_diff.changed_sections == ("findings", "methods")

    moved = replace(ordered, sections=(findings,), appendices=(methods,))
    container_diff = compare_documents("before", ordered, "after", moved)
    assert container_diff.changed is True
    assert container_diff.changed_sections == ("methods",)


def test_semantic_diff_preserves_duplicate_identifier_occurrences(tmp_path: Path) -> None:
    value = scope(tmp_path)
    original = artifact_document(value)
    section = original.sections[0]
    duplicate_section = replace(section, title="Stable duplicate section")
    before = replace(original, sections=(section, duplicate_section))
    after = replace(
        before,
        sections=(replace(section, title="Changed shadowed section"), duplicate_section),
    )
    section_diff = compare_documents("before", before, "after", after)
    assert section_diff.changed is True
    assert section_diff.changed_sections == (section.section_id,)

    block = section.blocks[0]
    duplicate_block = replace(block, text="Stable duplicate block")
    before = replace(original, sections=(replace(section, blocks=(block, duplicate_block)),))
    after = replace(
        before,
        sections=(replace(section, blocks=(replace(block, text="Changed shadowed block"), duplicate_block)),),
    )
    block_diff = compare_documents("before", before, "after", after)
    assert block_diff.changed is True
    assert block_diff.changed_blocks == (block.block_id,)

    citation = original.citations[0]
    stable_citation = replace(citation, pinpoint="stable duplicate")
    before = replace(original, citations=(citation, stable_citation))
    after = replace(original, citations=(replace(citation, pinpoint="changed shadowed"), stable_citation))
    citation_diff = compare_documents("before", before, "after", after)
    assert citation_diff.changed is True
    assert citation_diff.changed_citations == (citation.citation_id,)

    source = original.sources[0]
    stable_source = replace(source, title="Stable duplicate source")
    before = replace(original, sources=(source, stable_source))
    after = replace(original, sources=(replace(source, title="Changed shadowed source"), stable_source))
    source_diff = compare_documents("before", before, "after", after)
    assert source_diff.changed is True
    assert source_diff.changed_sources == (source.source_id,)


def test_concurrent_same_head_version_writers_have_one_winner(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    first = studio.create_version(
        value.organization_id, value.workspace_id, artifact_document(value),
        created_by=value.owner_id, assets={"figure-asset-1": PNG},
    )
    next_document = artifact_document(
        value, sequence=2, parent_hash=first.document_hash, paragraph="Concurrent update"
    )

    def write() -> str:
        try:
            result = studio.create_version(
                value.organization_id,
                value.workspace_id,
                next_document,
                created_by=value.owner_id,
                assets={"figure-asset-1": PNG},
                expected_parent_version_id=first.version_id,
            )
            return result.version_id
        except ArtifactServiceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: write(), range(2)))
    assert sum(item.startswith("artifact-version-") for item in results) == 1
    assert sum("stale" in item for item in results) == 1
    assert len(studio.list_versions(value.organization_id, value.workspace_id, "artifact-phase5")) == 2


def test_cross_process_same_head_version_writers_have_one_winner(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    first = studio.create_version(
        value.organization_id,
        value.workspace_id,
        artifact_document(value),
        created_by=value.owner_id,
        assets={"figure-asset-1": PNG},
    )
    next_document = artifact_document(
        value,
        sequence=2,
        parent_hash=first.document_hash,
        paragraph="Cross-process update",
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    args = (
        str(tmp_path / "praxis.db"),
        value.organization_id,
        value.workspace_id,
        value.owner_id,
        next_document.canonical_json(),
        first.version_id,
        barrier,
        results,
    )
    processes = [context.Process(target=_race_artifact_version, args=args) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert sorted(status for status, _ in outcomes) == ["created", "rejected"]
    rejected = next(detail for status, detail in outcomes if status == "rejected")
    assert "stale" in rejected
    assert len(studio.list_versions(value.organization_id, value.workspace_id, "artifact-phase5")) == 2


def test_versions_fail_closed_on_scope_assets_sources_and_revision_contracts(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    document = artifact_document(value)
    with pytest.raises(ArtifactRenderError, match="declared media type"):
        studio.create_version(
            value.organization_id,
            value.workspace_id,
            document,
            created_by=value.owner_id,
            assets={"figure-asset-1": b"NOT-A-PNG"},
        )
    with pytest.raises(ArtifactServiceError, match="exactly match"):
        studio.create_version(
            value.organization_id, value.workspace_id, document,
            created_by=value.owner_id, assets={"figure-asset-1": PNG, "extra": b"x"},
        )
    wrong_source = replace(
        document,
        sources=(SourceManifestEntry(
            value.source_id, value.source_version_id, "f" * 64,
            "Tampered source", "https://example.test/evidence/source-1",
        ),),
    )
    with pytest.raises(ArtifactServiceError, match="immutable evidence"):
        studio.create_version(
            value.organization_id, value.workspace_id, wrong_source,
            created_by=value.owner_id, assets={"figure-asset-1": PNG},
        )
    no_revision = replace(document, revisions=())
    with pytest.raises(ArtifactServiceError, match="revision history"):
        studio.create_version(
            value.organization_id, value.workspace_id, no_revision,
            created_by=value.owner_id, assets={"figure-asset-1": PNG},
        )


def test_two_workspaces_can_use_the_same_logical_artifact_id(tmp_path: Path) -> None:
    first_scope = scope(tmp_path)
    second_scope = _second_workspace_scope(first_scope)
    studio = ArtifactStudio(first_scope.store)
    first = studio.create_version(
        first_scope.organization_id,
        first_scope.workspace_id,
        artifact_document(first_scope),
        created_by=first_scope.owner_id,
        assets={"figure-asset-1": PNG},
    )
    second = studio.create_version(
        second_scope.organization_id,
        second_scope.workspace_id,
        artifact_document(second_scope),
        created_by=second_scope.owner_id,
        assets={"figure-asset-1": PNG},
    )
    assert first.artifact_id == second.artifact_id == "artifact-phase5"
    assert studio.list_versions(
        first_scope.organization_id, first_scope.workspace_id, first.artifact_id
    ) == [first]
    assert studio.list_versions(
        second_scope.organization_id, second_scope.workspace_id, second.artifact_id
    ) == [second]


def test_artifact_versions_are_tenant_scoped_and_storage_is_immutable(tmp_path: Path) -> None:
    value = scope(tmp_path)
    studio = ArtifactStudio(value.store)
    version = studio.create_version(
        value.organization_id, value.workspace_id, artifact_document(value),
        created_by=value.owner_id, assets={"figure-asset-1": PNG},
    )
    organizations = OrganizationDirectory(value.store)
    other_org, other_owner = organizations.bootstrap("Other Practice", "other@example.com")
    other_workspace = WorkspaceDirectory(value.store).create(
        other_org.organization_id, "OTHER-1", "matter", "Other",
        owner_user_id=other_owner.user_id,
    )
    assert studio.get_version(
        other_org.organization_id, other_workspace.workspace_id, version.version_id
    ) is None
    with pytest.raises(ArtifactServiceError, match="does not exist"):
        studio.render_version(
            other_org.organization_id, other_workspace.workspace_id,
            version.version_id, "json",
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        value.store._directory_execute(
            "UPDATE artifact_versions SET document_hash=? WHERE version_id=?",
            ("0" * 64, version.version_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        value.store._directory_execute(
            "DELETE FROM artifact_version_assets WHERE version_id=?",
            (version.version_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="head must move forward"):
        value.store._directory_execute(
            "UPDATE artifact_documents SET title='rewritten' WHERE artifact_id=?",
            (version.artifact_id,),
        )
