"""Structured research supervisor contracts for durable professional workflows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast

import pytest

from hybridagent.checkpoints import CheckpointRegistry
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.workspaces import WorkspaceDirectory


def _scope(tmp_path):
    store = Store(tmp_path / "praxis.db")
    orgs = OrganizationDirectory(store)
    org, owner = orgs.bootstrap("Practice", "owner@example.com")
    reviewer = orgs.create_user("reviewer@example.com")
    orgs.add_membership(org.organization_id, reviewer.user_id, roles=("reviewer",))
    workspace = WorkspaceDirectory(store).create(
        org.organization_id, "MAT-1", "matter", "Matter", owner_user_id=owner.user_id
    )
    checkpoints = CheckpointRegistry(store)
    return (
        store,
        checkpoints,
        org.organization_id,
        workspace.workspace_id,
        owner.user_id,
        reviewer.user_id,
    )


def test_research_supervisor_records_findings_and_requests_review(tmp_path):
    from hybridagent.research_run import ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    supervisor = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id
    )

    run = supervisor.start("authority question", hypotheses=["policy first"])
    supervisor.record_finding(
        run.run_id,
        {
            "claim": "Authority A governs the issue",
            "confidence": "high",
            "source_ids": ["src-1"],
        },
    )
    review = supervisor.request_review(run.run_id, required_role="reviewer")
    durable = checkpoints.get_run(org_id, workspace_id, run.run_id)
    state = supervisor.load(run.run_id)
    assert review.review_type == "research_findings"
    assert durable is not None and durable.status == "interrupted"
    assert durable.interrupt_type == "professional_review"
    assert state["status"] == "pending_review"
    assert state["findings"][0]["claim"] == "Authority A governs the issue"
    assert state["review"]["review_id"] == review.review_id
    assert review.subject["checkpoint_id"] == durable.head_checkpoint_id

    from hybridagent.research_run import ResearchRunError

    with pytest.raises(ResearchRunError, match="pending review"):
        supervisor.record_finding(
            run.run_id,
            {"claim": "unreviewed mutation", "confidence": "low", "source_ids": []},
        )


def test_research_supervisor_applies_review_and_resumes_run(tmp_path):
    from hybridagent.research_run import ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    supervisor = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id
    )

    run = supervisor.start("authority question")
    supervisor.record_finding(
        run.run_id,
        {"claim": "Authority A governs the issue", "confidence": "high", "source_ids": ["src-1"]},
    )
    review = supervisor.request_review(run.run_id, required_role="reviewer")
    reviews.submit_decision(
        org_id,
        workspace_id,
        review.review_id,
        reviewer_user_id=reviewer_id,
        decision="approved",
        payload={"summary": "sufficient support"},
    )

    state = supervisor.apply_review(review.review_id)
    durable = checkpoints.get_run(org_id, workspace_id, run.run_id)
    assert durable is not None and durable.status == "running"
    assert state["status"] == "reviewed"
    assert state["review"] == {
        "decision": "approved",
        "payload": {"summary": "sufficient support"},
        "review_id": review.review_id,
    }
    assert state["findings"][0]["source_ids"] == ["src-1"]


def test_research_supervisor_finding_payload_must_be_strict_json(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    supervisor = ResearchSupervisor(
        checkpoints,
        ReviewRegistry(store, checkpoints=checkpoints),
        organization_id=org_id,
        workspace_id=workspace_id,
        actor_id=owner_id,
    )
    run = supervisor.start("authority question")
    with pytest.raises(ResearchRunError, match="strict JSON"):
        supervisor.record_finding(run.run_id, {"bad": object()})


@pytest.mark.parametrize(
    ("decision", "expected_run_status", "expected_research_status"),
    [
        ("revise", "running", "collecting"),
        ("rejected", "failed", "rejected"),
    ],
)
def test_research_review_decision_controls_lifecycle(
    tmp_path, decision, expected_run_status, expected_research_status
):
    from hybridagent.research_run import ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    supervisor = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id
    )
    run = supervisor.start("authority question")
    review = supervisor.request_review(run.run_id, required_role="reviewer")
    reviews.submit_decision(
        org_id,
        workspace_id,
        review.review_id,
        reviewer_user_id=reviewer_id,
        decision=decision,
        payload={"reason": decision},
    )

    state = supervisor.apply_review(review.review_id)
    durable = checkpoints.get_run(org_id, workspace_id, run.run_id)
    assert durable is not None and durable.status == expected_run_status
    assert state["status"] == expected_research_status


def test_research_supervisor_rejects_non_research_run(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    run = checkpoints.create_run(
        org_id,
        workspace_id,
        kind="plan",
        created_by=owner_id,
        state={"goal": "not research", "steps": []},
        schema_manifest={"name": "plan-executor", "version": 1},
    )
    supervisor = ResearchSupervisor(
        checkpoints,
        ReviewRegistry(store, checkpoints=checkpoints),
        organization_id=org_id,
        workspace_id=workspace_id,
        actor_id=owner_id,
    )

    with pytest.raises(ResearchRunError, match="research run"):
        supervisor.record_finding(run.run_id, {"claim": "wrong kind"})


def test_concurrent_research_findings_detect_stale_head(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    database = tmp_path / "praxis.db"
    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    run = ResearchSupervisor(
        checkpoints,
        ReviewRegistry(store, checkpoints=checkpoints),
        organization_id=org_id,
        workspace_id=workspace_id,
        actor_id=owner_id,
    ).start("concurrent findings")
    stores = [Store(database), Store(database)]
    supervisors = [
        ResearchSupervisor(
            CheckpointRegistry(item),
            ReviewRegistry(item, checkpoints=CheckpointRegistry(item)),
            organization_id=org_id,
            workspace_id=workspace_id,
            actor_id=owner_id,
        )
        for item in stores
    ]
    barrier = Barrier(2)
    originals = [supervisor._checkpoint for supervisor in supervisors]
    for index, supervisor in enumerate(supervisors):
        original = originals[index]

        def synchronized(*args, _original=original, **kwargs):
            barrier.wait()
            return _original(*args, **kwargs)

        supervisor._checkpoint = synchronized

    def write(index):
        try:
            supervisors[index].record_finding(run.run_id, {"claim": str(index)})
        except ResearchRunError:
            return "stale"
        return "written"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, range(2)))
    finally:
        for item in stores:
            item.close()
    assert sorted(outcomes) == ["stale", "written"]
    assert (
        len(
            ResearchSupervisor(
                checkpoints,
                ReviewRegistry(store, checkpoints=checkpoints),
                organization_id=org_id,
                workspace_id=workspace_id,
                actor_id=owner_id,
            ).load(run.run_id)["findings"]
        )
        == 1
    )


def test_apply_review_requires_matching_active_interrupt(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    supervisor = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id
    )
    run = supervisor.start("interrupt correlation")
    review = supervisor.request_review(run.run_id, required_role="reviewer")
    reviews.submit_decision(
        org_id,
        workspace_id,
        review.review_id,
        reviewer_user_id=reviewer_id,
        decision="approved",
        payload={},
    )
    store._directory_execute(
        "UPDATE professional_runs SET interrupt_type='operator_input',"
        "interrupt_payload_json='{}' WHERE run_id=?",
        (run.run_id,),
    )

    with pytest.raises(ResearchRunError, match="active review interrupt"):
        supervisor.apply_review(review.review_id)


def test_apply_review_checkpoint_and_lifecycle_are_atomic(tmp_path):
    from hybridagent.research_run import ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    supervisor = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id
    )
    run = supervisor.start("atomic apply")
    review = supervisor.request_review(run.run_id, required_role="reviewer")
    reviews.submit_decision(
        org_id,
        workspace_id,
        review.review_id,
        reviewer_user_id=reviewer_id,
        decision="approved",
        payload={},
    )
    before = checkpoints.get_run(org_id, workspace_id, run.run_id)
    store._directory_execute(
        "CREATE TRIGGER fail_research_apply BEFORE INSERT ON run_checkpoints "
        "WHEN NEW.state_json LIKE '%\"decision\"%' BEGIN "
        "SELECT RAISE(ABORT,'forced apply failure'); END"
    )

    with pytest.raises(Exception, match="forced apply failure"):
        supervisor.apply_review(review.review_id)
    after = checkpoints.get_run(org_id, workspace_id, run.run_id)
    assert before is not None and after is not None
    assert after.status == "interrupted"
    assert after.head_checkpoint_id == before.head_checkpoint_id


def test_research_supervisor_requires_active_actor_for_reads_and_writes(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    owner = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id,
        workspace_id=workspace_id, actor_id=owner_id,
    )
    run = owner.start("authorization")
    outsider = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id,
        workspace_id=workspace_id, actor_id="usr-does-not-exist",
    )
    with pytest.raises(ResearchRunError, match="active workspace"):
        outsider.load(run.run_id)

    store._directory_execute(
        "UPDATE organization_users SET status='disabled' WHERE user_id=?", (owner_id,)
    )
    with pytest.raises(ResearchRunError, match="active workspace"):
        owner.record_finding(run.run_id, {"claim": "disabled actor write"})
    latest = checkpoints.latest(org_id, workspace_id, run.run_id)
    assert latest is not None and latest.state["findings"] == []


def test_research_start_rejects_state_it_cannot_reload(tmp_path):
    from hybridagent.research_run import ResearchRunError, ResearchSupervisor
    from hybridagent.reviews import ReviewRegistry

    store, checkpoints, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    supervisor = ResearchSupervisor(
        checkpoints, ReviewRegistry(store, checkpoints=checkpoints),
        organization_id=org_id, workspace_id=workspace_id, actor_id=owner_id,
    )
    with pytest.raises(ResearchRunError, match="state shape"):
        supervisor.start("query", hypotheses=cast(list[str], [1]))
    row = store._directory_one(
        "SELECT COUNT(*) AS n FROM professional_runs WHERE kind='research'"
    )
    assert row is not None and row["n"] == 0
