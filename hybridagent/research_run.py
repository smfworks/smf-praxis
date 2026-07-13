"""Structured research supervision on durable workspace-scoped runs."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .checkpoints import (
    Checkpoint,
    CheckpointError,
    CheckpointRegistry,
    WorkflowRun,
)
from .reviews import ProfessionalReview, ReviewRegistry

RESEARCH_SCHEMA_MANIFEST = {"name": "research-supervisor", "version": 1}
_RESEARCH_STATUSES = frozenset({"collecting", "pending_review", "reviewed", "rejected"})


class ResearchRunError(ValueError):
    """A structured research workflow invariant was violated."""


class ResearchSupervisor:
    def __init__(
        self,
        checkpoints: CheckpointRegistry,
        reviews: ReviewRegistry,
        *,
        organization_id: str,
        workspace_id: str,
        actor_id: str,
    ) -> None:
        if checkpoints.store is not reviews.store:
            raise ResearchRunError("research checkpoints and reviews must share one store")
        self.checkpoints = checkpoints
        self.reviews = reviews
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.actor_id = actor_id

    def start(self, query: str, *, hypotheses: list[str] | None = None) -> WorkflowRun:
        clean = query.strip()
        if not clean:
            raise ResearchRunError("research query is required")
        return self.checkpoints.create_run(
            self.organization_id,
            self.workspace_id,
            kind="research",
            created_by=self.actor_id,
            state={
                "query": clean,
                "hypotheses": list(hypotheses or []),
                "findings": [],
                "status": "collecting",
                "review": {},
            },
            schema_manifest=RESEARCH_SCHEMA_MANIFEST,
        )

    def load(self, run_id: str) -> dict[str, Any]:
        _, _, state = self._snapshot(run_id)
        return self._public_state(state)

    def record_finding(self, run_id: str, finding: dict[str, Any]) -> dict[str, Any]:
        run, latest, state = self._snapshot(run_id)
        if run.status != "running" or state["status"] != "collecting":
            raise ResearchRunError("findings cannot change while pending review")
        finding_json = self._json_object(finding, "research finding")
        findings = list(state["findings"])
        findings.append(finding_json)
        updated = dict(state)
        updated["findings"] = findings
        self._checkpoint(run_id, updated, expected_head_checkpoint_id=latest.checkpoint_id)
        return self.load(run_id)

    def request_review(self, run_id: str, *, required_role: str) -> ProfessionalReview:
        run, latest, state = self._snapshot(run_id)
        if run.status != "running" or state["status"] != "collecting":
            raise ResearchRunError("research run is not collecting")
        review_id = self.reviews.new_review_id()
        updated = dict(state)
        updated["status"] = "pending_review"
        updated["review"] = {"review_id": review_id}
        return self.reviews.request_review(
            self.organization_id,
            self.workspace_id,
            created_by=self.actor_id,
            review_type="research_findings",
            required_role=required_role,
            subject={
                "run_id": run_id,
                "finding_count": len(state["findings"]),
                "source_checkpoint_id": latest.checkpoint_id,
            },
            run_id=run_id,
            review_id=review_id,
            checkpoint_state=updated,
            expected_head_checkpoint_id=latest.checkpoint_id,
        )

    def apply_review(self, review_id: str) -> dict[str, Any]:
        store = self.checkpoints.store
        now = time.time()
        checkpoint_id = f"checkpoint-{uuid.uuid4().hex}"
        try:
            with store._lock:
                conn = store._conn
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    "SELECT 1 FROM professional_workspaces w JOIN organizations o ON "
                    "o.organization_id=w.organization_id JOIN organization_memberships m ON "
                    "m.organization_id=w.organization_id JOIN organization_users u ON "
                    "u.user_id=m.user_id WHERE w.organization_id=? AND w.workspace_id=? "
                    "AND w.status='active' AND o.status='active' AND m.user_id=? "
                    "AND m.status='active' AND u.status='active'",
                    (self.organization_id, self.workspace_id, self.actor_id),
                ).fetchone()
                if active is None:
                    raise ResearchRunError("active workspace membership is required")
                review_row = conn.execute(
                    "SELECT * FROM professional_reviews WHERE organization_id=? "
                    "AND workspace_id=? AND review_id=?",
                    (self.organization_id, self.workspace_id, review_id),
                ).fetchone()
                if review_row is None:
                    raise ResearchRunError("review does not exist in workspace")
                if review_row["status"] != "decided":
                    raise ResearchRunError("review is not decided")
                if review_row["review_type"] != "research_findings":
                    raise ResearchRunError("review is not a research findings review")
                run_id = review_row["run_id"]
                if not run_id:
                    raise ResearchRunError("review is not attached to a run")
                run = conn.execute(
                    "SELECT * FROM professional_runs WHERE organization_id=? "
                    "AND workspace_id=? AND run_id=?",
                    (self.organization_id, self.workspace_id, run_id),
                ).fetchone()
                if run is None:
                    raise ResearchRunError("research run does not exist in workspace")
                self._validate_run_row(run)
                interrupt_payload = json.loads(run["interrupt_payload_json"])
                if (
                    run["status"] != "interrupted"
                    or run["interrupt_type"] != "professional_review"
                    or interrupt_payload.get("review_id") != review_id
                    or interrupt_payload.get("review_type") != "research_findings"
                ):
                    raise ResearchRunError("review is not the active review interrupt")
                subject = json.loads(review_row["subject_json"])
                if (
                    subject.get("checkpoint_id") != run["head_checkpoint_id"]
                    or subject.get("run_id") != run_id
                ):
                    raise ResearchRunError("review snapshot no longer matches run head")
                head = conn.execute(
                    "SELECT * FROM run_checkpoints WHERE organization_id=? AND workspace_id=? "
                    "AND run_id=? AND checkpoint_id=?",
                    (self.organization_id, self.workspace_id, run_id, run["head_checkpoint_id"]),
                ).fetchone()
                if head is None:
                    raise ResearchRunError("review checkpoint does not exist")
                state = json.loads(head["state_json"])
                self._validate_state(state)
                review_state = state["review"]
                if (
                    state["status"] != "pending_review"
                    or review_state.get("review_id") != review_id
                ):
                    raise ResearchRunError("review snapshot is not pending this review")
                decision = review_row["decision"]
                updated = dict(state)
                updated["status"] = {
                    "approved": "reviewed",
                    "revise": "collecting",
                    "rejected": "rejected",
                }[decision]
                updated["review"] = {
                    "review_id": review_id,
                    "decision": decision,
                    "payload": json.loads(review_row["decision_payload_json"]),
                }
                state_json = CheckpointRegistry._json_object(updated, "research review state")
                sequence_row = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM run_checkpoints WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                assert sequence_row is not None
                conn.execute(
                    "INSERT INTO run_checkpoints(checkpoint_id,run_id,organization_id,"
                    "workspace_id,parent_checkpoint_id,sequence,state_json,"
                    "schema_manifest_json,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        checkpoint_id,
                        run_id,
                        self.organization_id,
                        self.workspace_id,
                        run["head_checkpoint_id"],
                        int(sequence_row[0]),
                        state_json,
                        run["schema_manifest_json"],
                        self.actor_id,
                        now,
                    ),
                )
                target_status = "failed" if decision == "rejected" else "running"
                reason = "professional review rejected" if decision == "rejected" else ""
                cur = conn.execute(
                    "UPDATE professional_runs SET status=?,head_checkpoint_id=?,"
                    "interrupt_type='',interrupt_payload_json='{}',cancel_reason=?,updated_ts=? "
                    "WHERE organization_id=? AND workspace_id=? AND run_id=? "
                    "AND status='interrupted' AND head_checkpoint_id=? "
                    "AND interrupt_type='professional_review'",
                    (
                        target_status,
                        checkpoint_id,
                        reason,
                        now,
                        self.organization_id,
                        self.workspace_id,
                        run_id,
                        run["head_checkpoint_id"],
                    ),
                )
                if cur.rowcount != 1:
                    raise ResearchRunError("invalid or concurrent review application")
                conn.commit()
        except BaseException as exc:
            with store._lock:
                if store._conn.in_transaction:
                    store._conn.rollback()
            if isinstance(exc, ResearchRunError):
                raise
            raise ResearchRunError(str(exc)) from exc
        return self.load(run_id)

    def _snapshot(self, run_id: str) -> tuple[WorkflowRun, Checkpoint, dict[str, Any]]:
        run = self.checkpoints.get_run(self.organization_id, self.workspace_id, run_id)
        if run is None:
            raise ResearchRunError("research run does not exist in workspace")
        if run.kind != "research" or run.schema_manifest != RESEARCH_SCHEMA_MANIFEST:
            raise ResearchRunError("run is not a compatible research run")
        latest = self.checkpoints.latest(self.organization_id, self.workspace_id, run_id)
        if latest is None:
            raise ResearchRunError("research run has no checkpoint state")
        state = dict(latest.state)
        self._validate_state(state)
        return run, latest, state

    @staticmethod
    def _validate_run_row(run: Any) -> None:
        if (
            run["kind"] != "research"
            or json.loads(run["schema_manifest_json"]) != RESEARCH_SCHEMA_MANIFEST
        ):
            raise ResearchRunError("run is not a compatible research run")

    @staticmethod
    def _validate_state(state: Any) -> None:
        if not CheckpointRegistry._is_exact_json(state) or type(state) is not dict:
            raise ResearchRunError("research state must be strict JSON")
        if (
            type(state.get("query")) is not str
            or not state["query"].strip()
            or type(state.get("hypotheses")) is not list
            or not all(type(item) is str for item in state["hypotheses"])
            or type(state.get("findings")) is not list
            or not all(type(item) is dict for item in state["findings"])
            or state.get("status") not in _RESEARCH_STATUSES
            or type(state.get("review")) is not dict
        ):
            raise ResearchRunError("research state shape is invalid")

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "query": state["query"],
            "hypotheses": list(state["hypotheses"]),
            "findings": list(state["findings"]),
            "status": state["status"],
            "review": dict(state["review"]),
        }

    def _checkpoint(
        self, run_id: str, state: dict[str, Any], *, expected_head_checkpoint_id: str | None = None
    ) -> None:
        try:
            self.checkpoints.checkpoint(
                self.organization_id,
                self.workspace_id,
                run_id,
                actor_id=self.actor_id,
                state=state,
                expected_head_checkpoint_id=expected_head_checkpoint_id,
            )
        except CheckpointError as exc:
            raise ResearchRunError(str(exc)) from exc

    @staticmethod
    def _json_object(value: dict[str, Any], label: str) -> dict[str, Any]:
        try:
            CheckpointRegistry._json_object(value, label)
        except CheckpointError as exc:
            raise ResearchRunError(str(exc)) from exc
        return dict(value)
