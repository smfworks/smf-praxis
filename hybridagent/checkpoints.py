"""Tenant-scoped durable workflow runs and immutable JSON checkpoints."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .persistence import Store
from .run_state import INTERRUPT_TYPES, RunState


class CheckpointError(ValueError):
    """A durable run or checkpoint invariant was violated."""


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    organization_id: str
    workspace_id: str
    kind: str
    status: str
    schema_manifest: dict[str, Any]
    head_checkpoint_id: str
    parent_run_id: str
    forked_from_checkpoint_id: str
    interrupt_type: str
    interrupt_payload: dict[str, Any]
    cancel_reason: str
    created_by: str
    created_ts: float
    updated_ts: float


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    run_id: str
    organization_id: str
    workspace_id: str
    parent_checkpoint_id: str | None
    sequence: int
    state: dict[str, Any]
    schema_manifest: dict[str, Any]
    created_by: str
    created_ts: float


@dataclass(frozen=True)
class EffectReceipt:
    receipt_id: str
    run_id: str
    organization_id: str
    workspace_id: str
    idempotency_key: str
    fingerprint: str
    effect_type: str
    result: dict[str, Any]
    created_by: str
    created_ts: float


class CheckpointRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store

    def create_run(self, organization_id: str, workspace_id: str, *, kind: str,
                   created_by: str, state: dict[str, Any],
                   schema_manifest: dict[str, Any]) -> WorkflowRun:
        self._validate_scope(organization_id, workspace_id, created_by)
        if not kind.strip():
            raise CheckpointError("run kind is required")
        state_json = self._json_object(state, "checkpoint state")
        manifest_json = self._json_object(schema_manifest, "schema manifest")
        run_id = f"run-{uuid.uuid4().hex}"
        checkpoint_id = f"checkpoint-{uuid.uuid4().hex}"
        now = time.time()
        with self.store._lock:
            conn = self.store._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO professional_runs(run_id,organization_id,workspace_id,"
                    "kind,status,schema_manifest_json,head_checkpoint_id,created_by,"
                    "created_ts,updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, organization_id, workspace_id, kind.strip(), "running",
                     manifest_json, checkpoint_id, created_by, now, now))
                conn.execute(
                    "INSERT INTO run_checkpoints(checkpoint_id,run_id,organization_id,"
                    "workspace_id,parent_checkpoint_id,sequence,state_json,"
                    "schema_manifest_json,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (checkpoint_id, run_id, organization_id, workspace_id, None, 1,
                     state_json, manifest_json, created_by, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = self.get_run(organization_id, workspace_id, run_id)
        assert result is not None
        return result

    def get_run(self, organization_id: str, workspace_id: str,
                run_id: str) -> WorkflowRun | None:
        row = self.store._directory_one(
            "SELECT * FROM professional_runs WHERE organization_id=? AND workspace_id=? "
            "AND run_id=?", (organization_id, workspace_id, run_id))
        return self._run(row) if row else None

    def latest(self, organization_id: str, workspace_id: str,
               run_id: str) -> Checkpoint | None:
        row = self.store._directory_one(
            "SELECT * FROM run_checkpoints WHERE organization_id=? AND workspace_id=? "
            "AND run_id=? ORDER BY sequence DESC LIMIT 1",
            (organization_id, workspace_id, run_id))
        return self._checkpoint(row) if row else None

    def get_checkpoint(self, organization_id: str, workspace_id: str,
                       checkpoint_id: str) -> Checkpoint | None:
        row = self.store._directory_one(
            "SELECT * FROM run_checkpoints WHERE organization_id=? AND workspace_id=? "
            "AND checkpoint_id=?", (organization_id, workspace_id, checkpoint_id))
        return self._checkpoint(row) if row else None

    def fork(self, organization_id: str, workspace_id: str, source_run_id: str, *,
             checkpoint_id: str, actor_id: str) -> WorkflowRun:
        """Fork a new lineage from one immutable scoped checkpoint."""
        self._validate_scope(organization_id, workspace_id, actor_id)
        source = self.get_run(organization_id, workspace_id, source_run_id)
        checkpoint = self.get_checkpoint(organization_id, workspace_id, checkpoint_id)
        if source is None or checkpoint is None or checkpoint.run_id != source_run_id:
            raise CheckpointError("source checkpoint does not exist in run")
        run_id = f"run-{uuid.uuid4().hex}"
        initial_id = f"checkpoint-{uuid.uuid4().hex}"
        state_json = self._json_object(checkpoint.state, "checkpoint state")
        manifest_json = self._json_object(checkpoint.schema_manifest, "schema manifest")
        now = time.time()
        with self.store._lock:
            conn = self.store._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT 1 FROM run_checkpoints WHERE organization_id=? AND workspace_id=? "
                    "AND run_id=? AND checkpoint_id=?",
                    (organization_id, workspace_id, source_run_id, checkpoint_id)).fetchone()
                if current is None:
                    raise CheckpointError("source checkpoint does not exist in run")
                conn.execute(
                    "INSERT INTO professional_runs(run_id,organization_id,workspace_id,kind,"
                    "status,schema_manifest_json,head_checkpoint_id,parent_run_id,"
                    "forked_from_checkpoint_id,created_by,created_ts,updated_ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, organization_id, workspace_id, source.kind, "running",
                     manifest_json, initial_id, source_run_id, checkpoint_id,
                     actor_id, now, now))
                conn.execute(
                    "INSERT INTO run_checkpoints(checkpoint_id,run_id,organization_id,"
                    "workspace_id,parent_checkpoint_id,sequence,state_json,"
                    "schema_manifest_json,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (initial_id, run_id, organization_id, workspace_id, checkpoint_id, 1,
                     state_json, manifest_json, actor_id, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = self.get_run(organization_id, workspace_id, run_id)
        assert result is not None
        return result

    def checkpoint(self, organization_id: str, workspace_id: str, run_id: str, *,
                   actor_id: str, state: dict[str, Any]) -> Checkpoint:
        self._validate_scope(organization_id, workspace_id, actor_id)
        state_json = self._json_object(state, "checkpoint state")
        run = self.get_run(organization_id, workspace_id, run_id)
        if run is None:
            raise CheckpointError("run does not exist in workspace")
        if run.status == "cancelled":
            raise CheckpointError("cancelled run cannot be checkpointed")
        if run.status not in {"running", "interrupted"}:
            raise CheckpointError(f"{run.status} run cannot be checkpointed")
        checkpoint_id = f"checkpoint-{uuid.uuid4().hex}"
        now = time.time()
        with self.store._lock:
            conn = self.store._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT head_checkpoint_id,status FROM professional_runs WHERE "
                    "organization_id=? AND workspace_id=? AND run_id=?",
                    (organization_id, workspace_id, run_id)).fetchone()
                if current is None:
                    raise CheckpointError("run does not exist in workspace")
                if current[1] == "cancelled":
                    raise CheckpointError("cancelled run cannot be checkpointed")
                sequence_row = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM run_checkpoints "
                    "WHERE run_id=?", (run_id,)).fetchone()
                sequence = int(sequence_row[0])
                conn.execute(
                    "INSERT INTO run_checkpoints(checkpoint_id,run_id,organization_id,"
                    "workspace_id,parent_checkpoint_id,sequence,state_json,"
                    "schema_manifest_json,created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (checkpoint_id, run_id, organization_id, workspace_id,
                     current[0] or None, sequence, state_json,
                     self._json_object(run.schema_manifest, "schema manifest"), actor_id, now))
                conn.execute(
                    "UPDATE professional_runs SET head_checkpoint_id=?,updated_ts=? "
                    "WHERE run_id=?", (checkpoint_id, now, run_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        result = self.latest(organization_id, workspace_id, run_id)
        assert result is not None
        return result

    def interrupt(self, organization_id: str, workspace_id: str, run_id: str, *,
                  actor_id: str, interrupt_type: str,
                  payload: dict[str, Any]) -> WorkflowRun:
        if interrupt_type not in INTERRUPT_TYPES:
            raise CheckpointError(f"unknown interrupt type: {interrupt_type}")
        payload_json = self._json_object(payload, "interrupt payload")
        return self._transition(organization_id, workspace_id, run_id, actor_id,
                                "interrupted", interrupt_type=interrupt_type,
                                interrupt_payload_json=payload_json)

    def resume(self, organization_id: str, workspace_id: str, run_id: str, *,
               actor_id: str, schema_manifest: dict[str, Any]) -> WorkflowRun:
        run = self.get_run(organization_id, workspace_id, run_id)
        if run is None:
            raise CheckpointError("run does not exist in workspace")
        manifest_json = self._json_object(schema_manifest, "schema manifest")
        if manifest_json != self._json_object(run.schema_manifest, "schema manifest"):
            raise CheckpointError("schema manifest is not resume-compatible")
        if run.status != "interrupted":
            raise CheckpointError("only interrupted runs can resume")
        return self._transition(organization_id, workspace_id, run_id, actor_id, "running")

    def cancel(self, organization_id: str, workspace_id: str, run_id: str, *,
               actor_id: str, reason: str) -> WorkflowRun:
        if not reason.strip():
            raise CheckpointError("cancel reason is required")
        return self._transition(organization_id, workspace_id, run_id, actor_id,
                                "cancelled", cancel_reason=reason.strip())

    def record_effect(self, organization_id: str, workspace_id: str, run_id: str, *,
                      actor_id: str, idempotency_key: str, effect_type: str,
                      request: dict[str, Any], result: dict[str, Any]) -> tuple[EffectReceipt, bool]:
        self._validate_scope(organization_id, workspace_id, actor_id)
        key = idempotency_key.strip()
        if not key or len(key) > 256:
            raise CheckpointError("idempotency key must contain 1 to 256 characters")
        if not effect_type.strip():
            raise CheckpointError("effect type is required")
        request_json = self._json_object(request, "effect request")
        result_json = self._json_object(result, "effect result")
        fingerprint = hashlib.sha256(
            (effect_type.strip() + "\n" + request_json).encode()).hexdigest()
        now = time.time()
        receipt_id = f"effect-{uuid.uuid4().hex}"
        with self.store._lock:
            conn = self.store._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                run = conn.execute(
                    "SELECT status FROM professional_runs WHERE organization_id=? "
                    "AND workspace_id=? AND run_id=?",
                    (organization_id, workspace_id, run_id)).fetchone()
                if run is None:
                    raise CheckpointError("run does not exist in workspace")
                existing = conn.execute(
                    "SELECT * FROM run_effect_receipts WHERE run_id=? AND idempotency_key=?",
                    (run_id, key)).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise CheckpointError("idempotency key conflicts with another effect")
                    conn.commit()
                    return self._receipt(dict(existing)), True
                if run["status"] != "running":
                    raise CheckpointError("effects require a running run")
                conn.execute(
                    "INSERT INTO run_effect_receipts(receipt_id,run_id,organization_id,"
                    "workspace_id,idempotency_key,fingerprint,effect_type,result_json,"
                    "created_by,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, run_id, organization_id, workspace_id, key, fingerprint,
                     effect_type.strip(), result_json, actor_id, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        receipt = self.get_effect(organization_id, workspace_id, run_id, key)
        assert receipt is not None
        return receipt, False

    def get_effect(self, organization_id: str, workspace_id: str, run_id: str,
                   idempotency_key: str) -> EffectReceipt | None:
        row = self.store._directory_one(
            "SELECT * FROM run_effect_receipts WHERE organization_id=? AND workspace_id=? "
            "AND run_id=? AND idempotency_key=?",
            (organization_id, workspace_id, run_id, idempotency_key))
        return self._receipt(row) if row else None

    def _transition(self, organization_id: str, workspace_id: str, run_id: str,
                    actor_id: str, status: str, *, interrupt_type: str = "",
                    interrupt_payload_json: str = "{}", cancel_reason: str = "") -> WorkflowRun:
        self._validate_scope(organization_id, workspace_id, actor_id)
        RunState(status, interrupt_type)
        now = time.time()
        self.store._directory_execute(
            "UPDATE professional_runs SET status=?,interrupt_type=?,"
            "interrupt_payload_json=?,cancel_reason=?,updated_ts=? WHERE "
            "organization_id=? AND workspace_id=? AND run_id=?",
            (status, interrupt_type, interrupt_payload_json, cancel_reason, now,
             organization_id, workspace_id, run_id))
        result = self.get_run(organization_id, workspace_id, run_id)
        if result is None:
            raise CheckpointError("run does not exist in workspace")
        return result

    def _validate_scope(self, organization_id: str, workspace_id: str,
                        actor_id: str) -> None:
        row = self.store._directory_one(
            "SELECT 1 FROM professional_workspaces w JOIN organizations o ON "
            "o.organization_id=w.organization_id JOIN organization_memberships m ON "
            "m.organization_id=w.organization_id WHERE w.organization_id=? AND "
            "w.workspace_id=? AND w.status='active' AND o.status='active' AND "
            "m.user_id=? AND m.status='active'", (organization_id, workspace_id, actor_id))
        if row is None:
            raise CheckpointError("active workspace membership is required")

    @staticmethod
    def _json_object(value: Any, label: str) -> str:
        if type(value) is not dict:
            raise CheckpointError(f"{label} must be a JSON object")
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"),
                              allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise CheckpointError(f"{label} must be strict JSON") from exc

    @staticmethod
    def _run(row: dict[str, Any]) -> WorkflowRun:
        return WorkflowRun(
            run_id=row["run_id"], organization_id=row["organization_id"],
            workspace_id=row["workspace_id"], kind=row["kind"], status=row["status"],
            schema_manifest=json.loads(row["schema_manifest_json"]),
            head_checkpoint_id=row["head_checkpoint_id"],
            parent_run_id=row["parent_run_id"],
            forked_from_checkpoint_id=row["forked_from_checkpoint_id"],
            interrupt_type=row["interrupt_type"],
            interrupt_payload=json.loads(row["interrupt_payload_json"]),
            cancel_reason=row["cancel_reason"], created_by=row["created_by"],
            created_ts=float(row["created_ts"]), updated_ts=float(row["updated_ts"]))

    @staticmethod
    def _checkpoint(row: dict[str, Any]) -> Checkpoint:
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"], run_id=row["run_id"],
            organization_id=row["organization_id"], workspace_id=row["workspace_id"],
            parent_checkpoint_id=row["parent_checkpoint_id"], sequence=int(row["sequence"]),
            state=json.loads(row["state_json"]),
            schema_manifest=json.loads(row["schema_manifest_json"]),
            created_by=row["created_by"], created_ts=float(row["created_ts"]))

    @staticmethod
    def _receipt(row: dict[str, Any]) -> EffectReceipt:
        return EffectReceipt(
            receipt_id=row["receipt_id"], run_id=row["run_id"],
            organization_id=row["organization_id"], workspace_id=row["workspace_id"],
            idempotency_key=row["idempotency_key"], fingerprint=row["fingerprint"],
            effect_type=row["effect_type"], result=json.loads(row["result_json"]),
            created_by=row["created_by"], created_ts=float(row["created_ts"]))
