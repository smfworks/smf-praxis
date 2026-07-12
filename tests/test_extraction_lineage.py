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


@pytest.mark.parametrize(("locator_type", "locator"), [
    ("document", {"char_start": False, "char_end": 1}),
    ("document", {"char_start": 0, "char_end": True}),
    ("image", {"bbox": [False, 0, 1, 1]}),
    ("image", {"bbox": [0, 0, float("nan"), 1]}),
    ("image", {"bbox": [0, 0, float("inf"), 1]}),
    ("image", {"bbox": [1, 1, 0, 0]}),
    ("media", {"start_seconds": False, "end_seconds": 1}),
    ("media", {"start_seconds": 0, "end_seconds": True}),
    ("media", {"start_seconds": 0, "end_seconds": float("nan")}),
    ("media", {"start_seconds": 0, "end_seconds": float("inf")}),
    ("repository", {"commit": "abc", "path": "x.py",
                    "line_start": False, "line_end": 1}),
    ("repository", {"commit": "abc", "path": "x.py",
                    "line_start": -1, "line_end": 1}),
])
def test_numeric_locators_reject_booleans_and_invalid_ranges(
        tmp_path, locator_type, locator):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    with pytest.raises(ExtractionError):
        registry.add_span(
            org.organization_id, workspace.workspace_id, version.version_id,
            locator_type=locator_type, locator=locator, extracted_text="bad",
            created_by=owner.user_id)


@pytest.mark.parametrize(("locator_type", "locator"), [
    ("table", {"table": 1, "cell": 2}),
    ("table", {"table": ["A"], "cell": {"row": 1}}),
    ("table", {"table": " ", "cell": "A1"}),
    ("repository", {"commit": 1, "path": 2,
                    "line_start": 1, "line_end": 1}),
    ("repository", {"commit": ["abc"], "path": {"file": "x.py"},
                    "line_start": 1, "line_end": 1}),
    ("repository", {"commit": " ", "path": "x.py",
                    "line_start": 1, "line_end": 1}),
])
def test_text_locators_require_non_empty_strings(tmp_path, locator_type, locator):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    with pytest.raises(ExtractionError):
        registry.add_span(
            org.organization_id, workspace.workspace_id, version.version_id,
            locator_type=locator_type, locator=locator, extracted_text="bad",
            created_by=owner.user_id)


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


@pytest.mark.parametrize("extra", [
    float("nan"), float("inf"), float("-inf"),
    {"nested": float("nan")}, [0, float("inf")],
])
def test_locator_rejects_non_standard_json_anywhere(tmp_path, extra):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    with pytest.raises(ExtractionError, match="JSON serializable"):
        registry.add_span(
            org.organization_id, workspace.workspace_id, version.version_id,
            locator_type="repository",
            locator={"commit": "abc", "path": "x.py", "line_start": 1,
                     "line_end": 1, "extra": extra},
            extracted_text="bad", created_by=owner.user_id)


def test_derived_configuration_rejects_non_standard_json(tmp_path):
    _, org, owner, workspace, version, registry = setup_lineage(tmp_path)
    span = registry.add_span(
        org.organization_id, workspace.workspace_id, version.version_id,
        locator_type="document", locator={"page": 1}, extracted_text="text",
        created_by=owner.user_id)
    with pytest.raises(ExtractionError, match="JSON serializable"):
        registry.add_derived_artifact(
            org.organization_id, workspace.workspace_id, span.span_id,
            kind="extraction", content="derived", extractor="test",
            extractor_version="1", configuration={"threshold": float("nan")},
            created_by=owner.user_id)
