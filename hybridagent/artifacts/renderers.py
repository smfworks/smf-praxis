"""Deterministic artifact renderer registry with lazy optional backends."""
from __future__ import annotations

from collections.abc import Mapping

from hybridagent.artifacts.models import ArtifactDocument
from hybridagent.artifacts.render_common import ArtifactRenderError
from hybridagent.artifacts.render_docx import render_docx
from hybridagent.artifacts.render_json import render_json
from hybridagent.artifacts.render_markdown import render_markdown
from hybridagent.artifacts.render_pdf import render_pdf
from hybridagent.artifacts.render_pptx import render_pptx
from hybridagent.artifacts.render_xlsx import render_xlsx

_FORMAT_EXTENSIONS = {
    "json": "json",
    "markdown": "md",
    "docx": "docx",
    "pdf": "pdf",
    "pptx": "pptx",
    "xlsx": "xlsx",
}


def supported_formats() -> tuple[str, ...]:
    return tuple(_FORMAT_EXTENSIONS)


def extension_for(format_name: str) -> str:
    if type(format_name) is not str or format_name not in _FORMAT_EXTENSIONS:
        raise ArtifactRenderError(f"unsupported artifact format: {format_name!r}")
    return _FORMAT_EXTENSIONS[format_name]


def render_artifact(
    document: ArtifactDocument,
    format_name: str,
    assets: Mapping[str, bytes] | None = None,
) -> bytes:
    extension_for(format_name)
    if assets is not None and type(assets) is not dict:
        raise ArtifactRenderError("renderer assets must be an exact dictionary")
    exact_assets = assets if type(assets) is dict else None
    if format_name == "json":
        return render_json(document)
    if format_name == "markdown":
        return render_markdown(document)
    if format_name == "docx":
        return render_docx(document, exact_assets)
    if format_name == "pdf":
        return render_pdf(document, exact_assets)
    if format_name == "pptx":
        return render_pptx(document, exact_assets)
    return render_xlsx(document, exact_assets)
