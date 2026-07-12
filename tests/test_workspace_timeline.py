"""Phase 2 contracts for workspace parties, deadlines, and immutable timeline."""

import pytest

from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspace_timeline import TimelineError, WorkspaceTimeline
from hybridagent.workspaces import WorkspaceDirectory


def setup_workspace(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, owner = organizations.bootstrap("Practice", "owner@example.com")
    workspace = WorkspaceDirectory(store).create(
        organization.organization_id, "MAT-1", "matter", "Matter",
        owner_user_id=owner.user_id)
    return store, organization, owner, workspace


def test_parties_and_contacts_are_tenant_and_workspace_scoped(tmp_path):
    store, organization, _, workspace = setup_workspace(tmp_path)
    timeline = WorkspaceTimeline(store)
    party = timeline.add_party(
        organization.organization_id, workspace.workspace_id, "person", "Jane Client",
        role="client", contacts=({"kind": "email", "value": "jane@example.com"},))
    assert party.contacts[0]["value"] == "jane@example.com"
    assert timeline.parties(organization.organization_id, workspace.workspace_id) == [party]
    assert timeline.parties("org-other", workspace.workspace_id) == []


def test_timeline_events_are_append_only_and_support_typed_links(tmp_path):
    store, organization, owner, workspace = setup_workspace(tmp_path)
    timeline = WorkspaceTimeline(store)
    event = timeline.append_event(
        organization.organization_id, workspace.workspace_id, "filing_received",
        "Claim form received", actor_user_id=owner.user_id,
        links=({"type": "external_record", "id": "DMS-42"},
               {"type": "task", "id": "task-1"}))
    assert event.sequence == 1
    assert event.links[0]["type"] == "external_record"
    second = timeline.append_event(
        organization.organization_id, workspace.workspace_id, "note", "Reviewed",
        actor_user_id=owner.user_id)
    assert second.sequence == 2
    with pytest.raises(TimelineError, match="append-only"):
        timeline.update_event(
            organization.organization_id, workspace.workspace_id,
            event.event_id, summary="Changed")
    with pytest.raises(Exception, match="append-only"):
        store._directory_execute(
            "UPDATE workspace_timeline_events SET summary='tampered' WHERE event_id=?",
            (event.event_id,))
    with pytest.raises(Exception, match="append-only"):
        store._directory_execute(
            "DELETE FROM workspace_timeline_events WHERE event_id=?",
            (event.event_id,))


def test_timeline_rejects_cross_tenant_workspace_and_actor(tmp_path):
    store, organization, owner, workspace = setup_workspace(tmp_path)
    organizations = OrganizationDirectory(store)
    other_org, other_user = organizations.bootstrap("Other", "other@example.com")
    timeline = WorkspaceTimeline(store)
    with pytest.raises(TimelineError, match="workspace"):
        timeline.append_event(
            other_org.organization_id, workspace.workspace_id, "note", "Cross tenant",
            actor_user_id=other_user.user_id)
    with pytest.raises(TimelineError, match="actor"):
        timeline.append_event(
            organization.organization_id, workspace.workspace_id, "note", "Wrong actor",
            actor_user_id=other_user.user_id)
    assert owner.user_id != other_user.user_id


def test_consequential_deadline_requires_rule_source_and_review(tmp_path):
    store, organization, owner, workspace = setup_workspace(tmp_path)
    timeline = WorkspaceTimeline(store)
    with pytest.raises(TimelineError, match="source and rule"):
        timeline.add_deadline(
            organization.organization_id, workspace.workspace_id, "Response due",
            "2026-08-01", actor_user_id=owner.user_id, consequential=True)
    deadline = timeline.add_deadline(
        organization.organization_id, workspace.workspace_id, "Response due",
        "2026-08-01", actor_user_id=owner.user_id, consequential=True,
        calculation_source="Court order dated 2026-07-12",
        calculation_rule="20 calendar days", links=({"type": "artifact", "id": "art-1"},))
    assert deadline.review_status == "required"
    reviewed = timeline.review_deadline(
        organization.organization_id, workspace.workspace_id, deadline.deadline_id,
        reviewer_user_id=owner.user_id, decision="approved")
    assert reviewed.review_status == "approved"


def test_timeline_and_deadlines_survive_restart(tmp_path):
    db = tmp_path / "praxis.db"
    store, organization, owner, workspace = setup_workspace(tmp_path)
    timeline = WorkspaceTimeline(store)
    timeline.append_event(
        organization.organization_id, workspace.workspace_id, "created", "Opened",
        actor_user_id=owner.user_id)
    timeline.add_deadline(
        organization.organization_id, workspace.workspace_id, "Internal check",
        "2026-07-20", actor_user_id=owner.user_id)
    store.close()
    reopened = WorkspaceTimeline(Store(db))
    assert len(reopened.events(organization.organization_id, workspace.workspace_id)) == 1
    assert len(reopened.deadlines(organization.organization_id, workspace.workspace_id)) == 1
