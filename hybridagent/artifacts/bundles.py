"""Deterministic, self-verifying professional artifact release bundles."""
from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from typing import Any, cast

from hybridagent.artifacts.models import ArtifactDocument, ArtifactModelError
from hybridagent.artifacts.renderers import extension_for, supported_formats
from hybridagent.artifacts.validation import ArtifactValidationError, validate_or_raise

BUNDLE_SCHEMA_VERSION = 1
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_MAX_MEMBERS = 1_000
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
_REQUIRED_GOVERNANCE = frozenset(
    {"claims", "evidence", "reviews", "signatures", "run"}
)
_RELEASE_CONTEXT_FIELDS = frozenset(
    {
        "release_id",
        "artifact_id",
        "version_id",
        "organization_id",
        "workspace_id",
        "document_sha256",
        "formats",
        "run_id",
        "checkpoint_id",
        "idempotency_key",
        "request_sha256",
        "created_by",
        "created_ts",
    }
)
_HEX = frozenset("0123456789abcdef")


class ArtifactBundleError(ValueError):
    """A release bundle failed structural or integrity verification."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactBundleError("bundle metadata must be strict JSON") from exc


def _strict_json(payload: bytes, label: str) -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ArtifactBundleError(f"{label} contains duplicate object keys")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactBundleError(f"{label} contains a non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactBundleError(f"{label} is invalid JSON") from exc


def _strict_canonical_json(payload: bytes, label: str) -> Any:
    value = _strict_json(payload, label)
    if payload != canonical_json_bytes(value):
        raise ArtifactBundleError(f"{label} is not canonical JSON")
    return value


def _validate_release_context(value: Any) -> list[str]:
    if type(value) is not dict or set(value) != _RELEASE_CONTEXT_FIELDS:
        raise ArtifactBundleError("release context has invalid fields")
    text_fields = _RELEASE_CONTEXT_FIELDS - {"formats", "created_ts"}
    if any(
        type(value.get(name)) is not str or not value[name]
        for name in text_fields
    ):
        raise ArtifactBundleError("release context text fields are invalid")
    created_ts = value.get("created_ts")
    if type(created_ts) not in {int, float}:
        raise ArtifactBundleError("release timestamp is invalid")
    numeric_ts = cast(float, created_ts)
    if not math.isfinite(numeric_ts):
        raise ArtifactBundleError("release timestamp is invalid")
    for name in ("document_sha256", "request_sha256"):
        digest = value[name]
        if len(digest) != 64 or any(char not in _HEX for char in digest):
            raise ArtifactBundleError("release context digest is invalid")
    formats = value.get("formats")
    if (
        type(formats) is not list
        or not formats
        or any(type(item) is not str for item in formats)
        or len(formats) != len(set(formats))
        or not set(formats) <= set(supported_formats())
    ):
        raise ArtifactBundleError("release format manifest is invalid")
    return formats


def _safe_path(path: str) -> bool:
    return (
        type(path) is str
        and bool(path)
        and not path.startswith(("/", "\\"))
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _media_type(path: str, asset_media: dict[str, str]) -> str:
    if path == "artifact/document.json" or path.endswith(".json"):
        return "application/json"
    if path.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if path.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if path.endswith(".pdf"):
        return "application/pdf"
    if path.endswith(".pptx"):
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if path.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if path.startswith("assets/"):
        return asset_media[path.removeprefix("assets/")]
    return "application/octet-stream"


def build_release_bundle(
    *,
    release_context: dict[str, Any],
    document: ArtifactDocument,
    renders: dict[str, bytes],
    assets: dict[str, bytes],
    asset_media: dict[str, str],
    governance: dict[str, Any],
    validation_report: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    validate_or_raise(document)
    formats = _validate_release_context(release_context)
    if type(renders) is not dict:
        raise ArtifactBundleError("release renders must be an exact object")
    if type(assets) is not dict or type(asset_media) is not dict:
        raise ArtifactBundleError("release assets must be exact objects")
    if type(governance) is not dict or set(governance) != _REQUIRED_GOVERNANCE:
        raise ArtifactBundleError("release governance payload is incomplete")
    if type(validation_report) is not dict or validation_report.get("valid") is not True:
        raise ArtifactBundleError("release validation report is not passing")
    if set(formats) != set(renders):
        raise ArtifactBundleError("release formats do not match rendered outputs")
    if set(assets) != set(asset_media):
        raise ArtifactBundleError("asset media manifest does not match payloads")

    files: dict[str, bytes] = {"artifact/document.json": document.canonical_bytes()}
    for format_name in formats:
        payload = renders[format_name]
        if type(payload) is not bytes:
            raise ArtifactBundleError("rendered output must be exact bytes")
        path = f"renders/document.{extension_for(format_name)}"
        if path in files:
            raise ArtifactBundleError("duplicate rendered output path")
        files[path] = payload
    for asset_id, payload in assets.items():
        if type(asset_id) is not str or type(payload) is not bytes:
            raise ArtifactBundleError("asset payload is invalid")
        path = f"assets/{asset_id}"
        if not _safe_path(path):
            raise ArtifactBundleError("asset path is unsafe")
        files[path] = payload
    for name in sorted(_REQUIRED_GOVERNANCE):
        files[f"governance/{name}.json"] = canonical_json_bytes(governance[name])
    files["validation/report.json"] = canonical_json_bytes(validation_report)
    if len(files) != len({path.casefold() for path in files}):
        raise ArtifactBundleError("release bundle paths collide case-insensitively")

    entries: list[dict[str, Any]] = []
    for path in sorted(files):
        payload = files[path]
        if len(payload) > _MAX_MEMBER_BYTES:
            raise ArtifactBundleError("release member exceeds the size limit")
        entries.append({
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "media_type": _media_type(path, asset_media),
        })
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "release": release_context,
        "document_sha256": document.content_hash(),
        "files": entries,
    }
    files["manifest.json"] = canonical_json_bytes(manifest)
    if len(files) > _MAX_MEMBERS or sum(len(value) for value in files.values()) > _MAX_BUNDLE_BYTES:
        raise ArtifactBundleError("release bundle exceeds governed limits")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[path], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    payload = output.getvalue()
    verify_release_bundle(payload)
    return payload, manifest


def verify_release_bundle(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or len(payload) > _MAX_BUNDLE_BYTES:
        raise ArtifactBundleError("release bundle payload is invalid or too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArtifactBundleError("release bundle is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if (
            not infos
            or len(infos) > _MAX_MEMBERS
            or len(names) != len(set(names))
            or len(names) != len({name.casefold() for name in names})
            or sum(info.file_size for info in infos) > _MAX_BUNDLE_BYTES
        ):
            raise ArtifactBundleError("release bundle member set is invalid")
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if (
                not _safe_path(info.filename)
                or info.is_dir()
                or mode == 0o120000
                or info.file_size > _MAX_MEMBER_BYTES
                or info.date_time != _FIXED_ZIP_TIME
            ):
                raise ArtifactBundleError("release bundle contains an unsafe member")
        if "manifest.json" not in names:
            raise ArtifactBundleError("release bundle manifest is missing")
        manifest_bytes = archive.read("manifest.json")
        manifest = _strict_canonical_json(manifest_bytes, "release manifest")
        if (
            type(manifest) is not dict
            or set(manifest) != {
                "schema_version",
                "release",
                "document_sha256",
                "files",
            }
            or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        ):
            raise ArtifactBundleError("release bundle schema is unsupported")
        entries = manifest.get("files")
        if type(entries) is not list:
            raise ArtifactBundleError("release manifest file list is invalid")
        declared: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if type(entry) is not dict or set(entry) != {"path", "sha256", "size", "media_type"}:
                raise ArtifactBundleError("release manifest entry is invalid")
            path = entry.get("path")
            if type(path) is not str or path in declared or not _safe_path(path):
                raise ArtifactBundleError("release manifest path is invalid or duplicated")
            size = entry.get("size")
            digest = entry.get("sha256")
            media_type = entry.get("media_type")
            if (
                type(size) is not int
                or size < 0
                or size > _MAX_MEMBER_BYTES
                or type(digest) is not str
                or len(digest) != 64
                or any(char not in _HEX for char in digest)
                or type(media_type) is not str
                or not media_type
            ):
                raise ArtifactBundleError("release manifest digest metadata is invalid")
            declared[path] = entry
        if set(names) != set(declared) | {"manifest.json"}:
            raise ArtifactBundleError("release payload and manifest file sets differ")
        for path, entry in declared.items():
            member = archive.read(path)
            if len(member) != entry["size"] or hashlib.sha256(member).hexdigest() != entry["sha256"]:
                raise ArtifactBundleError(f"release member integrity failed: {path}")
        required_paths = {
            "artifact/document.json",
            "validation/report.json",
            *(f"governance/{name}.json" for name in _REQUIRED_GOVERNANCE),
        }
        if not required_paths <= set(declared):
            raise ArtifactBundleError("release governance payloads are incomplete")
        try:
            document = ArtifactDocument.from_json(
                archive.read("artifact/document.json")
            )
            validate_or_raise(document)
        except (
            ArtifactModelError,
            ArtifactValidationError,
            TypeError,
            ValueError,
        ) as exc:
            raise ArtifactBundleError("released artifact document is invalid") from exc
        if manifest.get("document_sha256") != document.content_hash():
            raise ArtifactBundleError("release document hash does not match the manifest")
        release = manifest.get("release")
        formats = _validate_release_context(release)
        release_context = cast(dict[str, Any], release)
        if release_context.get("document_sha256") != document.content_hash():
            raise ArtifactBundleError("release identity is not bound to the document")
        expected_renders = {f"renders/document.{extension_for(item)}" for item in formats}
        actual_renders = {path for path in declared if path.startswith("renders/")}
        if expected_renders != actual_renders:
            raise ArtifactBundleError("release rendered outputs are incomplete or unexpected")
        if "json" in formats and archive.read("renders/document.json") != document.canonical_bytes():
            raise ArtifactBundleError("canonical JSON render differs from the released document")
        for name in _REQUIRED_GOVERNANCE:
            value = _strict_canonical_json(
                archive.read(f"governance/{name}.json"),
                f"{name} governance",
            )
            if type(value) not in {dict, list}:
                raise ArtifactBundleError(f"{name} governance payload is invalid")
        report = _strict_canonical_json(
            archive.read("validation/report.json"), "validation report"
        )
        if type(report) is not dict or report.get("valid") is not True:
            raise ArtifactBundleError("released validation report is not passing")
        return manifest
