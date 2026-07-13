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
from hybridagent.artifacts.render_common import (
    ArtifactRenderError,
    MissingArtifactBackendError,
)
from hybridagent.artifacts.renderers import (
    extension_for,
    render_artifact,
    supported_formats,
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
    "ArtifactRenderError",
    "ArtifactValidationError",
    "Citation",
    "DocumentMetadata",
    "FigureBlock",
    "ListBlock",
    "MissingArtifactBackendError",
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
    "extension_for",
    "render_artifact",
    "supported_formats",
    "validate_document",
    "validate_or_raise",
]
