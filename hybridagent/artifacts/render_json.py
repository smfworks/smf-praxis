"""Byte-deterministic canonical JSON artifact renderer."""

from hybridagent.artifacts.models import ArtifactDocument
from hybridagent.artifacts.validation import validate_or_raise


def render_json(document: ArtifactDocument) -> bytes:
    validate_or_raise(document)
    return document.canonical_bytes()
