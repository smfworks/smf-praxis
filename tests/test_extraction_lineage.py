"""Exact evidence-location and derived-extraction lineage contracts."""

import sqlite3

import pytest

from hybridagent.evidence import EvidenceRegistry
from hybridagent.extraction import ExtractionError, ExtractionRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def setup_lineage(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    evidence = EvidenceRegistry(store)
    source = evidence.create_source(
        org.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/report.pdf", publisher="Authority",
        created_by=owner.user_id)
    version = evidence.add_version(
        org.organization_id, workspace.workspace_id, source.source_id,
        content=b"report", mime_type="application/pdf", retrieved_ts=1.0,
        parser="pdf", parser_version="1", parser_config={}, license="permitted",
        original_object_path="objects/report.pdf", created_by=owner.user_id)
    return store, org, owner, workspace, version, ExtractionRegistry(store)


def test_exact_page_and_character_locator_is_preserved(tmp_path):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    span = registry.add_span(
        org.organization_id, workspace.workspace_id, version.version_id,
        locator_type="document", locator={"page": 4, "section": "Findings",
        "paragraph": 2, "char_start": 10, "char_end": 42},
        extracted_text="Exact quoted language", created_by=owner.user_id)
    assert span.locator["page"] == 4
    assert span.locator["char_end"] == 42
    assert registry.get_span(
        org.organization_id, workspace.workspace_id, span.span_id) == span
    assert registry.get_span(org.organization_id, "ws-forged", span.span_id) is None


def test_media_and_repository_locators_validate_shape(tmp_path):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    media = registry.add_span(
        org.organization_id, workspace.workspace_id, version.version_id,
        locator_type="media", locator={"start_seconds": 12.5, "end_seconds": 15.0},
        extracted_text="Spoken statement", created_by=owner.user_id)
    assert media.locator_type == "media"
    with pytest.raises(ExtractionError, match="repository locator"):
        registry.add_span(
            org.organization_id, workspace.workspace_id, version.version_id,
            locator_type="repository", locator={"path": "src/app.py"},
            extracted_text="code", created_by=owner.user_id)


def test_derived_artifact_links_to_original_span_and_extractor(tmp_path):
    store, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    original = registry.add_span(
        org.organization_id, workspace.workspace_id, version.version_id,
        locator_type="image", locator={"page": 2, "bbox": [0.1, 0.2, 0.8, 0.9]},
        extracted_text="", created_by=owner.user_id)
    derived = registry.add_derived_artifact(
        org.organization_id, workspace.workspace_id, original.span_id,
        kind="ocr", content="Detected text", extractor="tesseract",
        extractor_version="5.4", configuration={"language": "eng"},
        created_by=owner.user_id)
    assert derived.parent_span_id == original.span_id
    assert derived.extractor_version == "5.4"
    restarted = ExtractionRegistry(Store(store.path))
    assert restarted.get_derived(
        org.organization_id, workspace.workspace_id, derived.artifact_id) == derived
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._directory_execute(
            "UPDATE evidence_derived_artifacts SET content='changed' WHERE artifact_id=?",
            (derived.artifact_id,))


@pytest.mark.parametrize("locator", [
    {"page": None}, {"page": -1}, {"section": ""}, {"paragraph": True},
])
def test_document_locator_rejects_non_exact_values(tmp_path, locator):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    with pytest.raises(ExtractionError):
        registry.add_span(
            org.organization_id, workspace.workspace_id, version.version_id,
            locator_type="document", locator=locator, extracted_text="bad",
            created_by=owner.user_id)
