"""Deterministic structural validation for canonical artifact documents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from hybridagent.artifacts.models import (
    ArtifactDocument,
    FigureBlock,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    Section,
    TableBlock,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LANG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_ALLOWED_CONFIDENTIALITY = frozenset({"public", "internal", "confidential", "restricted"})
_ALLOWED_REVIEW_TYPES = frozenset({"quality", "professional_release", "research_findings"})
_ALLOWED_DECISIONS = frozenset({"pending", "approved", "revise", "rejected"})
_ALLOWED_MEDIA = frozenset({"image/png", "image/jpeg", "image/svg+xml"})


@dataclass(frozen=True, order=True)
class ValidationIssue:
    path: str
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issues": [
                {"path": issue.path, "code": issue.code,
                 "severity": issue.severity, "message": issue.message}
                for issue in self.issues
            ],
        }


class ArtifactValidationError(ValueError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        details = "; ".join(f"{issue.path}:{issue.code}" for issue in report.issues)
        super().__init__(f"artifact document failed validation: {details}")


def validate_document(document: ArtifactDocument) -> ValidationReport:
    if type(document) is not ArtifactDocument:
        raise TypeError("document must be an exact ArtifactDocument")
    issues: list[ValidationIssue] = []

    def add(path: str, code: str, message: str, severity: str = "error") -> None:
        issues.append(ValidationIssue(path, code, severity, message))

    def required(value: str, path: str, *, limit: int = 4096) -> None:
        if not value.strip():
            add(path, "required", "value must contain non-whitespace text")
        elif len(value) > limit:
            add(path, "too_long", f"value exceeds {limit} characters")

    def identifier(value: str, path: str) -> None:
        if not _ID.fullmatch(value):
            add(path, "invalid_id", "identifier must use portable ASCII identifier syntax")

    identifier(document.artifact_id, "$.artifact_id")
    metadata = document.metadata
    for name in ("title", "language", "document_type", "confidentiality", "organization_id",
                 "workspace_id", "created_by", "created_at"):
        required(getattr(metadata, name), f"$.metadata.{name}")
    if metadata.language and not _LANG.fullmatch(metadata.language):
        add("$.metadata.language", "invalid_language", "language must be a BCP-47-like tag")
    if metadata.confidentiality not in _ALLOWED_CONFIDENTIALITY:
        add("$.metadata.confidentiality", "invalid_confidentiality", "unknown confidentiality marking")

    seen: dict[str, str] = {}

    def unique(value: str, path: str) -> None:
        identifier(value, path)
        prior = seen.get(value)
        if prior is not None:
            add(path, "duplicate_id", f"identifier already used at {prior}")
        else:
            seen[value] = path

    source_ids: set[str] = set()
    for index, source in enumerate(document.sources):
        path = f"$.sources[{index}]"
        unique(source.source_id, f"{path}.source_id")
        source_ids.add(source.source_id)
        for name in ("source_version_id", "content_hash"):
            required(getattr(source, name), f"{path}.{name}")
        identifier(source.source_version_id, f"{path}.source_version_id")
        if not _HASH.fullmatch(source.content_hash):
            add(f"{path}.content_hash", "invalid_hash", "content hash must be lowercase SHA-256")

    citation_ids: set[str] = set()
    for index, citation in enumerate(document.citations):
        path = f"$.citations[{index}]"
        unique(citation.citation_id, f"{path}.citation_id")
        citation_ids.add(citation.citation_id)
        if citation.source_id not in source_ids:
            add(f"{path}.source_id", "dangling_source", "citation source is absent from the manifest")
        if not citation.span_ids:
            add(f"{path}.span_ids", "required", "citation requires at least one exact evidence span")
        for item_index, span_id in enumerate(citation.span_ids):
            identifier(span_id, f"{path}.span_ids[{item_index}]")
        if len(set(citation.span_ids)) != len(citation.span_ids):
            add(f"{path}.span_ids", "duplicate_reference", "citation span IDs must be distinct")
        for item_index, claim_id in enumerate(citation.claim_ids):
            identifier(claim_id, f"{path}.claim_ids[{item_index}]")

    def citation_refs(values: Iterable[str], path: str) -> None:
        values_tuple = tuple(values)
        if len(set(values_tuple)) != len(values_tuple):
            add(path, "duplicate_reference", "citation references must be distinct")
        for index, value in enumerate(values_tuple):
            if value not in citation_ids:
                add(f"{path}[{index}]", "dangling_citation", "block citation does not exist")

    def section(section_value: Section, path: str, appendix: bool) -> None:
        unique(section_value.section_id, f"{path}.section_id")
        required(section_value.title, f"{path}.title", limit=512)
        if not 1 <= section_value.level <= 6:
            add(f"{path}.level", "invalid_level", "section level must be between 1 and 6")
        if appendix and section_value.level != 1:
            add(f"{path}.level", "appendix_level", "top-level appendices must use level 1")
        if not section_value.blocks:
            add(f"{path}.blocks", "empty_section", "section must contain at least one block")
        for block_index, block in enumerate(section_value.blocks):
            block_path = f"{path}.blocks[{block_index}]"
            unique(block.block_id, f"{block_path}.block_id")
            if type(block) is ParagraphBlock:
                required(block.text, f"{block_path}.text", limit=100_000)
                citation_refs(block.citation_ids, f"{block_path}.citation_ids")
            elif type(block) is ListBlock:
                if not block.items:
                    add(f"{block_path}.items", "empty_list", "list must contain at least one item")
                for item_index, item in enumerate(block.items):
                    required(item, f"{block_path}.items[{item_index}]", limit=20_000)
                citation_refs(block.citation_ids, f"{block_path}.citation_ids")
            elif type(block) is TableBlock:
                if not block.columns:
                    add(f"{block_path}.columns", "empty_table", "table requires columns")
                if len(set(block.columns)) != len(block.columns):
                    add(f"{block_path}.columns", "duplicate_column", "table column labels must be distinct")
                for row_index, row in enumerate(block.rows):
                    if len(row) != len(block.columns):
                        add(f"{block_path}.rows[{row_index}]", "row_width", "table row width must match columns")
                if len(block.rows) * max(len(block.columns), 1) > 100_000:
                    add(block_path, "table_too_large", "table exceeds the governed cell limit")
                citation_refs(block.citation_ids, f"{block_path}.citation_ids")
            elif type(block) is FigureBlock:
                required(block.asset_id, f"{block_path}.asset_id", limit=256)
                identifier(block.asset_id, f"{block_path}.asset_id")
                required(block.alt_text, f"{block_path}.alt_text", limit=2_000)
                if block.media_type not in _ALLOWED_MEDIA:
                    add(f"{block_path}.media_type", "invalid_media_type", "unsupported safe figure media type")
                citation_refs(block.citation_ids, f"{block_path}.citation_ids")
            elif type(block) is PageBreakBlock:
                pass
            else:  # pragma: no cover - exact block types are enforced by the model
                add(block_path, "unknown_block", "unsupported block type")

    if not document.sections:
        add("$.sections", "required", "document requires at least one section")
    for index, item in enumerate(document.sections):
        section(item, f"$.sections[{index}]", False)
    for index, item in enumerate(document.appendices):
        section(item, f"$.appendices[{index}]", True)

    revision_sequences: list[int] = []
    for index, revision in enumerate(document.revisions):
        path = f"$.revisions[{index}]"
        unique(revision.revision_id, f"{path}.revision_id")
        revision_sequences.append(revision.sequence)
        for name in ("author_id", "created_at", "summary"):
            required(getattr(revision, name), f"{path}.{name}")
        if revision.sequence == 1 and revision.parent_hash:
            add(f"{path}.parent_hash", "unexpected_parent", "first revision cannot have a parent hash")
        if revision.sequence > 1 and not _HASH.fullmatch(revision.parent_hash):
            add(f"{path}.parent_hash", "invalid_parent", "later revisions require a SHA-256 parent hash")
    if revision_sequences and revision_sequences != list(range(1, len(revision_sequences) + 1)):
        add("$.revisions", "revision_sequence", "revision sequence must be ordered and contiguous from 1")

    review_ids: set[str] = set()
    for index, review in enumerate(document.reviews):
        path = f"$.reviews[{index}]"
        unique(review.review_id, f"{path}.review_id")
        review_ids.add(review.review_id)
        if review.review_type not in _ALLOWED_REVIEW_TYPES:
            add(f"{path}.review_type", "invalid_review_type", "unknown professional review type")
        if review.decision not in _ALLOWED_DECISIONS:
            add(f"{path}.decision", "invalid_review_decision", "unknown review decision")
        for name in ("reviewer_id", "reviewed_at"):
            required(getattr(review, name), f"{path}.{name}")

    signers: set[str] = set()
    for index, signature in enumerate(document.signatures):
        path = f"$.signatures[{index}]"
        unique(signature.signature_id, f"{path}.signature_id")
        for name in ("signer_id", "role", "signed_at", "meaning"):
            required(getattr(signature, name), f"{path}.{name}")
        if signature.signer_id in signers:
            add(f"{path}.signer_id", "duplicate_signer", "signature signers must be distinct")
        signers.add(signature.signer_id)
        if signature.review_id and signature.review_id not in review_ids:
            add(f"{path}.review_id", "dangling_review", "signature review does not exist")

    return ValidationReport(tuple(sorted(issues)))


def validate_or_raise(document: ArtifactDocument) -> ArtifactDocument:
    report = validate_document(document)
    if not report.valid:
        raise ArtifactValidationError(report)
    return document
