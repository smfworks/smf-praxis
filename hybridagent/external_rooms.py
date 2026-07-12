"""Controlled, item-allowlisted external collaboration rooms."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .organizations import OrganizationDirectory
from .persistence import Store
from .workspaces import WorkspaceDirectory

ROOM_PERMISSIONS = frozenset({"read_shared", "comment", "upload"})
SHAREABLE_ITEM_TYPES = frozenset({"artifact", "evidence", "task", "message"})
ROOM_ADMIN_ROLES = frozenset({"organization_admin", "workspace_admin", "professional"})
ROOM_SHARE_ROLES = ROOM_ADMIN_ROLES | {"member"}


class ExternalRoomError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalRoom:
    room_id: str
    organization_id: str
    workspace_id: str
    name: str
    permissions: tuple[str, ...]
    created_by: str
    status: str


@dataclass(frozen=True)
class RoomInvitation:
    room_id: str
    user_id: str
    status: str
    expires_ts: float | None


@dataclass(frozen=True)
class RoomDecision:
    allowed: bool
    reason: str


class ExternalRoomDirectory:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.organizations = OrganizationDirectory(store)
        self.workspaces = WorkspaceDirectory(store)

    def create(self, organization_id: str, workspace_id: str, name: str, *,
               created_by: str,
               permissions: tuple[str, ...] = ("read_shared", "comment"),
               ) -> ExternalRoom:
        self._workspace(organization_id, workspace_id)
        self._actor(organization_id, created_by, ROOM_ADMIN_ROLES)
        normalized = tuple(sorted(set(permissions)))
        unknown = set(normalized) - ROOM_PERMISSIONS
        if unknown:
            raise ExternalRoomError(f"unknown room permission: {sorted(unknown)[0]}")
        clean = name.strip()
        if not clean:
            raise ExternalRoomError("room name is required")
        room = ExternalRoom(f"room-{uuid.uuid4().hex}", organization_id,
                            workspace_id, clean, normalized, created_by, "active")
        self.store._directory_execute(
            "INSERT INTO external_rooms(room_id,organization_id,workspace_id,name,"
            "permissions_json,created_by,status,created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (room.room_id, organization_id, workspace_id, clean,
             json.dumps(normalized), created_by, room.status, time.time()))
        return room

    def invite(self, organization_id: str, workspace_id: str, room_id: str,
               user_id: str, *, invited_by: str, expires_ts: float | None = None,
               replace: bool = False) -> RoomInvitation:
        self._room(organization_id, workspace_id, room_id)
        self._actor(organization_id, invited_by, ROOM_ADMIN_ROLES)
        membership = self.organizations.membership(organization_id, user_id)
        user = self.organizations.user(user_id)
        organization = self.organizations.organization(organization_id)
        if (membership is None or membership.status != "active"
                or user is None or user.status != "active"
                or organization is None or organization.status != "active"
                or "external_collaborator" not in membership.roles):
            raise ExternalRoomError("invitee must be an active external collaborator")
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        try:
            self.store._directory_execute(
                f"{verb} INTO external_room_members(room_id,user_id,invited_by,status,"
                "expires_ts,revoked_by,revoked_ts,created_ts) VALUES (?,?,?,?,?,?,?,?)",
                (room_id, user_id, invited_by, "active", expires_ts, "", None,
                 time.time()))
        except sqlite3.IntegrityError as exc:
            raise ExternalRoomError("room invitation already exists") from exc
        return RoomInvitation(room_id, user_id, "active", expires_ts)

    def revoke(self, organization_id: str, workspace_id: str, room_id: str,
               user_id: str, *, revoked_by: str) -> None:
        self._room(organization_id, workspace_id, room_id)
        self._actor(organization_id, revoked_by, ROOM_ADMIN_ROLES)
        self.store._directory_execute(
            "UPDATE external_room_members SET status='revoked',revoked_by=?,"
            "revoked_ts=? WHERE room_id=? AND user_id=?",
            (revoked_by, time.time(), room_id, user_id))

    def authorize(self, organization_id: str, workspace_id: str, room_id: str,
                  user_id: str, permission: str) -> RoomDecision:
        room = self._room_row(organization_id, workspace_id, room_id)
        if room is None or room["status"] != "active":
            return RoomDecision(False, "room_scope_denied")
        if permission not in ROOM_PERMISSIONS:
            return RoomDecision(False, "permission_denied")
        permissions = set(json.loads(room["permissions_json"] or "[]"))
        if permission not in permissions:
            return RoomDecision(False, "permission_denied")
        member = self.store._directory_one(
            "SELECT status,expires_ts FROM external_room_members "
            "WHERE room_id=? AND user_id=?", (room_id, user_id))
        if member is None or member["status"] != "active":
            return RoomDecision(False, "invitation_required")
        user = self.organizations.user(user_id)
        organization = self.organizations.organization(organization_id)
        membership = self.organizations.membership(organization_id, user_id)
        if (user is None or user.status != "active"
                or organization is None or organization.status != "active"
                or membership is None or membership.status != "active"):
            return RoomDecision(False, "principal_inactive")
        if member["expires_ts"] is not None and member["expires_ts"] <= time.time():
            return RoomDecision(False, "invitation_expired")
        return RoomDecision(True, "allowed")

    def share_item(self, organization_id: str, workspace_id: str, room_id: str,
                   item_type: str, item_id: str, *, shared_by: str) -> None:
        self._room(organization_id, workspace_id, room_id)
        self._actor(organization_id, shared_by, ROOM_SHARE_ROLES)
        if item_type not in SHAREABLE_ITEM_TYPES or not item_id.strip():
            raise ExternalRoomError("unsupported shared item")
        resource = self.store._directory_one(
            "SELECT 1 AS found FROM workspace_resources WHERE organization_id=? "
            "AND workspace_id=? AND item_type=? AND item_id=?",
            (organization_id, workspace_id, item_type, item_id.strip()))
        if resource is None:
            raise ExternalRoomError("shared item does not exist in workspace")
        self.store._directory_execute(
            "INSERT INTO external_room_items(room_id,item_type,item_id,shared_by,"
            "created_ts) VALUES (?,?,?,?,?)",
            (room_id, item_type, item_id.strip(), shared_by, time.time()))

    def shared_items(self, organization_id: str, workspace_id: str,
                     room_id: str) -> list[dict[str, str]]:
        self._room(organization_id, workspace_id, room_id)
        rows = self.store._directory_all(
            "SELECT item_type,item_id FROM external_room_items WHERE room_id=? "
            "ORDER BY created_ts,item_type,item_id", (room_id,))
        return [{"item_type": row["item_type"], "item_id": row["item_id"]}
                for row in rows]

    def is_item_shared(self, organization_id: str, workspace_id: str, room_id: str,
                       item_type: str, item_id: str) -> bool:
        if self._room_row(organization_id, workspace_id, room_id) is None:
            return False
        return self.store._directory_one(
            "SELECT 1 AS found FROM external_room_items WHERE room_id=? "
            "AND item_type=? AND item_id=?", (room_id, item_type, item_id)) is not None

    def _workspace(self, organization_id: str, workspace_id: str) -> None:
        if self.workspaces.get(organization_id, workspace_id) is None:
            raise ExternalRoomError("workspace does not exist in organization")

    def register_resource(self, organization_id: str, workspace_id: str,
                          item_type: str, item_id: str) -> None:
        """Register a workspace-owned resource before external sharing."""
        self._workspace(organization_id, workspace_id)
        if item_type not in SHAREABLE_ITEM_TYPES or not item_id.strip():
            raise ExternalRoomError("unsupported workspace resource")
        self.store._directory_execute(
            "INSERT INTO workspace_resources(organization_id,workspace_id,item_type,"
            "item_id,created_ts) VALUES (?,?,?,?,?)",
            (organization_id, workspace_id, item_type, item_id.strip(), time.time()))

    def _actor(self, organization_id: str, user_id: str,
               allowed_roles: frozenset[str]) -> None:
        membership = self.organizations.membership(organization_id, user_id)
        user = self.organizations.user(user_id)
        organization = self.organizations.organization(organization_id)
        if (membership is None or membership.status != "active"
                or user is None or user.status != "active"
                or organization is None or organization.status != "active"):
            raise ExternalRoomError("actor must be an active organization member")
        if not set(membership.roles).intersection(allowed_roles):
            raise ExternalRoomError("actor role cannot manage external room")

    def _room_row(self, organization_id: str, workspace_id: str,
                  room_id: str) -> dict | None:
        return self.store._directory_one(
            "SELECT * FROM external_rooms WHERE organization_id=? AND workspace_id=? "
            "AND room_id=?", (organization_id, workspace_id, room_id))

    def _room(self, organization_id: str, workspace_id: str, room_id: str) -> dict:
        row = self._room_row(organization_id, workspace_id, room_id)
        if row is None:
            raise ExternalRoomError("room does not exist in workspace")
        return row
