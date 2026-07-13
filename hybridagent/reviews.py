"""Typed professional reviews for durable workspace-scoped workflows."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .checkpoints import CheckpointError, CheckpointRegistry
from .organizations import ROLES
from .persistence import Store

REVIEW_TYPES = frozenset({"quality", "professional_release", "research_findings"})
REVIEW_DECISIONS = frozenset({"approved", "revise", "rejected"})
RESEARCH_SCHEMA_MANIFEST = {"name": "research-supervisor", "version": 1}
REVIEW_ROLES = {
    "quality": frozenset(
        {
            "organization_admin",
            "workspace_admin",
            "professional",
            "reviewer",
            "auditor",
        }
    ),
    "professional_release": frozenset(
        {
            "organization_admin",
            "professional",
            "reviewer",
        }
    ),
    "research_findings": frozenset(
        {
            "organization_admin",
            "professional",
            "reviewer",
            "auditor",
        }
    ),
}


class ReviewError(ValueError):
    """A typed professional review invariant was violated."""


@dataclass(frozen=True)
class ProfessionalReview:
    review_id: str
    organization_id: str
    workspace_id: str
    run_id: str
    review_type: str
    required_role: str
    subject: dict[str, Any]
    status: str
    decision: str
    decision_payload: dict[str, Any]
    created_by: str
    reviewer_user_id: str
    created_ts: float
    reviewed_ts: float | None


class ReviewRegistry:
    def __init__(self, store: Store, checkpoints: CheckpointRegistry | None = None) -> None:
        self.store = store
        self.checkpoints = checkpoints

    @staticmethod
    def new_review_id() -> str:
        return f"review-{uuid.uuid4().hex}"

    def request_review(
        self,
        organization_id: str,
        workspace_id: str,
        *,
        created_by: str,
        review_type: str,
        required_role: str,
        subject: dict[str, Any],
        run_id: str = "",
        interrupt_run: bool = True,
        review_id: str = "",
        checkpoint_state: dict[str, Any] | None = None,
        expected_head_checkpoint_id: str | None = None,
    ) -> ProfessionalReview:
        self._validate_scope(organization_id, workspace_id, created_by)
        rtype = review_type.strip()
        if rtype not in REVIEW_TYPES:
            raise ReviewError(f"unknown review type: {review_type}")
        role = required_role.strip()
        if role not in ROLES:
            raise ReviewError(f"unknown review role: {required_role}")
        if role not in REVIEW_ROLES[rtype]:
            raise ReviewError(f"role {role} cannot authorize {rtype} review")
        if run_id and self.checkpoints is None:
            raise ReviewError("run-backed reviews require checkpoints")
        if checkpoint_state is not None and (not run_id or not interrupt_run):
            raise ReviewError("review checkpoint state requires an interrupting run")
        if rtype == "research_findings" and (
            not run_id
            or not interrupt_run
            or checkpoint_state is None
            or expected_head_checkpoint_id is None
        ):
            raise ReviewError(
                "research findings review requires a compatible research run and bound checkpoint"
            )
        review_id = review_id or self.new_review_id()
        if (
            type(review_id) is not str
            or not review_id.startswith("review-")
            or len(review_id) != 39
            or any(char not in "0123456789abcdef" for char in review_id[7:])
        ):
            raise ReviewError("invalid review id")
        base_subject_json = self._json_object(subject, "review subject")
        checkpoint_id = f"checkpoint-{uuid.uuid4().hex}" if checkpoint_state is not None else ""
        subject_payload = json.loads(base_subject_json)
        if rtype == "research_findings":
            if (
                type(checkpoint_state) is not dict
                or not CheckpointRegistry._is_exact_json(checkpoint_state)
            ):
                raise ReviewError("research review checkpoint state is invalid")
            assert checkpoint_state is not None
            if subject_payload.get("run_id") != run_id:
                raise ReviewError("research review subject must match its run")
            if (
                type(checkpoint_state.get("query")) is not str
                or not checkpoint_state["query"].strip()
                or type(checkpoint_state.get("hypotheses")) is not list
                or not all(type(item) is str for item in checkpoint_state["hypotheses"])
                or type(checkpoint_state.get("findings")) is not list
                or not all(type(item) is dict for item in checkpoint_state["findings"])
                or checkpoint_state.get("status") != "pending_review"
                or checkpoint_state.get("review") != {"review_id": review_id}
            ):
                raise ReviewError("research review checkpoint state is invalid")
        if checkpoint_id:
            subject_payload["checkpoint_id"] = checkpoint_id
        subject_json = self._json_object(subject_payload, "review subject")
        checkpoint_state_json = (
            self._json_object(checkpoint_state, "review checkpoint state")
            if checkpoint_state is not None
            else ""
        )
        interrupt_json = self._json_object(
            {"review_id": review_id, "review_type": rtype}, "review interrupt payload"
        )
        now = time.time()
        try:
            with self.store._lock:
                self.store._conn.execute("BEGIN IMMEDIATE")
                active = self.store._conn.execute(
                    "SELECT 1 FROM professional_workspaces w JOIN organizations o ON "
                    "o.organization_id=w.organization_id JOIN organization_memberships m ON "
                    "m.organization_id=w.organization_id JOIN organization_users u ON "
                    "u.user_id=m.user_id WHERE w.organization_id=? AND w.workspace_id=? "
                    "AND w.status='active' AND o.status='active' AND m.user_id=? "
                    "AND m.status='active' AND u.status='active'",
                    (organization_id, workspace_id, created_by),
                ).fetchone()
                if active is None:
                    raise ReviewError("active workspace membership is required")
                run = None
                if run_id:
                    run = self.store._conn.execute(
                        "SELECT kind,status,schema_manifest_json,head_checkpoint_id FROM "
                        "professional_runs WHERE organization_id=? AND workspace_id=? "
                        "AND run_id=?",
                        (organization_id, workspace_id, run_id),
                    ).fetchone()
                    if run is None:
                        raise ReviewError("run does not exist in workspace")
                    if rtype == "research_findings" and (
                        run["kind"] != "research"
                        or json.loads(run["schema_manifest_json"])
                        != RESEARCH_SCHEMA_MANIFEST
                    ):
                        raise ReviewError("run is not a compatible research run")
                    if (
                        expected_head_checkpoint_id is not None
                        and run["head_checkpoint_id"] != expected_head_checkpoint_id
                    ):
                        raise ReviewError("stale checkpoint head")
                    if interrupt_run and run["status"] != "running":
                        raise ReviewError("run is not available for review interruption")
                self.store._conn.execute(
                    "INSERT INTO professional_reviews("
                    "review_id,organization_id,workspace_id,run_id,review_type,required_role,"
                    "subject_json,status,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        review_id,
                        organization_id,
                        workspace_id,
                        run_id,
                        rtype,
                        role,
                        subject_json,
                        "pending",
                        created_by,
                        now,
                    ),
                )
                if checkpoint_state is not None:
                    sequence_row = self.store._conn.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence "
                        "FROM run_checkpoints WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    assert run is not None and sequence_row is not None
                    self.store._conn.execute(
                        "INSERT INTO run_checkpoints("
                        "checkpoint_id,run_id,organization_id,workspace_id,"
                        "parent_checkpoint_id,sequence,state_json,schema_manifest_json,"
                        "created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            checkpoint_id,
                            run_id,
                            organization_id,
                            workspace_id,
                            run["head_checkpoint_id"] or None,
                            int(sequence_row["next_sequence"]),
                            checkpoint_state_json,
                            run["schema_manifest_json"],
                            created_by,
                            now,
                        ),
                    )
                if run_id and interrupt_run:
                    head_sql = ",head_checkpoint_id=?" if checkpoint_id else ""
                    head_params: tuple[Any, ...] = (checkpoint_id,) if checkpoint_id else ()
                    cur = self.store._conn.execute(
                        "UPDATE professional_runs SET status='interrupted',"
                        "interrupt_type='professional_review',interrupt_payload_json=?,"
                        "cancel_reason='',updated_ts=?" + head_sql + " WHERE organization_id=? AND "
                        "workspace_id=? AND run_id=? AND status='running'",
                        (interrupt_json, now, *head_params, organization_id, workspace_id, run_id),
                    )
                    if cur.rowcount != 1:
                        raise ReviewError("invalid or concurrent review interruption")
                self.store._conn.commit()
        except BaseException:
            with self.store._lock:
                if self.store._conn.in_transaction:
                    self.store._conn.rollback()
            raise
        review = self.get_review(organization_id, workspace_id, review_id)
        assert review is not None
        return review

    def get_review(
        self, organization_id: str, workspace_id: str, review_id: str
    ) -> ProfessionalReview | None:
        row = self.store._directory_one(
            "SELECT * FROM professional_reviews WHERE organization_id=? AND workspace_id=? "
            "AND review_id=?",
            (organization_id, workspace_id, review_id),
        )
        return self._review(row) if row else None

    def submit_decision(
        self,
        organization_id: str,
        workspace_id: str,
        review_id: str,
        *,
        reviewer_user_id: str,
        decision: str,
        payload: dict[str, Any],
    ) -> ProfessionalReview:
        self._validate_scope(organization_id, workspace_id, reviewer_user_id)
        clean_decision = decision.strip()
        if clean_decision not in REVIEW_DECISIONS:
            raise ReviewError(f"unknown review decision: {decision}")
        payload_json = self._json_object(payload, "review decision payload")
        now = time.time()
        try:
            with self.store._lock:
                self.store._conn.execute("BEGIN IMMEDIATE")
                row = self.store._conn.execute(
                    "SELECT r.status,r.required_role,r.created_by,m.roles_json "
                    "FROM professional_reviews r JOIN organization_memberships m ON "
                    "m.organization_id=r.organization_id AND m.user_id=? AND m.status='active' "
                    "JOIN organization_users u ON u.user_id=m.user_id AND u.status='active' "
                    "JOIN organizations o ON o.organization_id=r.organization_id "
                    "JOIN professional_workspaces w ON w.organization_id=r.organization_id "
                    "AND w.workspace_id=r.workspace_id WHERE r.organization_id=? AND "
                    "r.workspace_id=? AND r.review_id=? AND o.status='active' "
                    "AND w.status='active'",
                    (reviewer_user_id, organization_id, workspace_id, review_id),
                ).fetchone()
                if row is None:
                    raise ReviewError("review does not exist in workspace")
                if row["status"] != "pending":
                    raise ReviewError("review is already decided")
                if row["created_by"] == reviewer_user_id:
                    raise ReviewError("reviewer must be distinct from the review creator")
                roles = set(json.loads(row["roles_json"]))
                if row["required_role"] not in roles:
                    raise ReviewError("reviewer does not hold the required role")
                cur = self.store._conn.execute(
                    "UPDATE professional_reviews SET status=?,decision=?,"
                    "decision_payload_json=?,reviewer_user_id=?,reviewed_ts=? "
                    "WHERE organization_id=? AND workspace_id=? AND review_id=? "
                    "AND status='pending'",
                    (
                        "decided",
                        clean_decision,
                        payload_json,
                        reviewer_user_id,
                        now,
                        organization_id,
                        workspace_id,
                        review_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise ReviewError("review is already decided")
                self.store._conn.commit()
        except BaseException:
            with self.store._lock:
                if self.store._conn.in_transaction:
                    self.store._conn.rollback()
            raise
        decided = self.get_review(organization_id, workspace_id, review_id)
        assert decided is not None
        return decided

    def _validate_scope(self, organization_id: str, workspace_id: str, user_id: str) -> None:
        row = self.store._directory_one(
            "SELECT 1 FROM professional_workspaces w JOIN organizations o ON "
            "o.organization_id=w.organization_id JOIN organization_memberships m ON "
            "m.organization_id=w.organization_id JOIN organization_users u ON "
            "u.user_id=m.user_id WHERE w.organization_id=? AND w.workspace_id=? "
            "AND w.status='active' AND o.status='active' AND m.user_id=? "
            "AND m.status='active' AND u.status='active'",
            (organization_id, workspace_id, user_id),
        )
        if row is None:
            raise ReviewError("active workspace membership is required")

    @staticmethod
    def _json_object(value: Any, label: str) -> str:
        try:
            return CheckpointRegistry._json_object(value, label)
        except CheckpointError as exc:
            raise ReviewError(str(exc)) from exc

    @staticmethod
    def _review(row: dict[str, Any]) -> ProfessionalReview:
        try:
            subject = json.loads(row["subject_json"])
            decision_payload = json.loads(row["decision_payload_json"])
        except (TypeError, ValueError) as exc:
            raise ReviewError("persisted review payload is invalid") from exc
        if (
            type(subject) is not dict
            or type(decision_payload) is not dict
            or not CheckpointRegistry._is_exact_json(subject)
            or not CheckpointRegistry._is_exact_json(decision_payload)
        ):
            raise ReviewError("persisted review payload is invalid")
        return ProfessionalReview(
            review_id=row["review_id"],
            organization_id=row["organization_id"],
            workspace_id=row["workspace_id"],
            run_id=row["run_id"],
            review_type=row["review_type"],
            required_role=row["required_role"],
            subject=subject,
            status=row["status"],
            decision=row["decision"],
            decision_payload=decision_payload,
            created_by=row["created_by"],
            reviewer_user_id=row["reviewer_user_id"],
            created_ts=float(row["created_ts"]),
            reviewed_ts=(float(row["reviewed_ts"]) if row["reviewed_ts"] is not None else None),
        )
