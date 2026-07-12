"""Tenant-owned professional workspace aggregate."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .organizations import OrganizationDirectory
from .persistence import Store

WORKSPACE_KINDS = frozenset({
    "matter", "patient_case", "dental_case", "forensic_case",
    "building_project", "course", "learner_portfolio",
    "consulting_engagement", "technology_engagement",
})
CONFIDENTIALITY_LEVELS = frozenset({
    "public", "internal", "confidential", "privileged", "phi",
    "education_record", "evidence",
})
_FIELD_TYPES = frozenset({"string", "number", "integer", "boolean"})


class WorkspaceError(ValueError):
    """A professional-workspace invariant was violated."""


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    organization_id: str
    human_identifier: str
    kind: str
    title: str
    client_or_subject: str
    owner_user_id: str
    team_id: str
    status: str
    confidentiality: str
    jurisdiction: str
    location: str
    opened_date: str
    target_date: str
    field_schema: dict[str, dict[str, Any]]
    custom_fields: dict[str, Any]
    external_links: tuple[dict[str, str], ...]
    legal_hold: bool
    hold_reason: str
    created_ts: float
    updated_ts: float


class WorkspaceDirectory:
    """Persistence and invariant boundary for professional workspaces."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.organizations = OrganizationDirectory(store)

    def create(
        self, organization_id: str, human_identifier: str, kind: str, title: str,
        *, owner_user_id: str, team_id: str = "", client_or_subject: str = "",
        confidentiality: str = "internal", jurisdiction: str = "",
        location: str = "", opened_date: str = "", target_date: str = "",
        field_schema: dict[str, dict[str, Any]] | None = None,
        custom_fields: dict[str, Any] | None = None,
        external_links: tuple[dict[str, str], ...] = (),
    ) -> Workspace:
        identifier = human_identifier.strip()
        clean_title = title.strip()
        if not identifier:
            raise WorkspaceError("human-readable identifier is required")
        if not clean_title:
            raise WorkspaceError("workspace title is required")
        if kind not in WORKSPACE_KINDS:
            raise WorkspaceError(f"unknown workspace kind: {kind}")
        if confidentiality not in CONFIDENTIALITY_LEVELS:
            raise WorkspaceError(f"unknown confidentiality: {confidentiality}")
        self._validate_owner(organization_id, owner_user_id)
        self._validate_team(organization_id, team_id)
        schema = field_schema or {}
        fields = custom_fields or {}
        self._validate_fields(schema, fields)
        links = self._validate_links(external_links)
        now = time.time()
        workspace_id = f"ws-{uuid.uuid4().hex}"
        try:
            self.store._directory_execute(
                "INSERT INTO professional_workspaces("
                "workspace_id,organization_id,human_identifier,kind,title,"
                "client_or_subject,owner_user_id,team_id,status,confidentiality,"
                "jurisdiction,location,opened_date,target_date,field_schema_json,"
                "custom_fields_json,external_links_json,legal_hold,hold_reason,"
                "created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workspace_id, organization_id, identifier, kind, clean_title,
                 client_or_subject.strip(), owner_user_id, team_id, "active",
                 confidentiality, jurisdiction.strip(), location.strip(),
                 opened_date.strip(), target_date.strip(), json.dumps(schema),
                 json.dumps(fields), json.dumps(links), 0, "", now, now),
            )
        except sqlite3.IntegrityError as exc:
            if "human_identifier" in str(exc) or "UNIQUE" in str(exc):
                raise WorkspaceError("workspace identifier already exists") from exc
            raise WorkspaceError("workspace could not be created") from exc
        result = self.get(organization_id, workspace_id)
        assert result is not None
        return result

    def get(self, organization_id: str, workspace_id: str) -> Workspace | None:
        row = self.store._directory_one(
            "SELECT * FROM professional_workspaces "
            "WHERE organization_id=? AND workspace_id=?",
            (organization_id, workspace_id))
        return self._workspace(row) if row else None

    def list_for(self, organization_id: str) -> list[Workspace]:
        rows = self.store._directory_all(
            "SELECT * FROM professional_workspaces WHERE organization_id=? "
            "ORDER BY created_ts,rowid", (organization_id,))
        return [self._workspace(row) for row in rows]

    def set_archived(self, organization_id: str, workspace_id: str, *,
                     archived: bool) -> Workspace:
        return self._update_state(
            organization_id, workspace_id, "status",
            "archived" if archived else "active")

    def set_hold(self, organization_id: str, workspace_id: str, *, held: bool,
                 reason: str = "") -> Workspace:
        clean_reason = reason.strip()
        if held and not clean_reason:
            raise WorkspaceError("legal hold reason is required")
        existing = self.get(organization_id, workspace_id)
        if existing is None:
            raise WorkspaceError("workspace does not exist")
        self.store._directory_execute(
            "UPDATE professional_workspaces SET legal_hold=?,hold_reason=?,updated_ts=? "
            "WHERE organization_id=? AND workspace_id=?",
            (int(held), clean_reason if held else "", time.time(),
             organization_id, workspace_id))
        result = self.get(organization_id, workspace_id)
        assert result is not None
        return result

    def _update_state(self, organization_id: str, workspace_id: str,
                      field: str, value: str) -> Workspace:
        if field != "status":
            raise WorkspaceError("unsupported state field")
        existing = self.get(organization_id, workspace_id)
        if existing is None:
            raise WorkspaceError("workspace does not exist")
        self.store._directory_execute(
            "UPDATE professional_workspaces SET status=?,updated_ts=? "
            "WHERE organization_id=? AND workspace_id=?",
            (value, time.time(), organization_id, workspace_id))
        result = self.get(organization_id, workspace_id)
        assert result is not None
        return result

    def _validate_owner(self, organization_id: str, user_id: str) -> None:
        membership = self.organizations.membership(organization_id, user_id)
        user = self.organizations.user(user_id)
        if (membership is None or membership.status != "active" or user is None
                or user.status != "active"):
            raise WorkspaceError("owner must be an active organization member")

    def _validate_team(self, organization_id: str, team_id: str) -> None:
        if not team_id:
            return
        row = self.store._directory_one(
            "SELECT organization_id FROM organization_teams WHERE team_id=?",
            (team_id,))
        if row is None or row["organization_id"] != organization_id:
            raise WorkspaceError("team must belong to the workspace organization")

    @staticmethod
    def _validate_fields(schema: dict[str, dict[str, Any]],
                         fields: dict[str, Any]) -> None:
        unknown = set(fields) - set(schema)
        if unknown:
            raise WorkspaceError(f"unknown field: {sorted(unknown)[0]}")
        for name, spec in schema.items():
            field_type = spec.get("type")
            if field_type not in _FIELD_TYPES:
                raise WorkspaceError(f"unknown field type for {name}")
            if spec.get("required") and name not in fields:
                raise WorkspaceError(f"required field missing: {name}")
            if name not in fields:
                continue
            value = fields[name]
            valid = {
                "string": isinstance(value, str),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
            }[field_type]
            if not valid:
                raise WorkspaceError(f"invalid type for field: {name}")

    @staticmethod
    def _validate_links(links: tuple[dict[str, str], ...]) -> list[dict[str, str]]:
        result = []
        for link in links:
            system = str(link.get("system") or "").strip()
            external_id = str(link.get("external_id") or "").strip()
            if not system or not external_id:
                raise WorkspaceError("external link requires system and external_id")
            result.append({"system": system, "external_id": external_id})
        return result

    @staticmethod
    def _workspace(row: dict[str, Any]) -> Workspace:
        return Workspace(
            workspace_id=row["workspace_id"], organization_id=row["organization_id"],
            human_identifier=row["human_identifier"], kind=row["kind"],
            title=row["title"], client_or_subject=row["client_or_subject"],
            owner_user_id=row["owner_user_id"], team_id=row["team_id"],
            status=row["status"], confidentiality=row["confidentiality"],
            jurisdiction=row["jurisdiction"], location=row["location"],
            opened_date=row["opened_date"], target_date=row["target_date"],
            field_schema=json.loads(row["field_schema_json"] or "{}"),
            custom_fields=json.loads(row["custom_fields_json"] or "{}"),
            external_links=tuple(json.loads(row["external_links_json"] or "[]")),
            legal_hold=bool(row["legal_hold"]), hold_reason=row["hold_reason"],
            created_ts=float(row["created_ts"]), updated_ts=float(row["updated_ts"]))
