"""0.21.0 — vertical jobs + budget hard-stop across surfaces."""
from __future__ import annotations

import pytest

from hybridagent import config as cfg
from hybridagent.jobs import get_job, list_jobs, schedule_colleague
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


def test_jobs_catalog():
    jobs = list_jobs()
    ids = {j["id"] for j in jobs}
    assert ids == {"research", "draft", "schedule"}
    assert get_job("research").mode == "research"
    assert get_job("draft").mode == "chat"
    assert get_job("schedule").mode == "cron"


def test_budget_blocks_chat_and_research(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    d.budget_set(0.001)
    d.store.add_spend(0.01)
    assert d.budget_status()["over"] is True

    chat = d.chat([{"role": "user", "content": "hi"}])
    assert chat.get("blocked") is True
    assert "Budget" in chat["text"]

    res = d.research("latest news")
    assert res.get("blocked") is True

    ask = d.ask("what is praxis?")
    assert getattr(ask, "blocked", False) is True or "Budget" in ask.text

    events = list(d.chat_agent([{"role": "user", "content": "use tools"}]))
    assert events and events[0]["type"] == "error" and events[0].get("blocked")

    with pytest.raises(RuntimeError, match="Budget"):
        d.submit("queue something")


def test_budget_blocks_task_runner(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    # Create task before hitting cap so submit path is not the blocker.
    d.budget_set(100.0)
    tid = d.submit("do a small thing")
    d.budget_set(0.001)
    d.store.add_spend(1.0)
    task = d.manager.get(tid)
    d._run_task(task)
    row = d.manager.store.get_task(tid)
    assert row["status"] == "failed"
    assert "Budget" in (row.get("error") or "")


def test_schedule_colleague_creates_cron(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    store = Store.open(tmp_path / "praxis.db")
    job = schedule_colleague(store, goal="draft a morning note", schedule="daily")
    assert "error" not in job
    assert job.get("job_id")
    jobs = store.list_cron_jobs()
    assert any(j["job_id"] == job["job_id"] for j in jobs)


def test_sandbox_default_is_auto(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.sandbox import backend_status, select_backend
    st = backend_status()
    assert st["configured"] == "auto"
    assert select_backend() in ("local", "docker")


def test_readiness_includes_budget(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.persistence import Store
    from hybridagent.readiness import readiness
    store = Store.open(tmp_path / "p.db")
    rep = readiness(store)
    keys = {c["key"] for c in rep["checks"]}
    assert "budget" in keys
    assert "sandbox" in keys
