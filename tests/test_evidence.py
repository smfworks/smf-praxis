"""Canonical source and immutable evidence-version contracts."""

import sqlite3

import pytest

from hybridagent.evidence import EvidenceError, EvidenceRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def setup_registry(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, owner = organizations.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        organization.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    return store, organization, owner, workspace, EvidenceRegistry(store)


def test_source_identity_is_tenant_and_workspace_scoped(tmp_path):
    _, organization, owner, workspace, registry = setup_registry(tmp_path)
    source = registry.create_source(
        organization.organization_id, workspace.workspace_id,
        canonical_uri="https://court.example/opinion/42",
        publisher="Example Court", author="Justice Example",
        jurisdiction="US-EX", authority_tier="binding",
        created_by=owner.user_id)
    assert source.organization_id == organization.organization_id
    assert source.workspace_id == workspace.workspace_id
    assert registry.get_source(
        organization.organization_id, workspace.workspace_id,
        source.source_id) == source
    assert registry.get_source(
        organization.organization_id, "ws-forged", source.source_id) is None


def test_source_versions_are_immutable_and_hash_verified(tmp_path):
    _, organization, owner, workspace, registry = setup_registry(tmp_path)
    source = registry.create_source(
        organization.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/source", publisher="Publisher",
        created_by=owner.user_id)
    version = registry.add_version(
        organization.organization_id, workspace.workspace_id, source.source_id,
        content=b"authoritative bytes", mime_type="text/plain",
        retrieved_ts=1_700_000_000.0, parser="plain-text", parser_version="1",
        parser_config={"encoding": "utf-8"}, license="permitted",
        original_object_path="objects/source-v1.txt", created_by=owner.user_id)
    assert len(version.content_hash) == 64
    assert registry.verify_content(version.version_id, b"authoritative bytes")
    assert not registry.verify_content(version.version_id, b"tampered")
    with pytest.raises(EvidenceError, match="immutable"):
        registry.update_version(version.version_id, mime_type="text/html")


def test_supersession_preserves_both_versions(tmp_path):
    _, organization, owner, workspace, registry = setup_registry(tmp_path)
    source = registry.create_source(
        organization.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/rule", publisher="Authority",
        created_by=owner.user_id)
    first = registry.add_version(
        organization.organization_id, workspace.workspace_id, source.source_id,
        content=b"v1", mime_type="text/plain", retrieved_ts=1.0,
        parser="plain", parser_version="1", parser_config={}, license="public",
        original_object_path="objects/v1", created_by=owner.user_id)
    second = registry.add_version(
        organization.organization_id, workspace.workspace_id, source.source_id,
        content=b"v2", mime_type="text/plain", retrieved_ts=2.0,
        parser="plain", parser_version="1", parser_config={}, license="public",
        original_object_path="objects/v2", created_by=owner.user_id,
        supersedes_version_id=first.version_id)
    versions = registry.list_versions(
        organization.organization_id, workspace.workspace_id, source.source_id)
    assert [item.version_id for item in versions] == [first.version_id, second.version_id]
    assert versions[0].superseded_by_version_id == second.version_id


def test_version_is_immutable_below_the_service_boundary(tmp_path):
    store, organization, owner, workspace, registry = setup_registry(tmp_path)
    source = registry.create_source(
        organization.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/immutable", publisher="Authority",
        created_by=owner.user_id)
    version = registry.add_version(
        organization.organization_id, workspace.workspace_id, source.source_id,
        content=b"original", mime_type="text/plain", retrieved_ts=1.0,
        parser="plain", parser_version="1", parser_config={}, license="public",
        original_object_path="objects/original", created_by=owner.user_id)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._directory_execute(
            "UPDATE evidence_source_versions SET mime_type='text/html' "
            "WHERE version_id=?", (version.version_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._directory_execute(
            "DELETE FROM evidence_source_versions WHERE version_id=?",
            (version.version_id,))


def test_versions_survive_restart_and_reject_cross_workspace_supersession(tmp_path):
    store, organization, owner, workspace, registry = setup_registry(tmp_path)
    source = registry.create_source(
        organization.organization_id, workspace.workspace_id,
        canonical_uri="https://example.test/durable", publisher="Authority",
        created_by=owner.user_id)
    first = registry.add_version(
        organization.organization_id, workspace.workspace_id, source.source_id,
        content=b"durable", mime_type="text/plain", retrieved_ts=1.0,
        parser="plain", parser_version="1", parser_config={}, license="public",
        original_object_path="objects/durable", created_by=owner.user_id)
    restarted = EvidenceRegistry(Store(store.path))
    assert restarted.verify_content(first.version_id, b"durable")
    other = WorkspaceDirectory(restarted.store).create(
        organization.organization_id, "MAT-2", "matter", "Other matter",
        owner_user_id=owner.user_id)
    other_source = restarted.create_source(
        organization.organization_id, other.workspace_id,
        canonical_uri="https://example.test/other", publisher="Authority",
        created_by=owner.user_id)
    with pytest.raises(EvidenceError, match="does not belong"):
        restarted.add_version(
            organization.organization_id, other.workspace_id, other_source.source_id,
            content=b"wrong scope", mime_type="text/plain", retrieved_ts=2.0,
            parser="plain", parser_version="1", parser_config={}, license="public",
            original_object_path="objects/wrong", created_by=owner.user_id,
            supersedes_version_id=first.version_id)
    assert restarted.list_versions(
        organization.organization_id, other.workspace_id, other_source.source_id) == []


def test_inactive_actor_cannot_create_evidence(tmp_path):
    _, organization, owner, workspace, registry = setup_registry(tmp_path)
    registry.store._directory_execute(
        "UPDATE organization_users SET status='disabled' WHERE user_id=?",
        (owner.user_id,))
    with pytest.raises(EvidenceError, match="active organization member"):
        registry.create_source(
            organization.organization_id, workspace.workspace_id,
            canonical_uri="https://example.test/denied", publisher="Authority",
            created_by=owner.user_id)
