"""Phase 5 contracts for the canonical professional document IR."""
from __future__ import annotations

from dataclasses import replace

import pytest

from hybridagent.artifacts import (
    ArtifactDocument,
    ArtifactModelError,
    ArtifactValidationError,
    Citation,
    DocumentMetadata,
    FigureBlock,
    ListBlock,
    ParagraphBlock,
    ReviewRecord,
    RevisionRecord,
    Section,
    SignatureRecord,
    SourceManifestEntry,
    TableBlock,
    validate_document,
    validate_or_raise,
)

_HASH = "a" * 64


def document() -> ArtifactDocument:
    return ArtifactDocument(
        artifact_id="artifact-1",
        metadata=DocumentMetadata(
            title="Professional opinion",
            language="en-US",
            document_type="expert_report",
            confidentiality="confidential",
            organization_id="org-1",
            workspace_id="workspace-1",
            created_by="user-1",
            created_at="2026-07-13T21:00:00Z",
            subject="Example matter",
        ),
        sections=(
            Section(
                "section-1",
                "Findings",
                1,
                (
                    ParagraphBlock("block-1", "The supported finding.", ("citation-1",)),
                    ListBlock("block-2", ("First", "Second"), True),
                    TableBlock("block-3", ("Item", "Result"), (("A", "Pass"),), "Results"),
                    FigureBlock("block-4", "asset-1", "image/png", "Inspection overview"),
                ),
            ),
        ),
        citations=(Citation("citation-1", "source-1", ("span-1",), ("claim-1",), "p. 2"),),
        sources=(SourceManifestEntry("source-1", "version-1", _HASH, "Source", "https://example.test/source"),),
        revisions=(RevisionRecord("revision-1", 1, "user-1", "2026-07-13T21:00:00Z", "Initial"),),
        reviews=(ReviewRecord("review-1", "professional_release", "approved", "reviewer-1", "2026-07-13T21:10:00Z"),),
        signatures=(SignatureRecord("signature-1", "reviewer-1", "reviewer", "2026-07-13T21:11:00Z", "approved for release", "review-1"),),
    )


def test_valid_document_round_trips_canonical_json_and_hash() -> None:
    original = document()
    validate_or_raise(original)
    encoded = original.canonical_json()
    decoded = ArtifactDocument.from_json(encoded)
    assert decoded == original
    assert decoded.canonical_json() == encoded
    assert decoded.content_hash() == original.content_hash()
    assert len(original.content_hash()) == 64


def test_canonical_model_normalizes_unicode_to_nfc() -> None:
    decomposed = replace(document().metadata, title="Cafe\u0301")
    normalized = replace(document().metadata, title="Caf\u00e9")
    first = replace(document(), metadata=decomposed)
    second = replace(document(), metadata=normalized)
    assert first.metadata.title == "Caf\u00e9"
    assert first.canonical_bytes() == second.canonical_bytes()


def test_decoder_fails_closed_on_unknown_fields_and_schema_versions() -> None:
    payload = document().to_dict()
    payload["surprise"] = True
    with pytest.raises(ArtifactModelError, match="unknown fields"):
        ArtifactDocument.from_dict(payload)
    payload = document().to_dict()
    payload["schema_version"] = 2
    with pytest.raises(ArtifactModelError, match="unsupported"):
        ArtifactDocument.from_dict(payload)


def test_decoder_rejects_bool_as_int_float_and_subclassed_text() -> None:
    payload = document().to_dict()
    payload["schema_version"] = True
    with pytest.raises(ArtifactModelError, match="exact integer"):
        ArtifactDocument.from_dict(payload)
    payload = document().to_dict()
    payload["metadata"]["created_at"] = 1.5
    with pytest.raises(ArtifactModelError, match="exact text"):
        ArtifactDocument.from_dict(payload)

    class Text(str):
        pass

    with pytest.raises(ArtifactModelError, match="exact text"):
        replace(document().metadata, title=Text("forged"))


