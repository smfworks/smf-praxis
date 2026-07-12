"""Cross-workspace isolation contracts for memory, knowledge, runs, and board."""

import multiprocessing
from pathlib import Path

from hybridagent.embeddings import EmbeddingClient
from hybridagent.organizations import OrganizationDirectory
from hybridagent.persistence import Store
from hybridagent.rag import Rag
from hybridagent.workspace_context import WorkspaceScope
from hybridagent.workspaces import WorkspaceDirectory


def _race_start_run(db_path: str, workspace_id: str, barrier, results) -> None:
    store = Store(Path(db_path))
    barrier.wait()
    try:
        store.start_run("shared-run", workspace_id=workspace_id)
    except ValueError:
        results.put((workspace_id, "rejected"))
    else:
        results.put((workspace_id, "created"))


def setup_scopes(tmp_path):
    store = Store(tmp_path / "praxis.db")
    organizations = OrganizationDirectory(store)
    organization, owner = organizations.bootstrap("Practice", "owner@example.com")
    directory = WorkspaceDirectory(store)
    first = directory.create(
        organization.organization_id, "A", "matter", "A",
        owner_user_id=owner.user_id)
    second = directory.create(
        organization.organization_id, "B", "matter", "B",
        owner_user_id=owner.user_id)
    return store, WorkspaceScope(store, organization.organization_id, first.workspace_id), WorkspaceScope(
        store, organization.organization_id, second.workspace_id)


def test_workspace_memory_never_crosses_scope(tmp_path):
    store, first, second = setup_scopes(tmp_path)
    first.add_memory("episodic", "Alpha confidential fact", "user", "note")
    second.add_memory("episodic", "Beta confidential fact", "user", "note")
    assert [row["text"] for row in first.load_memory("episodic")] == [
        "Alpha confidential fact"]
    assert [row["text"] for row in second.load_memory("episodic")] == [
        "Beta confidential fact"]
    assert store.load_memory("episodic") == []  # legacy view sees unowned rows only


def test_workspace_knowledge_uses_unforgeable_canonical_namespace(tmp_path):
    store, first, second = setup_scopes(tmp_path)
    embedder = EmbeddingClient(mode="mock")
    rag_a = Rag(store, embedder=embedder, ns=first.knowledge_namespace)
    rag_b = Rag(store, embedder=embedder, ns=second.knowledge_namespace)
    rag_a.ingest_text("Alpha-only engineering evidence", "alpha.txt")
    rag_b.ingest_text("Beta-only legal evidence", "beta.txt")
    assert [item.source for item in rag_a.retrieve("engineering evidence")] == ["alpha.txt"]
    assert [item.source for item in rag_b.retrieve("legal evidence")] == ["beta.txt"]
    assert first.knowledge_namespace != second.knowledge_namespace


def test_runs_and_trace_lookup_are_workspace_scoped(tmp_path):
    store, first, second = setup_scopes(tmp_path)
    first.start_run("run-a", "Analyze alpha")
    second.start_run("run-b", "Analyze beta")
    store.add_run_event("run-a", "step", {"secret": "alpha"})
    assert [row["run_id"] for row in first.list_runs()] == ["run-a"]
    assert second.get_run("run-a") is None
    assert second.run_events("run-a") == []


def test_board_cards_are_workspace_scoped_and_linked_runs_must_match(tmp_path):
    store, first, second = setup_scopes(tmp_path)
    first.start_run("run-a", "Run A")
    first.add_card("card-a", "Alpha card", "Do alpha", run_id="run-a")
    second.add_card("card-b", "Beta card", "Do beta")
    assert [row["card_id"] for row in first.list_cards()] == ["card-a"]
    assert [row["card_id"] for row in second.list_cards()] == ["card-b"]
    assert second.get_card("card-a") is None
    try:
        second.add_card("bad", "Bad", "Bad", run_id="run-a")
    except ValueError as exc:
        assert "run" in str(exc)
    else:
        raise AssertionError("cross-workspace run link unexpectedly accepted")


def test_context_key_is_stable_and_workspace_specific(tmp_path):
    _, first, second = setup_scopes(tmp_path)
    assert first.context_key("chat", "thread-1").startswith("workspace:")
    assert first.context_key("chat", "thread-1") != second.context_key("chat", "thread-1")


def test_legacy_board_surface_never_sees_or_mutates_owned_cards(tmp_path):
    store, first, second = setup_scopes(tmp_path)
    first.add_card("owned-a", "A", "A")
    second.add_card("owned-b", "B", "B")
    store.add_card("legacy", "Legacy", "Legacy")
    assert [row["card_id"] for row in store.list_unowned_cards()] == ["legacy"]
    assert not store.move_card(
        "owned-a", "done", organization_id="", workspace_id="")
    assert not store.delete_card(
        "owned-b", organization_id="", workspace_id="")
    owned = store.get_card("owned-a", workspace_id=first.workspace_id)
    assert owned is not None and owned["lane"] == "backlog"


def test_cross_workspace_run_id_collision_is_rejected(tmp_path):
    _, first, second = setup_scopes(tmp_path)
    first.start_run("collision", "First")
    try:
        second.start_run("collision", "Second")
    except ValueError as exc:
        assert "another workspace" in str(exc)
    else:
        raise AssertionError("cross-workspace run collision was silently accepted")


def test_cross_process_run_ownership_has_one_winner(tmp_path):
    db_path = tmp_path / "race.db"
    Store(db_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_race_start_run,
            args=(str(db_path), workspace_id, barrier, results))
        for workspace_id in ("ws-a", "ws-b")]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert sorted(status for _, status in outcomes) == ["created", "rejected"]
    stored = Store(db_path)._directory_one(
        "SELECT workspace_id FROM runs WHERE run_id='shared-run'")
    assert stored is not None
    winner = next(workspace for workspace, status in outcomes if status == "created")
    assert stored["workspace_id"] == winner
