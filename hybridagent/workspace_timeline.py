"""Append-only professional workspace timeline, parties, and deadlines."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .organizations import OrganizationDirectory
from .persistence import Store
from .workspaces import WorkspaceDirectory

_LINK_TYPES = frozenset({"evidence", "task", "artifact", "external_record"})


class TimelineError(ValueError):
    """A workspace timeline invariant was violated."""


@dataclass(frozen=True)
class Party:
    party_id: str
    organization_id: str
    workspace_id: str
    kind: str
    name: str
    role: str
    contacts: tuple[dict[str, str], ...]
    created_ts: float


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    organization_id: str
    workspace_id: str
    sequence: int
    event_type: str
    summary: str
    actor_user_id: str
    links: tuple[dict[str, str], ...]
    occurred_ts: float
    created_ts: float


@dataclass(frozen=True)
class Deadline:
    deadline_id: str
    organization_id: str
    workspace_id: str
    title: str
    due_date: str
    actor_user_id: str
    consequential: bool
    calculation_source: str
    calculation_rule: str
    links: tuple[dict[str, str], ...]
    review_status: str
    reviewer_user_id: str
    reviewed_ts: float | None
    created_ts: float


class WorkspaceTimeline:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.workspaces = WorkspaceDirectory(store)
        self.organizations = OrganizationDirectory(store)

    def add_party(self, organization_id: str, workspace_id: str, kind: str,
                  name: str, *, role: str = "",
                  contacts: tuple[dict[str, str], ...] = ()) -> Party:
        self._workspace(organization_id, workspace_id)
        clean_name = name.strip()
        if not clean_name or not kind.strip():
            raise TimelineError("party kind and name are required")
        normalized = []
        for contact in contacts:
            contact_kind = str(contact.get("kind") or "").strip()
            value = str(contact.get("value") or "").strip()
            if not contact_kind or not value:
                raise TimelineError("contact kind and value are required")
            normalized.append({"kind": contact_kind, "value": value})
        party_id = f"party-{uuid.uuid4().hex}"
        now = time.time()
        self.store._directory_execute(
            "INSERT INTO workspace_parties(party_id,organization_id,workspace_id,"
            "kind,name,role,contacts_json,created_ts) VALUES (?,?,?,?,?,?,?,?)",
            (party_id, organization_id, workspace_id, kind.strip(), clean_name,
             role.strip(), json.dumps(normalized), now))
        return Party(party_id, organization_id, workspace_id, kind.strip(),
                     clean_name, role.strip(), tuple(normalized), now)

    def parties(self, organization_id: str, workspace_id: str) -> list[Party]:
        rows = self.store._directory_all(
            "SELECT * FROM workspace_parties WHERE organization_id=? AND workspace_id=? "
            "ORDER BY created_ts,party_id", (organization_id, workspace_id))
        return [Party(
            row["party_id"], row["organization_id"], row["workspace_id"],
            row["kind"], row["name"], row["role"],
            tuple(json.loads(row["contacts_json"] or "[]")), row["created_ts"])
            for row in rows]

    def append_event(
        self, organization_id: str, workspace_id: str, event_type: str, summary: str,
        *, actor_user_id: str, links: tuple[dict[str, str], ...] = (),
        occurred_ts: float | None = None,
    ) -> TimelineEvent:
        self._workspace(organization_id, workspace_id)
        self._actor(organization_id, actor_user_id)
        clean_type, clean_summary = event_type.strip(), summary.strip()
        if not clean_type or not clean_summary:
            raise TimelineError("event type and summary are required")
        normalized_links = self._links(links)
        with self.store._lock:
            self.store._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.store._conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence "
                    "FROM workspace_timeline_events WHERE workspace_id=?",
                    (workspace_id,)).fetchone()
                sequence = int(row["next_sequence"])
                event_id = f"evt-{uuid.uuid4().hex}"
                now = time.time()
                occurred = now if occurred_ts is None else occurred_ts
                self.store._conn.execute(
                    "INSERT INTO workspace_timeline_events(event_id,organization_id,"
                    "workspace_id,sequence,event_type,summary,actor_user_id,links_json,"
                    "occurred_ts,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (event_id, organization_id, workspace_id, sequence, clean_type,
                     clean_summary, actor_user_id, json.dumps(normalized_links),
                     occurred, now))
                self.store._conn.commit()
            except Exception:
                self.store._conn.rollback()
                raise
        return TimelineEvent(event_id, organization_id, workspace_id, sequence,
                             clean_type, clean_summary, actor_user_id,
                             tuple(normalized_links), occurred, now)

    def events(self, organization_id: str, workspace_id: str) -> list[TimelineEvent]:
        rows = self.store._directory_all(
            "SELECT * FROM workspace_timeline_events WHERE organization_id=? "
            "AND workspace_id=? ORDER BY sequence", (organization_id, workspace_id))
        return [self._event(row) for row in rows]

    def update_event(self, organization_id: str, workspace_id: str,
                     event_id: str, **changes: Any) -> None:
        del organization_id, workspace_id, event_id, changes
        raise TimelineError("timeline events are append-only")

    def add_deadline(
        self, organization_id: str, workspace_id: str, title: str, due_date: str,
        *, actor_user_id: str, consequential: bool = False,
        calculation_source: str = "", calculation_rule: str = "",
        links: tuple[dict[str, str], ...] = (),
    ) -> Deadline:
        self._workspace(organization_id, workspace_id)
        self._actor(organization_id, actor_user_id)
        clean_title, clean_date = title.strip(), due_date.strip()
        if not clean_title or not clean_date:
            raise TimelineError("deadline title and due date are required")
        source, rule = calculation_source.strip(), calculation_rule.strip()
        if consequential and (not source or not rule):
            raise TimelineError("consequential deadline requires source and rule")
        deadline_id = f"deadline-{uuid.uuid4().hex}"
        now = time.time()
        status = "required" if consequential else "not_required"
        normalized_links = self._links(links)
        self.store._directory_execute(
            "INSERT INTO workspace_deadlines(deadline_id,organization_id,workspace_id,"
            "title,due_date,actor_user_id,consequential,calculation_source,"
            "calculation_rule,links_json,review_status,reviewer_user_id,reviewed_ts,"
            "created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (deadline_id, organization_id, workspace_id, clean_title, clean_date,
             actor_user_id, int(consequential), source, rule,
             json.dumps(normalized_links), status, "", None, now))
        result = self._deadline(self.store._directory_one(
            "SELECT * FROM workspace_deadlines WHERE deadline_id=?", (deadline_id,)))
        return result

    def deadlines(self, organization_id: str, workspace_id: str) -> list[Deadline]:
        rows = self.store._directory_all(
            "SELECT * FROM workspace_deadlines WHERE organization_id=? "
            "AND workspace_id=? ORDER BY due_date,created_ts",
            (organization_id, workspace_id))
        return [self._deadline(row) for row in rows]

    def review_deadline(self, organization_id: str, workspace_id: str,
                        deadline_id: str, *, reviewer_user_id: str,
                        decision: str) -> Deadline:
        if decision not in {"approved", "rejected"}:
            raise TimelineError("deadline review decision must be approved or rejected")
        self._actor(organization_id, reviewer_user_id)
        existing = self.store._directory_one(
            "SELECT * FROM workspace_deadlines WHERE organization_id=? "
            "AND workspace_id=? AND deadline_id=?",
            (organization_id, workspace_id, deadline_id))
        if existing is None:
            raise TimelineError("deadline does not exist in workspace")
        now = time.time()
        self.store._directory_execute(
            "UPDATE workspace_deadlines SET review_status=?,reviewer_user_id=?,"
            "reviewed_ts=? WHERE organization_id=? AND workspace_id=? AND deadline_id=?",
            (decision, reviewer_user_id, now, organization_id, workspace_id, deadline_id))
        return self._deadline(self.store._directory_one(
            "SELECT * FROM workspace_deadlines WHERE deadline_id=?", (deadline_id,)))

    def _workspace(self, organization_id: str, workspace_id: str) -> None:
        if self.workspaces.get(organization_id, workspace_id) is None:
            raise TimelineError("workspace does not exist in organization")

    def _actor(self, organization_id: str, user_id: str) -> None:
        membership = self.organizations.membership(organization_id, user_id)
        user = self.organizations.user(user_id)
        if (membership is None or membership.status != "active" or user is None
                or user.status != "active"):
            raise TimelineError("actor must be an active organization member")

    @staticmethod
    def _links(links: tuple[dict[str, str], ...]) -> list[dict[str, str]]:
        normalized = []
        for link in links:
            link_type = str(link.get("type") or "").strip()
            link_id = str(link.get("id") or "").strip()
            if link_type not in _LINK_TYPES or not link_id:
                raise TimelineError("link requires a supported type and id")
            normalized.append({"type": link_type, "id": link_id})
        return normalized

    @staticmethod
    def _event(row: dict[str, Any]) -> TimelineEvent:
        return TimelineEvent(
            row["event_id"], row["organization_id"], row["workspace_id"],
            int(row["sequence"]), row["event_type"], row["summary"],
            row["actor_user_id"], tuple(json.loads(row["links_json"] or "[]")),
            float(row["occurred_ts"]), float(row["created_ts"]))

    @staticmethod
    def _deadline(row: dict[str, Any] | None) -> Deadline:
        if row is None:
            raise TimelineError("deadline does not exist")
        return Deadline(
            row["deadline_id"], row["organization_id"], row["workspace_id"],
            row["title"], row["due_date"], row["actor_user_id"],
            bool(row["consequential"]), row["calculation_source"],
            row["calculation_rule"], tuple(json.loads(row["links_json"] or "[]")),
            row["review_status"], row["reviewer_user_id"], row["reviewed_ts"],
            float(row["created_ts"]))