def test_block_union_decoder_rejects_unknown_type_and_unknown_properties() -> None:
    payload = document().to_dict()
    payload["sections"][0]["blocks"][0]["type"] = "html"
    with pytest.raises(ArtifactModelError, match="unknown artifact block type"):
        ArtifactDocument.from_dict(payload)
    payload = document().to_dict()
    payload["sections"][0]["blocks"][0]["script"] = "alert(1)"
    with pytest.raises(ArtifactModelError, match="unknown fields"):
        ArtifactDocument.from_dict(payload)


def test_validation_report_is_deterministic_and_machine_readable() -> None:
    invalid = replace(
        document(),
        sections=(Section("section-1", "", 9, (ParagraphBlock("block-1", "", ("missing",)),)),),
    )
    first = validate_document(invalid)
    second = validate_document(invalid)
    assert first == second
    assert first.valid is False
    assert first.issues == tuple(sorted(first.issues))
    assert {issue.code for issue in first.issues} >= {"required", "invalid_level", "dangling_citation"}
    assert first.to_dict()["valid"] is False
    with pytest.raises(ArtifactValidationError) as caught:
        validate_or_raise(invalid)
    assert caught.value.report == first


def test_validation_requires_exact_source_and_citation_references() -> None:
    invalid = replace(
        document(),
        citations=(Citation("citation-1", "missing-source", (), ()),),
        sources=(),
    )
    codes = {issue.code for issue in validate_document(invalid).issues}
    assert {"dangling_source", "required"} <= codes


def test_validation_rejects_duplicate_ids_and_references() -> None:
    invalid = replace(
        document(),
        sections=(
            Section("same", "One", 1, (ParagraphBlock("same", "text", ("citation-1", "citation-1")),)),
        ),
    )
    codes = [issue.code for issue in validate_document(invalid).issues]
    assert "duplicate_id" in codes
    assert "duplicate_reference" in codes


def test_validation_rejects_bad_tables_figures_and_empty_sections() -> None:
    invalid = replace(
        document(),
        sections=(
            Section("section-1", "Bad", 1, (
                TableBlock("table-1", ("A", "A"), (("only-one",),)),
                FigureBlock("figure-1", "asset-1", "text/html", ""),
            )),
            Section("section-2", "Empty", 1, ()),
        ),
    )
    codes = {issue.code for issue in validate_document(invalid).issues}
    assert {"duplicate_column", "row_width", "invalid_media_type", "required", "empty_section"} <= codes


def test_revision_history_must_be_contiguous_and_parent_linked() -> None:
    invalid = replace(
        document(),
        revisions=(
            RevisionRecord("revision-1", 1, "user-1", "now", "first", _HASH),
            RevisionRecord("revision-3", 3, "user-1", "later", "third", "bad"),
        ),
    )
    codes = {issue.code for issue in validate_document(invalid).issues}
    assert {"unexpected_parent", "invalid_parent", "revision_sequence"} <= codes


def test_signatures_require_distinct_signers_and_existing_review() -> None:
    invalid = replace(
        document(),
        signatures=(
            SignatureRecord("sig-1", "same-user", "reviewer", "now", "approved", "missing"),
            SignatureRecord("sig-2", "same-user", "professional", "now", "approved", "review-1"),
        ),
    )
    codes = {issue.code for issue in validate_document(invalid).issues}
    assert {"duplicate_signer", "dangling_review"} <= codes


def test_appendix_heading_level_is_explicit() -> None:
    invalid = replace(
        document(),
        appendices=(Section("appendix-1", "Appendix", 2, (ParagraphBlock("appendix-block", "text"),)),),
    )
    assert "appendix_level" in {issue.code for issue in validate_document(invalid).issues}


def test_validation_rejects_artifact_document_subclasses() -> None:
    class ForgedDocument(ArtifactDocument):
        pass

    base = document()
    forged = ForgedDocument(**base.__dict__)
    with pytest.raises(TypeError, match="exact ArtifactDocument"):
        validate_document(forged)
