"""Shared safety and determinism helpers for artifact renderers."""
from __future__ import annotations

import io
import zipfile
import zlib

from hybridagent.artifacts.models import ArtifactDocument, FigureBlock
from hybridagent.artifacts.validation import validate_or_raise

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 100 * 1024 * 1024
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


class MissingArtifactBackendError(RuntimeError):
    """An explicitly requested optional artifact backend is unavailable."""


class ArtifactRenderError(ValueError):
    """An artifact could not be rendered safely."""


def portable_member_path(path: str) -> bool:
    if type(path) is not str or not path or path.startswith(("/", "\\")) or "\\" in path:
        return False
    for part in path.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(char) < 32 for char in part)
            or part.split(".", 1)[0].rstrip(" .").casefold() in _WINDOWS_RESERVED
        ):
            return False
    return True


def require_backend(module: str, package: str) -> object:
    try:
        return __import__(module)
    except ImportError as exc:
        raise MissingArtifactBackendError(
            f"{package} is required for this renderer; from a Praxis source checkout run "
            'pip install -e ".[artifacts]"'
        ) from exc


def figure_ids(document: ArtifactDocument) -> set[str]:
    return {
        block.asset_id
        for section in (*document.sections, *document.appendices)
        for block in section.blocks
        if type(block) is FigureBlock
    }


def checked_assets(
    document: ArtifactDocument, assets: dict[str, bytes] | None
) -> dict[str, bytes]:
    validate_or_raise(document)
    if assets is None:
        assets = {}
    if type(assets) is not dict:
        raise ArtifactRenderError("figure assets must be an exact dictionary")
    result: dict[str, bytes] = {}
    total = 0
    for key, value in assets.items():
        if type(key) is not str or type(value) is not bytes:
            raise ArtifactRenderError("asset keys and payloads must be exact text and bytes")
        if not portable_member_path(f"assets/{key}") or "/" in key:
            raise ArtifactRenderError("asset ID is not a safe portable name")
        if len(value) > MAX_ASSET_BYTES:
            raise ArtifactRenderError("figure asset exceeds the per-asset size limit")
        total += len(value)
        if total > MAX_TOTAL_ASSET_BYTES:
            raise ArtifactRenderError("figure assets exceed the total size limit")
        result[key] = value
    missing = sorted(figure_ids(document) - result.keys())
    if missing:
        raise ArtifactRenderError(f"figure assets are missing: {missing}")
    expected_kinds = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/svg+xml": "svg",
    }
    for section in (*document.sections, *document.appendices):
        for block in section.blocks:
            if type(block) is not FigureBlock:
                continue
            try:
                actual_kind = image_kind(result[block.asset_id])
            except ArtifactRenderError as exc:
                raise ArtifactRenderError(
                    f"figure asset does not match its declared media type: {block.asset_id}"
                ) from exc
            if actual_kind != expected_kinds[block.media_type]:
                raise ArtifactRenderError(
                    f"figure asset does not match its declared media type: {block.asset_id}"
                )
    return result


def normalize_zip_package(data: bytes) -> bytes:
    """Normalize ZIP member order, metadata, and compression for stable Office output."""
    source = io.BytesIO(data)
    target = io.BytesIO()
    try:
        with zipfile.ZipFile(source, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ArtifactRenderError("generated package contains duplicate members")
            if len(names) != len({name.casefold() for name in names}):
                raise ArtifactRenderError("generated package member paths collide")
            if any(not portable_member_path(name) for name in names):
                raise ArtifactRenderError("generated package contains a nonportable member path")
            members = [(name, archive.read(name)) for name in sorted(names)]
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error) as exc:
        raise ArtifactRenderError("generated Office package is invalid") from exc
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in members:
            info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return target.getvalue()


def image_kind(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if payload.lstrip().startswith(b"<svg") or b"<svg" in payload[:512].lower():
        return "svg"
    raise ArtifactRenderError("figure asset is not a recognized PNG, JPEG, or SVG")
