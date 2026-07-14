"""Deterministic, self-verifying professional artifact release bundles."""
from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
import zlib
from typing import Any, cast

from hybridagent.artifacts.models import ArtifactDocument, ArtifactModelError, FigureBlock
from hybridagent.artifacts.render_common import (
    ArtifactRenderError,
    checked_assets,
    portable_member_path,
)
from hybridagent.artifacts.render_markdown import render_markdown
from hybridagent.artifacts.renderers import extension_for, supported_formats
from hybridagent.artifacts.validation import (
    ArtifactValidationError,
    validate_document,
    validate_or_raise,
)

BUNDLE_SCHEMA_VERSION = 2
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
        "review_ids",
        "signature_ids",
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
    text_fields = _RELEASE_CONTEXT_FIELDS - {
        "formats", "review_ids", "signature_ids", "created_ts"
    }
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
        or formats != sorted(set(formats))
        or not set(formats) <= set(supported_formats())
    ):
        raise ArtifactBundleError("release format manifest is invalid")
    for name in ("review_ids", "signature_ids"):
        identifiers = value.get(name)
        if (
            type(identifiers) is not list
            or not identifiers
            or any(type(item) is not str or not item for item in identifiers)
            or identifiers != sorted(set(identifiers))
        ):
            raise ArtifactBundleError(f"release {name} manifest is invalid")
    return formats


def _safe_path(path: str) -> bool:
    return portable_member_path(path)


def _figure_media(document: ArtifactDocument) -> dict[str, str]:
    result: dict[str, str] = {}
    for section in (*document.sections, *document.appendices):
        for block in section.blocks:
            if type(block) is FigureBlock:
                previous = result.get(block.asset_id)
                if previous is not None and previous != block.media_type:
                    raise ArtifactBundleError("figure asset has conflicting media types")
                result[block.asset_id] = block.media_type
    return result


def _validate_document_identity(
    release_context: dict[str, Any], document: ArtifactDocument
) -> None:
    if (
        release_context["artifact_id"] != document.artifact_id
        or release_context["organization_id"] != document.metadata.organization_id
        or release_context["workspace_id"] != document.metadata.workspace_id
        or release_context["document_sha256"] != document.content_hash()
    ):
        raise ArtifactBundleError("release identity is not bound to the artifact document")


def _exact_object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ArtifactBundleError(f"release {label} payload has invalid fields")
    return cast(dict[str, Any], value)


def _scope_matches(value: dict[str, Any], context: dict[str, Any]) -> bool:
    return (
        value.get("organization_id") == context["organization_id"]
        and value.get("workspace_id") == context["workspace_id"]
    )


