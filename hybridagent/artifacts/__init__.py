"""Governed professional artifact models and validation."""

from hybridagent.artifacts.models import (
    SCHEMA_VERSION,
    ArtifactDocument,
    ArtifactModelError,
    Citation,
    DocumentMetadata,
    FigureBlock,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    ReviewRecord,
    RevisionRecord,
    Section,
    SignatureRecord,
    SourceManifestEntry,
    TableBlock,
)
from hybridagent.artifacts.validation import (
    ArtifactValidationError,
    ValidationIssue,
    ValidationReport,
    validate_document,
    validate_or_raise,
)

__all__ = [
    "SCHEMA_VERSION",
    "ArtifactDocument",
    "ArtifactModelError",
    "ArtifactValidationError",
    "Citation",
    "DocumentMetadata",
    "FigureBlock",
    "ListBlock",
    "PageBreakBlock",
    "ParagraphBlock",
    "ReviewRecord",
    "RevisionRecord",
    "Section",
    "SignatureRecord",
    "SourceManifestEntry",
    "TableBlock",
    "ValidationIssue",
    "ValidationReport",
    "validate_document",
    "validate_or_raise",
]
