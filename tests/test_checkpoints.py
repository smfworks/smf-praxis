"""Durable professional workflow runs and checkpoint contracts."""
from __future__ import annotations

import math

import pytest

from hybridagent.checkpoints import CheckpointError, CheckpointRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.run_state import RunState
from hybridagent.workspaces import WorkspaceDirectory


def setup_scope(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-001", "matter", "Matter",
        owner_user_id=owner.user_id)
    return store, org.organization_id, workspace.workspace_id, owner.user_id


def test_run_and_checkpoint_survive_restart(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    run = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={"query": "authority", "pending_tasks": ["collect"]},
        schema_manifest={"name": "research", "version": 1})
    checkpoint = registry.checkpoint(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        state={"query": "authority", "pending_tasks": ["normalize"]})

    reopened = CheckpointRegistry(Store(store.path))
    loaded = reopened.get_run(org_id, workspace_id, run.run_id)
    latest = reopened.latest(org_id, workspace_id, run.run_id)
    assert loaded is not None and loaded.status == "running"
    assert latest == checkpoint
    assert latest.state["pending_tasks"] == ["normalize"]
    assert latest.parent_checkpoint_id == run.head_checkpoint_id


def test_cross_workspace_lookup_is_concealed(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    other = WorkspaceDirectory(store).create(
        org_id, "MAT-002", "matter", "Other", owner_user_id=actor_id)
    registry = CheckpointRegistry(store)
    run = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={}, schema_manifest={"name": "research", "version": 1})
    assert registry.get_run(org_id, other.workspace_id, run.run_id) is None
    assert registry.latest(org_id, other.workspace_id, run.run_id) is None


@pytest.mark.parametrize("bad", [
    {"value": math.nan}, {"value": math.inf}, {"value": object()},
    {"value": {1, 2}}, {"value": b"secret"},
])
def test_checkpoint_state_is_strict_json(bad, tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    with pytest.raises(CheckpointError, match="JSON"):
        registry.create_run(
            org_id, workspace_id, kind="research", created_by=actor_id,
            state=bad, schema_manifest={"name": "research", "version": 1})


def test_typed_interrupt_cancel_and_resume_compatibility(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    run = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={"step": 1}, schema_manifest={"name": "research", "version": 1})
    interrupted = registry.interrupt(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        interrupt_type="professional_review", payload={"review_id": "rev-1"})
    assert interrupted.status == "interrupted"
    assert interrupted.interrupt_type == "professional_review"

    with pytest.raises(CheckpointError, match="schema manifest"):
        registry.resume(
            org_id, workspace_id, run.run_id, actor_id=actor_id,
            schema_manifest={"name": "research", "version": 2})

    resumed = registry.resume(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        schema_manifest={"name": "research", "version": 1})
    assert resumed.status == "running"
    cancelled = registry.cancel(
        org_id, workspace_id, run.run_id, actor_id=actor_id, reason="operator request")
    assert cancelled.status == "cancelled"
    with pytest.raises(CheckpointError, match="cancelled"):
        registry.checkpoint(
            org_id, workspace_id, run.run_id, actor_id=actor_id, state={"step": 2})


def test_run_state_has_closed_status_and_interrupt_vocabularies():
    with pytest.raises(ValueError):
        RunState(status="invented")
    with pytest.raises(ValueError):
        RunState(status="interrupted", interrupt_type="invented")


def test_effect_receipt_is_exactly_once_and_survives_restart(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    run = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={}, schema_manifest={"name": "research", "version": 1})
    first, replayed = registry.record_effect(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        idempotency_key="send-1", effect_type="send_message",
        request={"recipient": "client"}, result={"message_id": "m-1"})
    assert replayed is False
    reopened = CheckpointRegistry(Store(store.path))
    second, replayed = reopened.record_effect(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        idempotency_key="send-1", effect_type="send_message",
        request={"recipient": "client"}, result={"message_id": "ignored"})
    assert replayed is True
    assert second == first
    assert second.result == {"message_id": "m-1"}


def test_effect_receipt_rejects_key_reuse_with_different_request(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    run = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={}, schema_manifest={"name": "research", "version": 1})
    registry.record_effect(
        org_id, workspace_id, run.run_id, actor_id=actor_id,
        idempotency_key="effect-1", effect_type="export",
        request={"format": "pdf"}, result={"artifact": "a"})
    with pytest.raises(CheckpointError, match="conflicts"):
        registry.record_effect(
            org_id, workspace_id, run.run_id, actor_id=actor_id,
            idempotency_key="effect-1", effect_type="export",
            request={"format": "docx"}, result={"artifact": "b"})


def test_fork_replays_checkpoint_state_without_inheriting_effects(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    registry = CheckpointRegistry(store)
    source = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={"step": 1}, schema_manifest={"name": "research", "version": 1})
    selected = registry.checkpoint(
        org_id, workspace_id, source.run_id, actor_id=actor_id, state={"step": 2})
    registry.record_effect(
        org_id, workspace_id, source.run_id, actor_id=actor_id,
        idempotency_key="publish-1", effect_type="publish",
        request={"artifact": "a"}, result={"url": "https://example.test/a"})

    fork = registry.fork(
        org_id, workspace_id, source.run_id,
        checkpoint_id=selected.checkpoint_id, actor_id=actor_id)
    replay = registry.latest(org_id, workspace_id, fork.run_id)
    assert fork.parent_run_id == source.run_id
    assert fork.forked_from_checkpoint_id == selected.checkpoint_id
    assert replay is not None and replay.state == {"step": 2}
    assert replay.parent_checkpoint_id == selected.checkpoint_id
    assert registry.get_effect(org_id, workspace_id, fork.run_id, "publish-1") is None


def test_fork_conceals_checkpoint_from_another_workspace(tmp_path):
    store, org_id, workspace_id, actor_id = setup_scope(tmp_path)
    other = WorkspaceDirectory(store).create(
        org_id, "MAT-002", "matter", "Other", owner_user_id=actor_id)
    registry = CheckpointRegistry(store)
    source = registry.create_run(
        org_id, workspace_id, kind="research", created_by=actor_id,
        state={"step": 1}, schema_manifest={"name": "research", "version": 1})
    with pytest.raises(CheckpointError, match="does not exist"):
        registry.fork(
            org_id, other.workspace_id, source.run_id,
            checkpoint_id=source.head_checkpoint_id, actor_id=actor_id)