def _valid_timestamp(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(cast(float, value))


def _validate_governance(
    governance: dict[str, Any],
    release_context: dict[str, Any],
    document: ArtifactDocument,
) -> None:
    if type(governance) is not dict or set(governance) != _REQUIRED_GOVERNANCE:
        raise ArtifactBundleError("release governance payload is incomplete")

    review_fields = {
        "review_id", "organization_id", "workspace_id", "run_id", "review_type",
        "required_role", "subject", "status", "decision", "decision_payload",
        "created_by", "created_ts", "reviewer_user_id", "reviewed_ts",
    }
    reviews_value = governance["reviews"]
    if type(reviews_value) is not list or not reviews_value:
        raise ArtifactBundleError("release reviews payload is invalid")
    reviews: dict[str, dict[str, Any]] = {}
    expected_subject = {
        "artifact_id": release_context["artifact_id"],
        "version_id": release_context["version_id"],
        "document_sha256": release_context["document_sha256"],
    }
    for value in reviews_value:
        review = _exact_object(value, review_fields, "reviews")
        review_id = review.get("review_id")
        if (
            type(review_id) is not str
            or not review_id
            or review_id in reviews
            or not _scope_matches(review, release_context)
            or review.get("run_id") != release_context["run_id"]
            or review.get("review_type") != "professional_release"
            or review.get("status") != "decided"
            or review.get("decision") != "approved"
            or review.get("subject") != expected_subject
            or type(review.get("required_role")) is not str
            or not review["required_role"]
            or type(review.get("reviewer_user_id")) is not str
            or not review["reviewer_user_id"]
            or not _valid_timestamp(review.get("created_ts"))
            or not _valid_timestamp(review.get("reviewed_ts"))
            or type(review.get("decision_payload")) is not dict
        ):
            raise ArtifactBundleError("release reviews are not bound to the exact release")
        reviews[review_id] = review
    if sorted(reviews) != release_context["review_ids"]:
        raise ArtifactBundleError("release review IDs do not match the release context")

    signature_fields = {
        "signature_id", "artifact_id", "version_id", "organization_id",
        "workspace_id", "document_hash", "review_id", "signer_user_id", "role",
        "meaning", "signed_ts",
    }
    signatures_value = governance["signatures"]
    if type(signatures_value) is not list or not signatures_value:
        raise ArtifactBundleError("release signatures payload is invalid")
    signature_ids: set[str] = set()
    for value in signatures_value:
        signature = _exact_object(value, signature_fields, "signatures")
        signature_id = signature.get("signature_id")
        review_id = signature.get("review_id")
        approved_review = reviews.get(review_id) if type(review_id) is str else None
        if (
            type(signature_id) is not str
            or not signature_id
            or signature_id in signature_ids
            or approved_review is None
            or not _scope_matches(signature, release_context)
            or signature.get("artifact_id") != release_context["artifact_id"]
            or signature.get("version_id") != release_context["version_id"]
            or signature.get("document_hash") != release_context["document_sha256"]
            or signature.get("signer_user_id") != approved_review["reviewer_user_id"]
            or signature.get("role") != approved_review["required_role"]
            or type(signature.get("meaning")) is not str
            or not signature["meaning"]
            or not _valid_timestamp(signature.get("signed_ts"))
        ):
            raise ArtifactBundleError("release signatures are not bound to approved reviews")
        signature_ids.add(signature_id)
    if sorted(signature_ids) != release_context["signature_ids"]:
        raise ArtifactBundleError("release signature IDs do not match the release context")

    run_value = _exact_object(governance["run"], {"run", "checkpoint"}, "run provenance")
    run = run_value.get("run")
    checkpoint = run_value.get("checkpoint")
    if (
        type(run) is not dict
        or type(checkpoint) is not dict
        or not _scope_matches(run, release_context)
        or not _scope_matches(checkpoint, release_context)
        or run.get("run_id") != release_context["run_id"]
        or run.get("status") in {"cancelled", "failed"}
        or checkpoint.get("run_id") != release_context["run_id"]
        or checkpoint.get("checkpoint_id") != release_context["checkpoint_id"]
    ):
        raise ArtifactBundleError("release run provenance is not bound to the release context")

    evidence = _exact_object(
        governance["evidence"], {"source_manifest", "versions", "spans"}, "evidence"
    )
    expected_sources = document.to_dict()["sources"]
    versions = evidence.get("versions")
    spans = evidence.get("spans")
    if evidence.get("source_manifest") != expected_sources or type(versions) is not list or type(spans) is not list:
        raise ArtifactBundleError("release evidence is not bound to the artifact document")
    version_map: dict[str, dict[str, Any]] = {}
    for value in versions:
        if type(value) is not dict or not _scope_matches(value, release_context):
            raise ArtifactBundleError("release evidence version scope is invalid")
        version_id = value.get("version_id")
        if type(version_id) is not str or not version_id or version_id in version_map:
            raise ArtifactBundleError("release evidence version identity is invalid")
        version_map[version_id] = value
    if set(version_map) != {source.source_version_id for source in document.sources}:
        raise ArtifactBundleError("release evidence versions do not match the source manifest")
    for source in document.sources:
        value = version_map[source.source_version_id]
        if value.get("source_id") != source.source_id or value.get("content_hash") != source.content_hash:
            raise ArtifactBundleError("release evidence version is not bound to its source")
    span_map: dict[str, dict[str, Any]] = {}
    for value in spans:
        if type(value) is not dict or not _scope_matches(value, release_context):
            raise ArtifactBundleError("release evidence span scope is invalid")
        span_id = value.get("span_id")
        if type(span_id) is not str or not span_id or span_id in span_map:
            raise ArtifactBundleError("release evidence span identity is invalid")
        span_map[span_id] = value
    expected_span_ids = {span_id for citation in document.citations for span_id in citation.span_ids}
    if set(span_map) != expected_span_ids:
        raise ArtifactBundleError("release evidence spans do not match artifact citations")
    source_versions = {source.source_id: source.source_version_id for source in document.sources}
    for citation in document.citations:
        if any(span_map[span_id].get("version_id") != source_versions[citation.source_id] for span_id in citation.span_ids):
            raise ArtifactBundleError("release evidence span is not bound to its cited source")

    claims = _exact_object(
        governance["claims"], {"claims", "evidence_links"}, "claims"
    )
    claim_values = claims.get("claims")
    link_values = claims.get("evidence_links")
    if type(claim_values) is not list or type(link_values) is not list:
        raise ArtifactBundleError("release claims payload is invalid")
    claim_map: dict[str, dict[str, Any]] = {}
    for value in claim_values:
        if type(value) is not dict or not _scope_matches(value, release_context):
            raise ArtifactBundleError("release claims scope is invalid")
        claim_id = value.get("claim_id")
        if type(claim_id) is not str or not claim_id or claim_id in claim_map:
            raise ArtifactBundleError("release claims identity is invalid")
        if value.get("material") == 1 and value.get("status") != "supported":
            raise ArtifactBundleError("release contains an unsupported material claim")
        claim_map[claim_id] = value
    expected_claim_ids = {claim_id for citation in document.citations for claim_id in citation.claim_ids}
    if not expected_claim_ids <= set(claim_map):
        raise ArtifactBundleError("release claims do not cover artifact citations")
    links: list[dict[str, Any]] = []
    for value in link_values:
        if type(value) is not dict or not _scope_matches(value, release_context):
            raise ArtifactBundleError("release claim evidence link scope is invalid")
        links.append(value)
    for citation in document.citations:
        for claim_id in citation.claim_ids:
            if not any(
                link.get("claim_id") == claim_id and link.get("span_id") in citation.span_ids
                for link in links
            ):
                raise ArtifactBundleError("release claims are not linked to cited evidence")


def _canonical_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(
                info,
                files[path],
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return output.getvalue()


def _read_member(archive: zipfile.ZipFile, path: str) -> bytes:
    try:
        return archive.read(path)
    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        raise ArtifactBundleError(f"release bundle member cannot be read: {path}") from exc


def _zip_flag_bits(path: str) -> int:
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        return 0x800
    return 0


def _media_type(path: str, asset_media: dict[str, str]) -> str:
    if path.startswith("assets/"):
        return asset_media[path.removeprefix("assets/")]
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
    _validate_document_identity(release_context, document)
    if type(renders) is not dict:
        raise ArtifactBundleError("release renders must be an exact object")
    if type(assets) is not dict or type(asset_media) is not dict:
        raise ArtifactBundleError("release assets must be exact objects")
    if any(type(key) is not str or type(value) is not bytes for key, value in assets.items()):
        raise ArtifactBundleError("asset payload is invalid")
    if any(
        type(key) is not str or type(value) is not str or not value
        for key, value in asset_media.items()
    ):
        raise ArtifactBundleError("asset media manifest is invalid")
    if type(governance) is not dict or set(governance) != _REQUIRED_GOVERNANCE:
        raise ArtifactBundleError("release governance payload is incomplete")
    _validate_governance(governance, release_context, document)
    expected_report = validate_document(document).to_dict()
    if validation_report != expected_report or validation_report.get("valid") is not True:
        raise ArtifactBundleError("release validation report is not passing or exact")
    if set(formats) != set(renders):
        raise ArtifactBundleError("release formats do not match rendered outputs")
    if set(assets) != set(asset_media):
        raise ArtifactBundleError("asset media manifest does not match payloads")
    for asset_id in assets:
        if not _safe_path(f"assets/{asset_id}"):
            raise ArtifactBundleError("asset ID is not a safe portable name")
    expected_asset_media = _figure_media(document)
    if set(assets) != set(expected_asset_media):
        raise ArtifactBundleError("figure assets must exactly match the artifact document")
    if asset_media != expected_asset_media:
        raise ArtifactBundleError("asset media type does not match the artifact document")
    try:
        checked_assets(document, assets)
    except ArtifactRenderError as exc:
        raise ArtifactBundleError("asset media payload does not match its declared type") from exc

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

    payload = _canonical_zip(files)
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
            or names != sorted(names)
            or archive.comment
            or len(names) != len(set(names))
            or len(names) != len({name.casefold() for name in names})
            or sum(info.file_size for info in infos) > _MAX_BUNDLE_BYTES
        ):
            raise ArtifactBundleError("release bundle member set or canonical ZIP order is invalid")
        for info in infos:
            if (
                not _safe_path(info.filename)
                or info.is_dir()
                or info.file_size > _MAX_MEMBER_BYTES
                or info.date_time != _FIXED_ZIP_TIME
                or info.compress_type != zipfile.ZIP_DEFLATED
                or info.create_system != 0
                or info.external_attr != 0o600 << 16
                or info.internal_attr != 0
                or info.extra
                or info.comment
                or info.flag_bits != _zip_flag_bits(info.filename)
                or info.create_version != 20
                or info.extract_version != 20
            ):
                raise ArtifactBundleError("release bundle contains an unsafe or noncanonical member")
        if "manifest.json" not in names:
            raise ArtifactBundleError("release bundle manifest is missing")
        manifest_bytes = _read_member(archive, "manifest.json")
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
        if list(declared) != sorted(declared):
            raise ArtifactBundleError("release manifest file order is not canonical")
        if set(names) != set(declared) | {"manifest.json"}:
            raise ArtifactBundleError("release payload and manifest file sets differ")
        for path, entry in declared.items():
            member = _read_member(archive, path)
            if len(member) != entry["size"] or hashlib.sha256(member).hexdigest() != entry["sha256"]:
                raise ArtifactBundleError(f"release member integrity failed: {path}")
        required_paths = {
            "artifact/document.json",
            "validation/report.json",
            *(f"governance/{name}.json" for name in _REQUIRED_GOVERNANCE),
        }
        if not required_paths <= set(declared):
            raise ArtifactBundleError("release governance payloads are incomplete")
        document_payload = _strict_canonical_json(
            _read_member(archive, "artifact/document.json"), "artifact document"
        )
        try:
            document = ArtifactDocument.from_dict(document_payload)
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
        _validate_document_identity(release_context, document)
        expected_renders = {f"renders/document.{extension_for(item)}" for item in formats}
        asset_media = _figure_media(document)
        expected_assets = {f"assets/{asset_id}" for asset_id in asset_media}
        if set(declared) != required_paths | expected_renders | expected_assets:
            raise ArtifactBundleError("release payload members are incomplete or unexpected")
        for path, entry in declared.items():
            if entry["media_type"] != _media_type(path, asset_media):
                raise ArtifactBundleError(f"release manifest media type is invalid: {path}")
        if (
            "json" in formats
            and _read_member(archive, "renders/document.json") != document.canonical_bytes()
        ):
            raise ArtifactBundleError("canonical JSON render differs from the released document")
        if (
            "markdown" in formats
            and _read_member(archive, "renders/document.md") != render_markdown(document)
        ):
            raise ArtifactBundleError("deterministic Markdown render differs from the released document")
        try:
            checked_assets(
                document,
                {
                    asset_id: _read_member(archive, f"assets/{asset_id}")
                    for asset_id in asset_media
                },
            )
        except ArtifactRenderError as exc:
            raise ArtifactBundleError(
                "asset media payload does not match its declared type"
            ) from exc
        governance: dict[str, Any] = {}
        for name in _REQUIRED_GOVERNANCE:
            value = _strict_canonical_json(
                _read_member(archive, f"governance/{name}.json"),
                f"{name} governance",
            )
            if type(value) not in {dict, list}:
                raise ArtifactBundleError(f"{name} governance payload is invalid")
            governance[name] = value
        _validate_governance(governance, release_context, document)
        report = _strict_canonical_json(
            _read_member(archive, "validation/report.json"), "validation report"
        )
        if report != validate_document(document).to_dict():
            raise ArtifactBundleError("released validation report is not exact")
        return manifest
