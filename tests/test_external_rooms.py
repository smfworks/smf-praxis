"""Controlled external collaboration room contracts."""

import time

import pytest

from hybridagent.external_rooms import ExternalRoomDirectory, ExternalRoomError
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def setup_room(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, owner = organizations.bootstrap("Practice", "owner@example.com")
    external = organizations.create_user("expert@example.com")
    organizations.add_membership(
        organization.organization_id, external.user_id,
        roles=("external_collaborator",))
    workspace = WorkspaceDirectory(store).create(
        organization.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    return store, organization, owner, external, workspace


def test_room_is_owned_by_one_workspace_and_invites_are_explicit(tmp_path):
    store, organization, owner, external, workspace = setup_room(tmp_path)
    rooms = ExternalRoomDirectory(store)
    room = rooms.create(
        organization.organization_id, workspace.workspace_id, "Expert Review",
        created_by=owner.user_id, permissions=("read_shared", "comment"))
    invitation = rooms.invite(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, invited_by=owner.user_id)
    assert invitation.status == "active"
    assert rooms.authorize(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, "comment").allowed
    assert not rooms.authorize(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, "read_workspace_memory").allowed


def test_cross_workspace_and_cross_tenant_room_access_is_denied(tmp_path):
    store, organization, owner, external, workspace = setup_room(tmp_path)
    directories = OrganizationDirectory(store)
    other_org, other_owner = directories.bootstrap("Other", "other@example.com")
    other_workspace = WorkspaceDirectory(store).create(
        other_org.organization_id, "OTH-1", "matter", "Other",
        owner_user_id=other_owner.user_id)
    rooms = ExternalRoomDirectory(store)
    room = rooms.create(
        organization.organization_id, workspace.workspace_id, "Room",
        created_by=owner.user_id)
    with pytest.raises(ExternalRoomError, match="room"):
        rooms.invite(
            other_org.organization_id, other_workspace.workspace_id, room.room_id,
            other_owner.user_id, invited_by=other_owner.user_id)
    assert not rooms.authorize(
        organization.organization_id, other_workspace.workspace_id, room.room_id,
        external.user_id, "read_shared").allowed


def test_room_permissions_are_closed_and_cannot_grant_execution(tmp_path):
    store, organization, owner, _, workspace = setup_room(tmp_path)
    rooms = ExternalRoomDirectory(store)
    with pytest.raises(ExternalRoomError, match="permission"):
        rooms.create(
            organization.organization_id, workspace.workspace_id, "Dangerous",
            created_by=owner.user_id, permissions=("execute_tool",))


def test_invitation_expiry_and_revocation_are_enforced(tmp_path):
    store, organization, owner, external, workspace = setup_room(tmp_path)
    rooms = ExternalRoomDirectory(store)
    room = rooms.create(
        organization.organization_id, workspace.workspace_id, "Temporary",
        created_by=owner.user_id)
    rooms.invite(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, invited_by=owner.user_id, expires_ts=time.time() - 1)
    assert not rooms.authorize(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, "read_shared").allowed
    rooms.invite(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, invited_by=owner.user_id, replace=True)
    rooms.revoke(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, revoked_by=owner.user_id)
    assert not rooms.authorize(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, "comment").allowed


def test_shared_items_are_allowlisted_not_workspace_wide(tmp_path):
    store, organization, owner, external, workspace = setup_room(tmp_path)
    rooms = ExternalRoomDirectory(store)
    room = rooms.create(
        organization.organization_id, workspace.workspace_id, "Disclosure",
        created_by=owner.user_id)
    rooms.invite(
        organization.organization_id, workspace.workspace_id, room.room_id,
        external.user_id, invited_by=owner.user_id)
    rooms.register_resource(
        organization.organization_id, workspace.workspace_id,
        "artifact", "artifact-1")
    rooms.share_item(
        organization.organization_id, workspace.workspace_id, room.room_id,
        "artifact", "artifact-1", shared_by=owner.user_id)
    assert rooms.shared_items(
        organization.organization_id, workspace.workspace_id, room.room_id) == [
        {"item_type": "artifact", "item_id": "artifact-1"}]
    assert not rooms.is_item_shared(
        organization.organization_id, workspace.workspace_id, room.room_id,
        "artifact", "artifact-2")
    with pytest.raises(ExternalRoomError, match="does not exist"):
        rooms.share_item(
            organization.organization_id, workspace.workspace_id, room.room_id,
            "artifact", "fabricated", shared_by=owner.user_id)


def test_room_mutations_require_active_privileged_principals(tmp_path):
    store, organization, owner, external, workspace = setup_room(tmp_path)
    rooms = ExternalRoomDirectory(store)
    room = rooms.create(
        organization.organization_id, workspace.workspace_id, "Managed",
        created_by=owner.user_id)
    member = OrganizationDirectory(store).create_user("member@example.com")
    OrganizationDirectory(store).add_membership(
        organization.organization_id, member.user_id, roles=("member",))
    with pytest.raises(ExternalRoomError, match="role"):
        rooms.invite(
            organization.organization_id, workspace.workspace_id, room.room_id,
            external.user_id, invited_by=member.user_id)
    store._directory_execute(
        "UPDATE organization_users SET status='disabled' WHERE user_id=?",
        (owner.user_id,))
    with pytest.raises(ExternalRoomError, match="active"):
        rooms.revoke(
            organization.organization_id, workspace.workspace_id, room.room_id,
            external.user_id, revoked_by=owner.user_id)
