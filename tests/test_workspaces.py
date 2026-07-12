"""Phase 2 contracts for tenant-owned professional workspaces."""

import pytest

from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WORKSPACE_KINDS, WorkspaceDirectory, WorkspaceError


def setup_directory(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    org_a, admin_a = organizations.bootstrap("Practice A", "a@example.com")
    org_b, admin_b = organizations.bootstrap("Practice B", "b@example.com")
    team_a = organizations.create_team(org_a.organization_id, "Case Team")
    organizations.add_team_member(team_a.team_id, admin_a.user_id)
    return store, organizations, org_a, admin_a, org_b, admin_b, team_a


def test_workspace_kind_vocabulary_is_closed():
    assert WORKSPACE_KINDS == frozenset({
        "matter", "patient_case", "dental_case", "forensic_case",
        "building_project", "course", "learner_portfolio",
        "consulting_engagement", "technology_engagement",
    })


def test_workspace_is_tenant_owned_and_identifier_is_tenant_unique(tmp_path):
    store, _, org_a, admin_a, org_b, admin_b, _ = setup_directory(tmp_path)
    workspaces = WorkspaceDirectory(store)
    first = workspaces.create(
        org_a.organization_id, "MAT-2026-001", "matter", "Alpha Matter",
        owner_user_id=admin_a.user_id, client_or_subject="Alpha Client")
    second = workspaces.create(
        org_b.organization_id, "MAT-2026-001", "matter", "Beta Matter",
        owner_user_id=admin_b.user_id)
    assert first.workspace_id != second.workspace_id
    with pytest.raises(WorkspaceError, match="identifier already exists"):
        workspaces.create(
            org_a.organization_id, "mat-2026-001", "matter", "Duplicate",
            owner_user_id=admin_a.user_id)
    assert [item.workspace_id for item in workspaces.list_for(org_a.organization_id)] == [
        first.workspace_id]
    assert workspaces.get(org_b.organization_id, first.workspace_id) is None


def test_owner_and_team_must_be_active_members_of_workspace_organization(tmp_path):
    store, organizations, org_a, admin_a, org_b, admin_b, team_a = setup_directory(tmp_path)
    workspaces = WorkspaceDirectory(store)
    with pytest.raises(WorkspaceError, match="owner"):
        workspaces.create(
            org_a.organization_id, "A-1", "matter", "Wrong owner",
            owner_user_id=admin_b.user_id)
    with pytest.raises(WorkspaceError, match="team"):
        workspaces.create(
            org_b.organization_id, "B-1", "matter", "Wrong team",
            owner_user_id=admin_b.user_id, team_id=team_a.team_id)
    organizations.store._directory_execute(
        "UPDATE organization_memberships SET status='disabled' "
        "WHERE organization_id=? AND user_id=?",
        (org_a.organization_id, admin_a.user_id))
    with pytest.raises(WorkspaceError, match="owner"):
        workspaces.create(
            org_a.organization_id, "A-2", "matter", "Disabled owner",
            owner_user_id=admin_a.user_id)


def test_vertical_fields_are_validated_against_declared_schema(tmp_path):
    store, _, org_a, admin_a, *_ = setup_directory(tmp_path)
    workspaces = WorkspaceDirectory(store)
    schema = {
        "court": {"type": "string", "required": True},
        "claim_value": {"type": "number"},
        "urgent": {"type": "boolean"},
    }
    created = workspaces.create(
        org_a.organization_id, "MAT-2", "matter", "Validated",
        owner_user_id=admin_a.user_id, field_schema=schema,
        custom_fields={"court": "High Court", "claim_value": 1250.0,
                       "urgent": False})
    assert created.custom_fields["court"] == "High Court"
    with pytest.raises(WorkspaceError, match="required field"):
        workspaces.create(
            org_a.organization_id, "MAT-3", "matter", "Missing",
            owner_user_id=admin_a.user_id, field_schema=schema,
            custom_fields={"urgent": True})
    with pytest.raises(WorkspaceError, match="unknown field"):
        workspaces.create(
            org_a.organization_id, "MAT-4", "matter", "Unknown",
            owner_user_id=admin_a.user_id, field_schema=schema,
            custom_fields={"court": "Court", "invented": "value"})


def test_external_links_require_system_and_identifier(tmp_path):
    store, _, org_a, admin_a, *_ = setup_directory(tmp_path)
    workspaces = WorkspaceDirectory(store)
    created = workspaces.create(
        org_a.organization_id, "TECH-1", "technology_engagement", "Migration",
        owner_user_id=admin_a.user_id,
        external_links=({"system": "linear", "external_id": "ENG-42"},))
    assert created.external_links[0]["external_id"] == "ENG-42"
    with pytest.raises(WorkspaceError, match="external link"):
        workspaces.create(
            org_a.organization_id, "TECH-2", "technology_engagement", "Invalid",
            owner_user_id=admin_a.user_id,
            external_links=({"system": "linear"},))


def test_archive_and_legal_hold_are_distinct_durable_states(tmp_path):
    db = tmp_path / "praxis.db"
    store, _, org_a, admin_a, *_ = setup_directory(tmp_path)
    workspaces = WorkspaceDirectory(store)
    created = workspaces.create(
        org_a.organization_id, "FOR-1", "forensic_case", "Investigation",
        owner_user_id=admin_a.user_id, confidentiality="evidence")
    archived = workspaces.set_archived(
        org_a.organization_id, created.workspace_id, archived=True)
    held = workspaces.set_hold(
        org_a.organization_id, created.workspace_id, held=True,
        reason="Preservation notice")
    assert archived.status == "archived"
    assert held.legal_hold and held.hold_reason == "Preservation notice"
    store.close()
    reopened = WorkspaceDirectory(Store(db)).get(
        org_a.organization_id, created.workspace_id)
    assert reopened is not None
    assert reopened.status == "archived" and reopened.legal_hold
