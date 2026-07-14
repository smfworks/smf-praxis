"""Typed append-only Artifact Studio version, signature, release, and diff records."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from hybridagent.artifacts.models import ArtifactDocument


@dataclass(frozen=True)
class ArtifactAsset:
    asset_id: str
    media_type: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True)
class ArtifactVersion:
    version_id: str
    artifact_id: str
    organization_id: str
    workspace_id: str
    sequence: int
    parent_version_id: str
    document_hash: str
    document: ArtifactDocument
    assets: tuple[ArtifactAsset, ...]
    created_by: str
    created_ts: float


@dataclass(frozen=True)
class ArtifactSignature:
    signature_id: str
    artifact_id: str
    version_id: str
    organization_id: str
    workspace_id: str
    document_hash: str
    review_id: str
    signer_user_id: str
    role: str
    meaning: str
    signed_ts: float


@dataclass(frozen=True)
class ArtifactRelease:
    release_id: str
    artifact_id: str
    version_id: str
    organization_id: str
    workspace_id: str
    document_hash: str
    formats: tuple[str, ...]
    manifest: dict[str, Any]
    validation_report: dict[str, Any]
    review_ids: tuple[str, ...]
    signature_ids: tuple[str, ...]
    idempotency_key: str
    request_hash: str
    run_id: str
    checkpoint_id: str
    bundle_hash: str
    bundle: bytes
    created_by: str
    created_ts: float


@dataclass(frozen=True)
class ArtifactDiff:
    from_version_id: str
    to_version_id: str
    document_fields: tuple[str, ...]
    metadata_fields: tuple[str, ...]
    added_sections: tuple[str, ...]
    removed_sections: tuple[str, ...]
    changed_sections: tuple[str, ...]
    added_blocks: tuple[str, ...]
    removed_blocks: tuple[str, ...]
    changed_blocks: tuple[str, ...]
    added_citations: tuple[str, ...]
    removed_citations: tuple[str, ...]
    changed_citations: tuple[str, ...]
    added_sources: tuple[str, ...]
    removed_sources: tuple[str, ...]
    changed_sources: tuple[str, ...]
    governance_fields: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return any(
            getattr(self, field.name)
            for field in fields(self)
            if field.name not in {"from_version_id", "to_version_id"}
        )


def _section_map(document: ArtifactDocument) -> dict[str, tuple[str, int, dict[str, Any]]]:
    result: dict[str, tuple[str, int, dict[str, Any]]] = {}
    raw = document.to_dict()
    for group_name in ("sections", "appendices"):
        for position, section in enumerate(raw[group_name]):
            result[section["section_id"]] = (group_name, position, section)
    return result


def _block_map(document: ArtifactDocument) -> dict[str, tuple[str, int, dict[str, Any]]]:
    result: dict[str, tuple[str, int, dict[str, Any]]] = {}
    raw = document.to_dict()
    for group in (raw["sections"], raw["appendices"]):
        for section in group:
            for position, block in enumerate(section["blocks"]):
                result[block["block_id"]] = (
                    section["section_id"],
                    position,
                    block,
                )
    return result


def _record_map(
    values: list[dict[str, Any]], key: str
) -> dict[str, tuple[int, dict[str, Any]]]:
    return {value[key]: (position, value) for position, value in enumerate(values)}


def _map_delta(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    added = tuple(sorted(after.keys() - before.keys()))
    removed = tuple(sorted(before.keys() - after.keys()))
    changed = tuple(sorted(key for key in before.keys() & after.keys() if before[key] != after[key]))
    return added, removed, changed


def compare_documents(
    from_version_id: str,
    before: ArtifactDocument,
    to_version_id: str,
    after: ArtifactDocument,
) -> ArtifactDiff:
    before_raw = before.to_dict()
    after_raw = after.to_dict()
    document_fields = tuple(
        key
        for key in ("schema_version", "artifact_id")
        if before_raw[key] != after_raw[key]
    )
    metadata_fields = tuple(sorted(
        key for key in before_raw["metadata"]
        if before_raw["metadata"][key] != after_raw["metadata"][key]
    ))
    section_delta = _map_delta(_section_map(before), _section_map(after))
    block_delta = _map_delta(_block_map(before), _block_map(after))
    citation_delta = _map_delta(
        _record_map(before_raw["citations"], "citation_id"),
        _record_map(after_raw["citations"], "citation_id"),
    )
    source_delta = _map_delta(
        _record_map(before_raw["sources"], "source_id"),
        _record_map(after_raw["sources"], "source_id"),
    )
    governance_fields = tuple(
        name for name in ("revisions", "reviews", "signatures")
        if before_raw[name] != after_raw[name]
    )
    return ArtifactDiff(
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        document_fields=document_fields,
        metadata_fields=metadata_fields,
        added_sections=section_delta[0],
        removed_sections=section_delta[1],
        changed_sections=section_delta[2],
        added_blocks=block_delta[0],
        removed_blocks=block_delta[1],
        changed_blocks=block_delta[2],
        added_citations=citation_delta[0],
        removed_citations=citation_delta[1],
        changed_citations=citation_delta[2],
        added_sources=source_delta[0],
        removed_sources=source_delta[1],
        changed_sources=source_delta[2],
        governance_fields=governance_fields,
    )
