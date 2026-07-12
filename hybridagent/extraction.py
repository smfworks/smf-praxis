"""Exact evidence locators and append-only derived extraction lineage."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from .evidence import EvidenceRegistry
from .persistence import Store

_LOCATOR_TYPES = frozenset({"document", "table", "image", "media", "repository"})
_DERIVED_KINDS = frozenset({"ocr", "caption", "transcript", "summary", "extraction"})


class ExtractionError(ValueError):
    """An extraction locator or lineage invariant was violated."""


@dataclass(frozen=True)
class EvidenceSpan:
    span_id: str
    organization_id: str
    workspace_id: str
    version_id: str
    locator_type: str
    locator: dict[str, Any]
    extracted_text: str
    created_by: str
    created_ts: float


@dataclass(frozen=True)
class DerivedArtifact:
    artifact_id: str
    organization_id: str
    workspace_id: str
    parent_span_id: str
    kind: str
    content: str
    extractor: str
    extractor_version: str
    configuration: dict[str, Any]
    created_by: str
    created_ts: float


class ExtractionRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.evidence = EvidenceRegistry(store)

    def add_span(self, organization_id: str, workspace_id: str, version_id: str, *,
                 locator_type: str, locator: dict[str, Any], extracted_text: str,
                 created_by: str) -> EvidenceSpan:
        self.evidence._validate_scope_and_actor(organization_id, workspace_id, created_by)
        if self.evidence.get_version(organization_id, workspace_id, version_id) is None:
            raise ExtractionError("evidence version does not exist in workspace")
        clean_locator = self._validate_locator(locator_type, locator)
        span_id = f"span-{uuid.uuid4().hex}"
        now = time.time()
        self.store._directory_execute(
            "INSERT INTO evidence_spans(span_id,organization_id,workspace_id,version_id,"
            "locator_type,locator_json,extracted_text,created_by,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (span_id, organization_id, workspace_id, version_id, locator_type,
             json.dumps(clean_locator, sort_keys=True), extracted_text, created_by, now))
        result = self.get_span(organization_id, workspace_id, span_id)
        assert result is not None
        return result

    def get_span(self, organization_id: str, workspace_id: str,
                 span_id: str) -> EvidenceSpan | None:
        row = self.store._directory_one(
            "SELECT * FROM evidence_spans WHERE organization_id=? AND workspace_id=? "
            "AND span_id=?", (organization_id, workspace_id, span_id))
        return self._span(row) if row else None

    def add_derived_artifact(
        self, organization_id: str, workspace_id: str, parent_span_id: str, *,
        kind: str, content: str, extractor: str, extractor_version: str,
        configuration: dict[str, Any], created_by: str,
    ) -> DerivedArtifact:
        self.evidence._validate_scope_and_actor(organization_id, workspace_id, created_by)
        if self.get_span(organization_id, workspace_id, parent_span_id) is None:
            raise ExtractionError("parent span does not exist in workspace")
        if kind not in _DERIVED_KINDS:
            raise ExtractionError(f"unknown derived artifact kind: {kind}")
        if not extractor.strip() or not extractor_version.strip():
            raise ExtractionError("extractor and version are required")
        try:
            config = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ExtractionError("extractor configuration must be JSON serializable") from exc
        artifact_id = f"derived-{uuid.uuid4().hex}"
        now = time.time()
        self.store._directory_execute(
            "INSERT INTO evidence_derived_artifacts(artifact_id,organization_id,"
            "workspace_id,parent_span_id,kind,content,extractor,extractor_version,"
            "configuration_json,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, organization_id, workspace_id, parent_span_id, kind, content,
             extractor.strip(), extractor_version.strip(), config, created_by, now))
        result = self.get_derived(organization_id, workspace_id, artifact_id)
        assert result is not None
        return result

    def get_derived(self, organization_id: str, workspace_id: str,
                    artifact_id: str) -> DerivedArtifact | None:
        row = self.store._directory_one(
            "SELECT * FROM evidence_derived_artifacts WHERE organization_id=? "
            "AND workspace_id=? AND artifact_id=?",
            (organization_id, workspace_id, artifact_id))
        return self._derived(row) if row else None

    @staticmethod
    def _validate_locator(locator_type: str,
                          locator: dict[str, Any]) -> dict[str, Any]:
        if locator_type not in _LOCATOR_TYPES:
            raise ExtractionError(f"unknown locator type: {locator_type}")
        if not isinstance(locator, dict):
            raise ExtractionError("locator must be an object")
        result = dict(locator)

        def number(value: Any) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool)

        def integer(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        if locator_type == "document":
            if not any(key in result for key in ("page", "section", "paragraph",
                                                  "char_start", "char_end")):
                raise ExtractionError("document locator requires an exact location")
            if "char_start" in result or "char_end" in result:
                start, end = result.get("char_start"), result.get("char_end")
                if not integer(start) or not integer(end):
                    raise ExtractionError("character range is invalid")
                start_int, end_int = cast(int, start), cast(int, end)
                if start_int < 0 or end_int <= start_int:
                    raise ExtractionError("character range is invalid")
            if "page" in result and (
                    not integer(result["page"]) or result["page"] < 1):
                raise ExtractionError("page must be a positive integer")
            if "paragraph" in result and (
                    not integer(result["paragraph"])
                    or result["paragraph"] < 1):
                raise ExtractionError("paragraph must be a positive integer")
            if "section" in result and (
                    not isinstance(result["section"], str)
                    or not result["section"].strip()):
                raise ExtractionError("section must be non-empty text")
        elif locator_type == "table":
            if not result.get("table") or not result.get("cell"):
                raise ExtractionError("table locator requires table and cell")
        elif locator_type == "image":
            box = result.get("bbox")
            if (not isinstance(box, list) or len(box) != 4
                    or not all(number(value) for value in box)
                    or box[2] <= box[0] or box[3] <= box[1]):
                raise ExtractionError("image locator requires a four-value bounding box")
        elif locator_type == "media":
            start, end = result.get("start_seconds"), result.get("end_seconds")
            if not number(start) or not number(end):
                raise ExtractionError("media locator requires a valid time range")
            start_num = cast(int | float, start)
            end_num = cast(int | float, end)
            if start_num < 0 or end_num <= start_num:
                raise ExtractionError("media locator requires a valid time range")
        elif locator_type == "repository":
            if (not result.get("commit") or not result.get("path")
                    or not integer(result.get("line_start"))
                    or not integer(result.get("line_end"))
                    or result["line_start"] < 1
                    or result["line_end"] < result["line_start"]):
                raise ExtractionError(
                    "repository locator requires commit, path, and line range")
        try:
            json.dumps(result, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ExtractionError("locator must be JSON serializable") from exc
        return result

    @staticmethod
    def _span(row: dict[str, Any]) -> EvidenceSpan:
        return EvidenceSpan(
            span_id=row["span_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], version_id=row["version_id"],
            locator_type=row["locator_type"], locator=json.loads(row["locator_json"]),
            extracted_text=row["extracted_text"], created_by=row["created_by"],
            created_ts=float(row["created_ts"]))

    @staticmethod
    def _derived(row: dict[str, Any]) -> DerivedArtifact:
        return DerivedArtifact(
            artifact_id=row["artifact_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], parent_span_id=row["parent_span_id"],
            kind=row["kind"], content=row["content"], extractor=row["extractor"],
            extractor_version=row["extractor_version"],
            configuration=json.loads(row["configuration_json"]),
            created_by=row["created_by"], created_ts=float(row["created_ts"]))
