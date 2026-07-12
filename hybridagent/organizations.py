"""Tenant-safe organization, user, membership, and team directory."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .persistence import Store

ROLES = frozenset({
    "organization_admin", "workspace_admin", "professional", "reviewer",
    "member", "external_collaborator", "auditor",
})


class OrganizationError(ValueError):
    """A directory invariant was violated."""


@dataclass(frozen=True)
class Organization:
    organization_id: str
    name: str
    status: str


@dataclass(frozen=True)
class User:
    user_id: str
    email: str
    display_name: str
    status: str


@dataclass(frozen=True)
class Membership:
    organization_id: str
    user_id: str
    roles: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class Team:
    team_id: str
    organization_id: str
    name: str


class OrganizationDirectory:
    """Domain boundary for the professional organization directory."""

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def create_organization(self, name: str) -> Organization:
        clean = name.strip()
        if not clean:
            raise OrganizationError("organization name is required")
        result = Organization(self._id("org"), clean, "active")
        self.store._directory_execute(
            "INSERT INTO organizations(organization_id,name,status,created_ts) "
            "VALUES (?,?,?,?)",
            (result.organization_id, result.name, result.status, time.time()),
        )
        return result

    def organization(self, organization_id: str) -> Organization | None:
        row = self.store._directory_one(
            "SELECT organization_id,name,status FROM organizations "
            "WHERE organization_id=?", (organization_id,))
        return Organization(**row) if row else None

    def create_user(self, email: str, *, display_name: str = "") -> User:
        normalized = email.strip().lower()
        if not normalized or "@" not in normalized:
            raise OrganizationError("valid email is required")
        result = User(self._id("usr"), normalized, display_name.strip(), "active")
        try:
            self.store._directory_execute(
                "INSERT INTO organization_users"
                "(user_id,email,display_name,status,created_ts) VALUES (?,?,?,?,?)",
                (result.user_id, result.email, result.display_name,
                 result.status, time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise OrganizationError("user email already exists") from exc
        return result

    def user(self, user_id: str) -> User | None:
        row = self.store._directory_one(
            "SELECT user_id,email,display_name,status FROM organization_users "
            "WHERE user_id=?", (user_id,))
        return User(**row) if row else None

    def add_membership(self, organization_id: str, user_id: str, *,
                       roles: tuple[str, ...] = ("member",)) -> Membership:
        normalized = tuple(sorted(set(roles)))
        unknown = set(normalized) - ROLES
        if unknown:
            raise OrganizationError(f"unknown role: {sorted(unknown)[0]}")
        if not normalized:
            raise OrganizationError("at least one role is required")
        if self.organization(organization_id) is None:
            raise OrganizationError("organization does not exist")
        if self.user(user_id) is None:
            raise OrganizationError("user does not exist")
        result = Membership(organization_id, user_id, normalized, "active")
        try:
            self.store._directory_execute(
                "INSERT INTO organization_memberships"
                "(organization_id,user_id,roles_json,status,created_ts) "
                "VALUES (?,?,?,?,?)",
                (organization_id, user_id, json.dumps(normalized),
                 result.status, time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise OrganizationError("membership already exists") from exc
        return result

    def membership(self, organization_id: str, user_id: str) -> Membership | None:
        row = self.store._directory_one(
            "SELECT organization_id,user_id,roles_json,status "
            "FROM organization_memberships WHERE organization_id=? AND user_id=?",
            (organization_id, user_id),
        )
        return self._membership(row) if row else None

    def members_for(self, organization_id: str) -> list[Membership]:
        rows = self.store._directory_all(
            "SELECT organization_id,user_id,roles_json,status "
            "FROM organization_memberships WHERE organization_id=? "
            "ORDER BY created_ts,user_id", (organization_id,))
        return [self._membership(row) for row in rows]

    @staticmethod
    def _membership(row: dict) -> Membership:
        return Membership(row["organization_id"], row["user_id"],
                          tuple(json.loads(row["roles_json"])), row["status"])

    def bootstrap(self, name: str, admin_email: str) -> tuple[Organization, User]:
        organization = self.create_organization(name)
        admin = self.create_user(admin_email)
        self.add_membership(organization.organization_id, admin.user_id,
                            roles=("organization_admin",))
        return organization, admin

    def create_team(self, organization_id: str, name: str) -> Team:
        if self.organization(organization_id) is None:
            raise OrganizationError("organization does not exist")
        clean = name.strip()
        if not clean:
            raise OrganizationError("team name is required")
        team = Team(self._id("team"), organization_id, clean)
        try:
            self.store._directory_execute(
                "INSERT INTO organization_teams"
                "(team_id,organization_id,name,created_ts) VALUES (?,?,?,?)",
                (team.team_id, organization_id, clean, time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise OrganizationError("team already exists") from exc
        return team

    def add_team_member(self, team_id: str, user_id: str) -> None:
        team = self.store._directory_one(
            "SELECT team_id,organization_id,name FROM organization_teams "
            "WHERE team_id=?", (team_id,))
        if team is None:
            raise OrganizationError("team does not exist")
        membership = self.membership(team["organization_id"], user_id)
        user = self.user(user_id)
        if (membership is None or membership.status != "active"
                or user is None or user.status != "active"):
            raise OrganizationError("user is not a member or is inactive")
        try:
            self.store._directory_execute(
                "INSERT INTO organization_team_members(team_id,user_id,created_ts) "
                "VALUES (?,?,?)", (team_id, user_id, time.time()))
        except sqlite3.IntegrityError as exc:
            raise OrganizationError("team membership already exists") from exc

    def team_members(self, team_id: str) -> list[User]:
        rows = self.store._directory_all(
            "SELECT u.user_id,u.email,u.display_name,u.status "
            "FROM organization_team_members tm "
            "JOIN organization_users u ON u.user_id=tm.user_id "
            "WHERE tm.team_id=? ORDER BY tm.created_ts,u.user_id", (team_id,))
        return [User(**row) for row in rows]
