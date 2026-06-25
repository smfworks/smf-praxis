"""Tests for Phase 11 security / liveness fixes."""
import os
import time

import pytest

from hybridagent import PraxisAgent, config as cfg
from hybridagent.compliance import ComplianceReporter
from hybridagent.orchestrator import Orchestrator, PredictiveRouter
from hybridagent.persistence import Store
from hybridagent.skills import Skill, SkillLibrary
from hybridagent.task_manager import TaskManager
from hybridagent.wiki import KBSourceManager
from hybridagent.wiki_safe import UnsafeSourceError, validate_uri


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# ----------------------------------------------------------- SSRF fixes (#1)
def test_validate_uri_rejects_file_scheme():
    with pytest.raises(UnsafeSourceError):
        validate_uri("file:///etc/passwd")


def test_validate_uri_rejects_loopback_by_default(monkeypatch):
    monkeypatch.delenv("PRAXIS_KB_ALLOW_PRIVATE", raising=False)
    with pytest.raises(UnsafeSourceError):
        validate_uri("http://127.0.0.1/internal")


def test_validate_uri_rejects_link_local_metadata(monkeypatch):
    monkeypatch.delenv("PRAXIS_KB_ALLOW_PRIVATE", raising=False)
    with pytest.raises(UnsafeSourceError):
        validate_uri("http://169.254.169.254/latest/meta-data/")


def test_validate_uri_allows_loopback_with_opt_in(monkeypatch):
    monkeypatch.setenv("PRAXIS_KB_ALLOW_PRIVATE", "1")
    assert validate_uri("http://127.0.0.1/wiki") == "http://127.0.0.1/wiki"


def test_wiki_add_refuses_file_uri(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    with pytest.raises(UnsafeSourceError):
        KBSourceManager(Store.open()).add("file:///etc/passwd")


# --------------------------------------------------- skill outcomes auto (#2)
def test_handle_records_skill_outcome_automatically(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    agent.skills.add(Skill(name="followup-skill",
                           trigger="prepare a customer follow-up email"))
    agent.handle("Prepare a customer follow-up email")
    meta = agent.skills.metadata("followup-skill")
    assert meta is not None
    assert meta["usage_count"] >= 1


# ------------------------------------------------- KB infinite-due fix (#4)
def test_unchanged_refresh_advances_last_ingested(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    doc = tmp_path / "wiki.md"
    doc.write_text("stable content", encoding="utf-8")
    mgr = KBSourceManager(Store.open())
    src = mgr.add(str(doc), refresh_interval_seconds=3600)
    mgr.refresh(src.source_id)                                   # first ingest
    again = mgr.refresh(src.source_id)
    assert again.status == "unchanged"
    assert again.last_ingested_ts is not None
    assert src.source_id not in [s.source_id for s in mgr.due()]  # no longer due


# -------------------------------------------------- task idempotency (#5)
def test_run_once_refuses_to_re_execute_waiting_approval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    tm = TaskManager(agent.store)
    task = tm.create("Prepare a customer follow-up email")
    first = tm.run_once(task.task_id, agent)
    assert first.status == "waiting_approval"

    # Second call must NOT enqueue another approval (idempotency guard).
    pending_before = len(agent.store.list_approvals())
    second = tm.run_once(task.task_id, agent)
    pending_after = len(agent.store.list_approvals())
    assert second.status == "waiting_approval"
    assert pending_after == pending_before


# --------------------------------------- predictive-router injection guard (#7)
def test_router_refuses_keyword_escalation_when_injection_flagged():
    r = PredictiveRouter()
    # The goal text says "draft", which would normally route to drafter.
    assert r.route("draft a sensitive email") == "drafter"
    assert r.route("draft a sensitive email",
                   injection_flagged=True) == "researcher"


def test_orchestrator_route_uses_injection_flag(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    orch = Orchestrator(Store.open())
    run = orch.run("draft a customer follow-up email", injection_flagged=True)
    assert run.role == "researcher"


# ---------------------------------------------- task orphan recovery (#8)
def test_orphaned_task_is_recovered_on_taskmanager_construction(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    tm = TaskManager(store)
    task = tm.create("some goal")
    # Simulate a crash mid-execution: status='running', updated long ago.
    store.update_task(task.task_id, status="running", attempts=1,
                      updated_ts=time.time() - 1000)
    # Fresh TaskManager must sweep the orphan back to 'retry'.
    recovered = TaskManager(store).get(task.task_id)
    assert recovered.status == "retry"


# --------------------------------------- compliance now includes errors (#6)
def test_compliance_passes_after_normal_cycle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    agent.handle("Review recent mail and save a brief")
    report = ComplianceReporter(agent.store).build()
    assert report.passed
