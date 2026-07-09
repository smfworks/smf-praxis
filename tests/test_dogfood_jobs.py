"""Dogfood harness: research / draft / schedule jobs end-to-end (mock LLM)."""

from __future__ import annotations

import json
import urllib.request

from hybridagent import config as cfg
from hybridagent.jobs import get_job, list_jobs, run_research, schedule_colleague
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


def test_jobs_catalog_shape():
    jobs = list_jobs()
    assert {j["id"] for j in jobs} == {"research", "draft", "schedule"}
    for jid in ("research", "draft", "schedule"):
        j = get_job(jid)
        assert j and j.example_prompt and j.risk_note


def test_dogfood_research_mocked(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SEARCH_DISABLE_DEFAULT", "1")
    from hybridagent.daemon import Daemon

    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    res = run_research(d, "agent runtimes")
    assert "text" in res
    # Disabled search still returns a structured payload (no crash).
    assert res.get("blocked") is not True or "Budget" in (res.get("error") or "")


def test_dogfood_research_with_fake_search(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent import search as search_mod
    from hybridagent.daemon import Daemon

    class Hit:
        def __init__(self, title, url, snippet):
            self.title, self.url, self.snippet = title, url, snippet

    def fake_search(q, max_results=5):
        return [Hit("Example", "https://example.com", "An example site about agents.")]

    monkeypatch.setattr(search_mod, "web_search", fake_search)
    # Avoid real network fetch
    monkeypatch.setattr(
        "hybridagent.real_tools.fetch_url",
        lambda url: "Title: Example\nExample body about agent runtimes.",
    )

    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    res = d.research("open source agents", max_results=3)
    assert res.get("blocked") is not True
    assert res.get("results")
    assert res.get("text") is not None


def test_dogfood_draft_governed_chat(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon

    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    job = get_job("draft")
    assert job is not None
    prompt = job.example_prompt
    events = list(d.chat_agent([{"role": "user", "content": prompt}]))
    assert events
    # Mock may final-answer without tools; must not crash.
    assert any(e.get("type") in ("final", "tool_call", "approval", "error", "recall") for e in events)


def test_dogfood_schedule_cron_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon, _find_port

    store = Store.open(tmp_path / "praxis.db")
    job = schedule_colleague(
        store,
        goal="scan for follow-ups and draft a short note",
        schedule="daily@09:00",
        name="morning",
    )
    assert job and "error" not in job
    jid = job["job_id"]
    assert any(j["job_id"] == jid for j in store.list_cron_jobs())

    port = _find_port("127.0.0.1", 30000, 30100)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._ensure_agent()
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{url}/api/cron", timeout=10) as r:
            data = json.loads(r.read())
        assert "jobs" in data

        # create via HTTP
        body = json.dumps({
            "goal": "draft weekly summary",
            "schedule": "weekly",
            "name": "weekly",
            "mode": "do",
        }).encode()
        req = urllib.request.Request(
            f"{url}/api/cron", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            created = json.loads(r.read())
        assert created.get("job_id")

        # pause
        body = json.dumps({"job_id": created["job_id"], "enabled": False}).encode()
        req = urllib.request.Request(
            f"{url}/api/cron/toggle", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            toggled = json.loads(r.read())
        assert toggled.get("updated") is True
        assert toggled.get("enabled") is False

        # resume
        body = json.dumps({"job_id": created["job_id"], "enabled": True}).encode()
        req = urllib.request.Request(
            f"{url}/api/cron/toggle", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            toggled = json.loads(r.read())
        assert toggled.get("enabled") is True

        # dashboard embeds schedule surface
        with urllib.request.urlopen(f"{url}/", timeout=10) as r:
            html = r.read().decode()
        assert "/web/cron.js" in html and 'id="cron-list"' in html
        with urllib.request.urlopen(f"{url}/web/cron.js", timeout=10) as r:
            assert "PraxisCron" in r.read().decode()
    finally:
        d._stop_status_server()


def test_dogfood_budget_blocks_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon

    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    d.budget_set(0.001)
    assert d.store is not None
    d.store.add_spend(1.0)
    res = d.research("anything")
    assert res.get("blocked") is True
    events = list(d.chat_agent([{"role": "user", "content": "draft email"}]))
    assert events[0].get("type") == "error" and events[0].get("blocked")
