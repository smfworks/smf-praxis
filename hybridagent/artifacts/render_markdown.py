"""Byte-deterministic Markdown renderer for professional artifacts."""
from __future__ import annotations

import json

from hybridagent.artifacts.models import (
    ArtifactDocument,
    FigureBlock,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    Section,
    TableBlock,
)
from hybridagent.artifacts.validation import validate_or_raise


def _escape(value: str) -> str:
    return (_safe_text(value).replace("\\", "\\\\").replace("|", "\\|")
            .replace("\n", " "))


def _safe_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _citations(values: tuple[str, ...]) -> str:
    return "" if not values else " " + " ".join(f"[^{value}]" for value in values)


def _section(lines: list[str], section: Section, *, appendix: bool = False) -> None:
    level = min(6, max(1, section.level + (1 if appendix else 0)))
    lines.extend((f"{'#' * level} {_safe_text(section.title)}", ""))
    for block in section.blocks:
        if type(block) is ParagraphBlock:
            lines.extend((_safe_text(block.text) + _citations(block.citation_ids), ""))
        elif type(block) is ListBlock:
            for index, item in enumerate(block.items, 1):
                marker = f"{index}." if block.ordered else "-"
                suffix = _citations(block.citation_ids) if index == len(block.items) else ""
                lines.append(f"{marker} {_safe_text(item)}{suffix}")
            lines.append("")
        elif type(block) is TableBlock:
            if block.caption:
                lines.extend((f"**{_safe_text(block.caption)}**{_citations(block.citation_ids)}", ""))
            lines.append("| " + " | ".join(_escape(item) for item in block.columns) + " |")
            lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
            for row in block.rows:
                lines.append("| " + " | ".join(_escape(item) for item in row) + " |")
            lines.append("")
        elif type(block) is FigureBlock:
            caption = block.caption or block.alt_text
            lines.extend((f"![{_escape(block.alt_text)}](assets/{block.asset_id})",
                          f"*{_safe_text(caption)}*{_citations(block.citation_ids)}", ""))
        elif type(block) is PageBreakBlock:
            lines.extend(("<!-- page-break -->", ""))


def render_markdown(document: ArtifactDocument) -> bytes:
    validate_or_raise(document)
    metadata = document.metadata
    lines = [
        "---",
        f"artifact_id: {_yaml(document.artifact_id)}",
        f"title: {_yaml(metadata.title)}",
        f"language: {_yaml(metadata.language)}",
        f"document_type: {_yaml(metadata.document_type)}",
        f"confidentiality: {_yaml(metadata.confidentiality)}",
        f'content_sha256: "{document.content_hash()}"',
        "---",
        "",
        f"# {_safe_text(metadata.title)}",
        "",
        f"**Classification:** {metadata.confidentiality.upper()}",
        "",
    ]
    if metadata.subject:
        lines.extend((f"**Subject:** {_safe_text(metadata.subject)}", ""))
    for section in document.sections:
        _section(lines, section)
    if document.appendices:
        lines.extend(("# Appendices", ""))
        for section in document.appendices:
            _section(lines, section, appendix=True)
    if document.citations:
        lines.extend(("# Citations", ""))
        for citation in document.citations:
            spans = ", ".join(citation.span_ids)
            claims = f"; claims: {', '.join(citation.claim_ids)}" if citation.claim_ids else ""
            pinpoint = f"; {_safe_text(citation.pinpoint)}" if citation.pinpoint else ""
            lines.append(
                f"[^{citation.citation_id}]: source `{citation.source_id}`; spans: {spans}{claims}{pinpoint}"
            )
        lines.append("")
    if document.sources:
        lines.extend(("# Source Manifest", "", "| Source | Version | SHA-256 | Title | URI |",
                      "| --- | --- | --- | --- | --- |"))
        for source in document.sources:
            lines.append("| " + " | ".join(_escape(value) for value in (
                source.source_id, source.source_version_id, source.content_hash,
                source.title, source.canonical_uri,
            )) + " |")
        lines.append("")
    if document.revisions:
        lines.extend(("# Revision History", ""))
        for revision in document.revisions:
            lines.append(f"- **{revision.sequence}** `{revision.revision_id}` — {_safe_text(revision.summary)} "
                         f"({_safe_text(revision.author_id)}, {_safe_text(revision.created_at)})")
        lines.append("")
    if document.reviews:
        lines.extend(("# Professional Reviews", ""))
        for review in document.reviews:
            lines.append(f"- `{review.review_id}` {review.review_type}: **{review.decision}** — "
                         f"{_safe_text(review.reviewer_id)}, {_safe_text(review.reviewed_at)}")
        lines.append("")
    if document.signatures:
        lines.extend(("# Signatures", ""))
        for signature in document.signatures:
            lines.append(f"- `{signature.signature_id}` {_safe_text(signature.signer_id)} ({_safe_text(signature.role)}) — "
                         f"{_safe_text(signature.meaning)}, {_safe_text(signature.signed_at)}")
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")
