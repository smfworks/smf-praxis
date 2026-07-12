"""Append-only evidence custody ledger contracts."""
import sqlite3

import pytest

from hybridagent.custody import CustodyError, CustodyLedger
from hybridagent.evidence import EvidenceRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def setup_custody(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Lab", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "CASE-1", "forensic_case", "Case",
        owner_user_id=owner.user_id)
    evidence = EvidenceRegistry(store)
    source = evidence.create_source(
        org.organization_id, workspace.workspace_id,
        canonical_uri="file:///evidence/device.img", publisher="Lab",
        created_by=owner.user_id)
    version = evidence.add_version(
        org.organization_id, workspace.workspace_id, source.source_id,
        content=b"disk image", mime_type="application/octet-stream", retrieved_ts=1.0,
        parser="raw", parser_version="1", parser_config={}, license="authorized",
        original_object_path="objects/device.img", created_by=owner.user_id)
    return store, org, owner, workspace, version, CustodyLedger(store)


def test_custody_events_are_ordered_and_hash_chained(tmp_path):
    _, org, owner, workspace, version, ledger = setup_custody(tmp_path)
    first = ledger.record(
        org.organization_id, workspace.workspace_id, version.version_id,
        event_type="acquisition", actor_id=owner.user_id, tool_id="imager-1",
        occurred_ts=10.0, details={"location": "Lab A"})
    second = ledger.record(
        org.organization_id, workspace.workspace_id, version.version_id,
        event_type="verification", actor_id=owner.user_id, tool_id="sha256",
        occurred_ts=11.0, details={"result": "match"})
    assert (first.sequence, second.sequence) == (1, 2)
    assert second.previous_event_hash == first.event_hash
    assert ledger.verify_chain(
        org.organization_id, workspace.workspace_id, version.version_id)


def test_custody_rejects_unknown_event_and_cross_workspace_version(tmp_path):
    _, org, owner, workspace, version, ledger = setup_custody(tmp_path)
    with pytest.raises(CustodyError, match="unknown custody event"):
        ledger.record(
            org.organization_id, workspace.workspace_id, version.version_id,
            event_type="edited", actor_id=owner.user_id, tool_id="tool",
            occurred_ts=1.0, details={})
    with pytest.raises(CustodyError, match="does not exist"):
        ledger.record(
            org.organization_id, "ws-forged", version.version_id,
            event_type="analysis", actor_id=owner.user_id, tool_id="tool",
            occurred_ts=1.0, details={})


def test_custody_is_immutable_at_sqlite_boundary(tmp_path):
    store, org, owner, workspace, version, ledger = setup_custody(tmp_path)
    event = ledger.record(
        org.organization_id, workspace.workspace_id, version.version_id,
        event_type="analysis", actor_id=owner.user_id, tool_id="tool-1",
        occurred_ts=2.0, details={"purpose": "review"})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._directory_execute(
            "UPDATE evidence_custody_events SET tool_id='other' WHERE event_id=?",
            (event.event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._directory_execute(
            "DELETE FROM evidence_custody_events WHERE event_id=?", (event.event_id,))
