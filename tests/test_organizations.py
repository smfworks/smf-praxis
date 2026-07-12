"""Organization and membership isolation contracts for professional Praxis."""
from __future__ import annotations

import pytest

from hybridagent.organizations import OrganizationDirectory, OrganizationError
from hybridagent.persistence import Store


def directory(tmp_path) -> OrganizationDirectory:
    return OrganizationDirectory(Store(tmp_path / "praxis.db"))


def test_create_organization_and_local_admin(tmp_path):
    orgs = directory(tmp_path)
    organization, admin = orgs.bootstrap("SMF Legal", "michael@example.com")
    assert organization.name == "SMF Legal"
    assert organization.status == "active"
    assert admin.email == "michael@example.com"
    membership = orgs.membership(organization.organization_id, admin.user_id)
    assert membership.roles == ("organization_admin",)
    assert membership.status == "active"


def test_email_is_normalized_and_unique(tmp_path):
    orgs = directory(tmp_path)
    user = orgs.create_user(" Michael@Example.COM ", display_name="Michael")
    assert user.email == "michael@example.com"
    with pytest.raises(OrganizationError, match="already exists"):
        orgs.create_user("michael@example.com")


def test_membership_is_scoped_to_organization(tmp_path):
    orgs = directory(tmp_path)
    first = orgs.create_organization("First")
    second = orgs.create_organization("Second")
    user = orgs.create_user("reviewer@example.com")
    orgs.add_membership(first.organization_id, user.user_id, roles=("reviewer",))

    assert orgs.membership(first.organization_id, user.user_id).roles == ("reviewer",)
    assert orgs.membership(second.organization_id, user.user_id) is None
    assert orgs.members_for(second.organization_id) == []


def test_role_vocabulary_rejects_unknown_role(tmp_path):
    orgs = directory(tmp_path)
    organization = orgs.create_organization("Practice")
    user = orgs.create_user("user@example.com")
    with pytest.raises(OrganizationError, match="unknown role"):
        orgs.add_membership(organization.organization_id, user.user_id,
                            roles=("supergod",))


def test_team_members_cannot_cross_organization_boundary(tmp_path):
    orgs = directory(tmp_path)
    first = orgs.create_organization("First")
    second = orgs.create_organization("Second")
    first_user = orgs.create_user("first@example.com")
    second_user = orgs.create_user("second@example.com")
    orgs.add_membership(first.organization_id, first_user.user_id, roles=("member",))
    orgs.add_membership(second.organization_id, second_user.user_id, roles=("member",))
    team = orgs.create_team(first.organization_id, "Matter Team")

    with pytest.raises(OrganizationError, match="not a member"):
        orgs.add_team_member(team.team_id, second_user.user_id)
    orgs.add_team_member(team.team_id, first_user.user_id)
    assert [member.user_id for member in orgs.team_members(team.team_id)] == [
        first_user.user_id]


def test_directory_survives_restart(tmp_path):
    path = tmp_path / "praxis.db"
    first = OrganizationDirectory(Store(path))
    organization, admin = first.bootstrap("Persistent Practice", "admin@example.com")

    second = OrganizationDirectory(Store(path))
    assert second.organization(organization.organization_id).name == "Persistent Practice"
    assert second.user(admin.user_id).email == "admin@example.com"
    assert second.membership(organization.organization_id, admin.user_id) is not None


def test_board_storage_is_tenant_scoped(tmp_path):
    store = Store(tmp_path / "praxis.db")
    store.add_card("a", "A", "A", organization_id="org-a")
    store.add_card("b", "B", "B", organization_id="org-b")
    assert [card["card_id"] for card in store.list_cards(
        organization_id="org-a")] == ["a"]
    assert [card["card_id"] for card in store.list_cards(
        organization_id="org-b")] == ["b"]


def test_idempotency_key_is_namespaced_by_tenant(tmp_path):
    from hybridagent.daemon import Daemon
    from hybridagent.llm import LLMClient

    daemon = Daemon(store=Store(tmp_path / "praxis.db"), llm=LLMClient(mode="mock"))
    first, replay_a, conflict_a = daemon.api_idempotent_board_create(
        "same", "fp", "A", "A", "org-a")
    second, replay_b, conflict_b = daemon.api_idempotent_board_create(
        "same", "fp", "B", "B", "org-b")
    assert not replay_a and not replay_b and not conflict_a and not conflict_b
    assert first["card"]["card_id"] != second["card"]["card_id"]
    assert daemon.store is not None
    assert len(daemon.store.list_cards(organization_id="org-a")) == 1
    assert len(daemon.store.list_cards(organization_id="org-b")) == 1


def test_disabled_member_cannot_join_team(tmp_path):
    orgs = directory(tmp_path)
    organization = orgs.create_organization("Practice")
    user = orgs.create_user("disabled@example.com")
    orgs.add_membership(organization.organization_id, user.user_id)
    team = orgs.create_team(organization.organization_id, "Team")
    orgs.store._directory_execute(
        "UPDATE organization_memberships SET status='disabled' "
        "WHERE organization_id=? AND user_id=?",
        (organization.organization_id, user.user_id))
    with pytest.raises(OrganizationError, match="inactive"):
        orgs.add_team_member(team.team_id, user.user_id)
