"""Typed professional review contracts for Phase 4 workflows."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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
    return store, orgs, org.organization_id, workspace.workspace_id, owner.user_id, reviewer.user_id


def test_professional_review_is_typed_role_bound_and_interrupts_run(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, orgs, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    checkpoints = CheckpointRegistry(store)
    run = checkpoints.create_run(
        org_id,
        workspace_id,
        kind="research",
        created_by=owner_id,
        state={"stage": "draft"},
        schema_manifest={"name": "research", "version": 1},
    )

    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    review = reviews.request_review(
        org_id,
        workspace_id,
        created_by=owner_id,
        review_type="professional_release",
        required_role="reviewer",
        subject={"artifact_id": "draft-1"},
        run_id=run.run_id,
    )
    durable = checkpoints.get_run(org_id, workspace_id, run.run_id)
    assert durable is not None and durable.status == "interrupted"
    assert durable.interrupt_type == "professional_review"
    assert durable.interrupt_payload == {
        "review_id": review.review_id,
        "review_type": "professional_release",
    }

    non_reviewer = orgs.create_user("member@example.com")
    orgs.add_membership(org_id, non_reviewer.user_id, roles=("member",))
    with pytest.raises(ReviewError, match="role"):
        reviews.submit_decision(
            org_id,
            workspace_id,
            review.review_id,
            reviewer_user_id=non_reviewer.user_id,
            decision="approved",
            payload={"summary": "ship it"},
        )

    decided = reviews.submit_decision(
        org_id,
        workspace_id,
        review.review_id,
        reviewer_user_id=reviewer_id,
        decision="revise",
        payload={"summary": "needs one citation", "severity": "medium"},
    )
    assert decided.status == "decided"
    assert decided.decision == "revise"
    assert decided.decision_payload == {
        "severity": "medium",
        "summary": "needs one citation",
    }
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._directory_execute(
            "UPDATE professional_reviews SET subject_json='{}' WHERE review_id=?",
            (review.review_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._directory_execute(
            "DELETE FROM professional_reviews WHERE review_id=?", (review.review_id,)
        )


def test_professional_release_cannot_be_downgraded_to_member_role(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    with pytest.raises(ReviewError, match="cannot authorize"):
        ReviewRegistry(store).request_review(
            org_id,
            workspace_id,
            created_by=owner_id,
            review_type="professional_release",
            required_role="member",
            subject={"artifact_id": "draft-1"},
        )


def test_professional_review_lookup_is_workspace_concealed(tmp_path):
    from hybridagent.reviews import ReviewRegistry

    store, _, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    other = WorkspaceDirectory(store).create(
        org_id, "MAT-2", "matter", "Other", owner_user_id=owner_id
    )
    reviews = ReviewRegistry(store)
    review = reviews.request_review(
        org_id,
        workspace_id,
        created_by=owner_id,
        review_type="quality",
        required_role="reviewer",
        subject={"artifact_id": "draft-1"},
    )
    assert reviews.get_review(org_id, other.workspace_id, review.review_id) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": object()},
        {"nested": {"tuple": (1, 2)}},
    ],
)
def test_professional_review_payloads_must_be_strict_json(tmp_path, payload):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store)
    with pytest.raises(ReviewError, match="strict JSON"):
        reviews.request_review(
            org_id,
            workspace_id,
            created_by=owner_id,
            review_type="quality",
            required_role="reviewer",
            subject=payload,
        )

    review = reviews.request_review(
        org_id,
        workspace_id,
        created_by=owner_id,
        review_type="quality",
        required_role="reviewer",
        subject={"artifact_id": "draft-1"},
    )
    with pytest.raises(ReviewError, match="strict JSON"):
        reviews.submit_decision(
            org_id,
            workspace_id,
            review.review_id,
            reviewer_user_id=reviewer_id,
            decision="approved",
            payload=payload,
        )


def test_professional_review_enforces_maker_checker_separation(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, _, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store)
    review = reviews.request_review(
        org_id,
        workspace_id,
        created_by=reviewer_id,
        review_type="quality",
        required_role="reviewer",
        subject={"artifact_id": "self-authored"},
    )

    with pytest.raises(ReviewError, match="distinct"):
        reviews.submit_decision(
            org_id,
            workspace_id,
            review.review_id,
            reviewer_user_id=reviewer_id,
            decision="approved",
            payload={},
        )


def test_professional_review_decision_is_atomic_across_connections(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    database = tmp_path / "praxis.db"
    store, _, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    review = ReviewRegistry(store).request_review(
        org_id,
        workspace_id,
        created_by=owner_id,
        review_type="quality",
        required_role="reviewer",
        subject={"artifact_id": "draft-1"},
    )
    stores = [Store(database) for _ in range(8)]
    registries = [ReviewRegistry(item) for item in stores]
    barrier = Barrier(len(registries))

    def decide(index: int) -> str:
        barrier.wait()
        try:
            registries[index].submit_decision(
                org_id,
                workspace_id,
                review.review_id,
                reviewer_user_id=reviewer_id,
                decision="approved" if index % 2 == 0 else "rejected",
                payload={"index": index},
            )
        except ReviewError:
            return "lost"
        return "won"

    try:
        with ThreadPoolExecutor(max_workers=len(registries)) as pool:
            outcomes = list(pool.map(decide, range(len(registries))))
    finally:
        for item in stores:
            item.close()
    assert outcomes.count("won") == 1
    assert outcomes.count("lost") == 7


def test_inactive_user_cannot_submit_review_decision(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store)
    review = reviews.request_review(
        org_id,
        workspace_id,
        created_by=owner_id,
        review_type="quality",
        required_role="reviewer",
        subject={"artifact_id": "draft-1"},
    )
    store._directory_execute(
        "UPDATE organization_users SET status='disabled' WHERE user_id=?", (reviewer_id,)
    )

    with pytest.raises(ReviewError, match="workspace"):
        reviews.submit_decision(
            org_id,
            workspace_id,
            review.review_id,
            reviewer_user_id=reviewer_id,
            decision="approved",
            payload={},
        )
    pending = reviews.get_review(org_id, workspace_id, review.review_id)
    assert pending is not None and pending.status == "pending"


def test_run_backed_review_request_is_atomic_with_interrupt(tmp_path):
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    checkpoints = CheckpointRegistry(store)
    run = checkpoints.create_run(
        org_id,
        workspace_id,
        kind="research",
        created_by=owner_id,
        state={"stage": "draft"},
        schema_manifest={"name": "research", "version": 1},
    )
    checkpoints.cancel(org_id, workspace_id, run.run_id, actor_id=owner_id, reason="cancelled")

    with pytest.raises(ReviewError, match="not available"):
        ReviewRegistry(store, checkpoints=checkpoints).request_review(
            org_id,
            workspace_id,
            created_by=owner_id,
            review_type="quality",
            required_role="reviewer",
            subject={"artifact_id": "draft-1"},
            run_id=run.run_id,
        )
    row = store._directory_one(
        "SELECT COUNT(*) AS n FROM professional_reviews WHERE run_id=?", (run.run_id,)
    )
    assert row is not None and row["n"] == 0


def test_research_review_requires_compatible_bound_checkpoint(tmp_path):
    from hybridagent.research_run import RESEARCH_SCHEMA_MANIFEST, ResearchSupervisor
    from hybridagent.reviews import ReviewError, ReviewRegistry

    store, _, org_id, workspace_id, owner_id, _ = _scope(tmp_path)
    checkpoints = CheckpointRegistry(store)
    reviews = ReviewRegistry(store, checkpoints=checkpoints)
    plan = checkpoints.create_run(
        org_id, workspace_id, kind="plan", created_by=owner_id,
        state={"goal": "not research", "steps": []},
        schema_manifest={"name": "plan-executor", "version": 1},
    )
    plan_review_id = reviews.new_review_id()
    with pytest.raises(ReviewError, match="compatible research"):
        reviews.request_review(
            org_id, workspace_id, created_by=owner_id,
            review_type="research_findings", required_role="reviewer",
            subject={"run_id": plan.run_id}, run_id=plan.run_id,
            review_id=plan_review_id,
            checkpoint_state={
                "query": "not research", "hypotheses": [], "findings": [],
                "status": "pending_review", "review": {"review_id": plan_review_id},
            },
            expected_head_checkpoint_id=plan.head_checkpoint_id,
        )
    plan_after = checkpoints.get_run(org_id, workspace_id, plan.run_id)
    assert plan_after is not None and plan_after.status == "running"

    research = ResearchSupervisor(
        checkpoints, reviews, organization_id=org_id,
        workspace_id=workspace_id, actor_id=owner_id,
    ).start("bound review")
    assert research.schema_manifest == RESEARCH_SCHEMA_MANIFEST
    with pytest.raises(ReviewError, match="checkpoint"):
        reviews.request_review(
            org_id, workspace_id, created_by=owner_id,
            review_type="research_findings", required_role="reviewer",
            subject={"run_id": research.run_id}, run_id=research.run_id,
        )
    research_after = checkpoints.get_run(org_id, workspace_id, research.run_id)
    assert research_after is not None and research_after.status == "running"


def test_database_rejects_unauthorized_or_malformed_raw_decision(tmp_path):
    from hybridagent.reviews import ReviewRegistry

    store, orgs, org_id, workspace_id, owner_id, reviewer_id = _scope(tmp_path)
    reviews = ReviewRegistry(store)
    review = reviews.request_review(
        org_id, workspace_id, created_by=owner_id,
        review_type="quality", required_role="reviewer",
        subject={"artifact_id": "raw-update"},
    )
    member = orgs.create_user("disabled-member@example.com")
    orgs.add_membership(org_id, member.user_id, roles=("member",))
    store._directory_execute(
        "UPDATE organization_users SET status='disabled' WHERE user_id=?", (member.user_id,)
    )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        store._directory_execute(
            "UPDATE professional_reviews SET status='decided',decision='approved',"
            "decision_payload_json='{}',reviewer_user_id=?,reviewed_ts=1.0 "
            "WHERE review_id=?",
            (member.user_id, review.review_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="invalid"):
        store._directory_execute(
            "UPDATE professional_reviews SET status='decided',decision='approved',"
            "decision_payload_json='[]',reviewer_user_id=?,reviewed_ts=1.0 "
            "WHERE review_id=?",
            (reviewer_id, review.review_id),
        )
    pending = reviews.get_review(org_id, workspace_id, review.review_id)
    assert pending is not None and pending.status == "pending"


def test_review_trigger_upgrade_preserves_existing_trigger(tmp_path):
    db_path = tmp_path / "upgrade.db"
    store = Store(db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_professional_reviews_decision_update_v2")
    conn.execute(
        "CREATE TRIGGER trg_professional_reviews_decision_update "
        "BEFORE UPDATE ON professional_reviews "
        "WHEN OLD.status <> 'pending' OR NEW.status <> 'decided' "
        "BEGIN SELECT RAISE(ABORT, 'review decision is immutable'); END"
    )
    conn.commit()
    conn.close()

    upgraded = Store(db_path)
    rows = upgraded._directory_all(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?) ORDER BY name",
        (
            "trg_professional_reviews_decision_update",
            "trg_professional_reviews_decision_update_v2",
        ),
    )
    assert [row["name"] for row in rows] == [
        "trg_professional_reviews_decision_update",
        "trg_professional_reviews_decision_update_v2",
    ]
    upgraded.close()
