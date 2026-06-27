"""Tests for concurrent subagent orchestration (Orchestrator.run_many)."""

import threading
import time

from hybridagent.orchestrator import Orchestrator
from hybridagent.persistence import Store


def test_run_many_executes_all_and_persists(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        orch = Orchestrator(store)
        runs = orch.run_many([f"summarize item {i}" for i in range(6)], max_workers=4)
        assert len(runs) == 6
        assert all(r.status in ("completed", "waiting_approval") for r in runs)
        persisted = {r["run_id"] for r in orch.list_runs(limit=50)}
        assert {r.run_id for r in runs} <= persisted
    finally:
        store.close()


def test_run_many_preserves_order(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        orch = Orchestrator(store)
        goals = [("draft a reply", "drafter"), ("research trends", "researcher"),
                 ("audit the policy", "compliance")]
        runs = orch.run_many(goals, max_workers=3)
        assert [r.role for r in runs] == ["drafter", "researcher", "compliance"]
    finally:
        store.close()


def test_run_many_is_actually_concurrent(tmp_path, monkeypatch):
    store = Store.open(tmp_path / "praxis.db")
    try:
        orch = Orchestrator(store)
        seen_threads: set[int] = set()
        real_run = Orchestrator.run

        def slow_run(self, goal, role=None, **kw):
            seen_threads.add(threading.get_ident())
            time.sleep(0.2)
            return real_run(self, goal, role, **kw)

        monkeypatch.setattr(Orchestrator, "run", slow_run)
        start = time.time()
        runs = orch.run_many([f"g{i}" for i in range(4)], max_workers=4)
        elapsed = time.time() - start
        assert len(runs) == 4
        # 4 x 0.2s run serially would exceed 0.8s; concurrent stays well under.
        assert elapsed < 0.6
        assert len(seen_threads) >= 2
    finally:
        store.close()


def test_run_many_empty(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        assert Orchestrator(store).run_many([]) == []
    finally:
        store.close()
