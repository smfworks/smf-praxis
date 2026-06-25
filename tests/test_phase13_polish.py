"""Tests for Phase 13 integration polish: contradiction detection, scratchpad,
ask() auto-refresh, health snapshot, evaluator warning."""
import os
import time

import pytest

from hybridagent import PraxisAgent, config as cfg
from hybridagent.contradiction import detect
from hybridagent.metrics import HealthMonitor
from hybridagent.persistence import Store
from hybridagent.rag import RetrievedChunk
from hybridagent.scratchpad import Scratchpad
from hybridagent.skill_evaluator import SkillEvaluator
from hybridagent.skills import SkillLibrary
from hybridagent.wiki import KBSourceManager


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# ---------------------------------------------------- contradiction detection
def test_contradiction_detects_polarity_flip():
    a = RetrievedChunk(text="The audit policy is mandatory for all clinical records",
                       source="policy_v1.md", score=1.0)
    b = RetrievedChunk(text="The audit policy is not mandatory for clinical records",
                       source="policy_v2.md", score=1.0)
    c = RetrievedChunk(text="Sourdough bread benefits from a long fermentation",
                       source="bread.md", score=1.0)
    found = detect([a, b, c])
    assert any({f.a_source, f.b_source} == {"policy_v1.md", "policy_v2.md"}
               for f in found)


def test_contradiction_detects_numeric_disagreement():
    a = RetrievedChunk(text="Q3 revenue grew 12 percent year over year",
                       source="finance_a.md", score=1.0)
    b = RetrievedChunk(text="Q3 revenue grew 7 percent year over year",
                       source="finance_b.md", score=1.0)
    found = detect([a, b])
    assert found and "numeric" in found[0].explanation


def test_no_contradiction_with_unrelated_chunks():
    a = RetrievedChunk(text="The sky is blue today", source="sky.md", score=1.0)
    b = RetrievedChunk(text="Coffee beans grow on bushes", source="coffee.md",
                       score=1.0)
    assert detect([a, b]) == []


# ------------------------------------------------------- ask() auto-refresh
def test_ask_refreshes_due_wiki_sources(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    doc = tmp_path / "policy.md"
    doc.write_text("The Q3 revenue guidance is 8 percent", encoding="utf-8")
    KBSourceManager(agent.store).add(str(doc), refresh_interval_seconds=0)
    ans = agent.ask("What is the Q3 revenue guidance?")
    # Wiki was due, refresh ingests, ask retrieves the new content.
    assert not ans.abstained


# ------------------------------------------ ask() surfaces contradictions
def test_ask_surfaces_contradictions_when_sources_disagree(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    agent.rag.ingest_text("Q3 revenue grew 12 percent year over year",
                          source="old.md")
    agent.rag.ingest_text("Q3 revenue grew 7 percent year over year",
                          source="new.md")
    ans = agent.ask("Q3 revenue growth")
    assert ans.contradictions
    sources = {(c.a_source, c.b_source) for c in ans.contradictions}
    assert any({"old.md", "new.md"} == set(pair) for pair in sources)


# ------------------------------------------------------- scratchpad sharing
def test_scratchpad_round_trip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sp = Scratchpad(Store.open())
    sp.write("AdventHealth.intel", "ARR target raised to $50M",
             written_by="agent-researcher", ns="account-AdventHealth")
    entries = sp.read("AdventHealth.intel", ns="account-AdventHealth")
    assert entries and entries[0].written_by == "agent-researcher"
    assert "ARR" in entries[0].value


def test_scratchpad_ttl_expires(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    sp = Scratchpad(Store.open())
    sp.write("ephemeral", "vanishes soon", written_by="agent-x",
             ttl_seconds=-1)  # already expired
    assert sp.read("ephemeral") == []


# ------------------------------------------------------------- health metrics
def test_health_snapshot_is_healthy_on_fresh_store(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    snap = HealthMonitor(Store.open()).snapshot()
    assert snap.healthy
    assert snap.failed_tasks == 0


def test_health_snapshot_degrades_on_failed_task(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    store.add_task("task-X", "broken")
    store.update_task("task-X", status="failed", error="boom")
    snap = HealthMonitor(store).snapshot()
    assert not snap.healthy
    assert snap.failed_tasks == 1


# -------------------------------------------- skill evaluator warning (#17)
def test_skill_evaluator_warns_without_store(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = SkillLibrary()  # no store
    ev = SkillEvaluator(lib)
    # Without a store, record() must NOT silently no-op: it must return None and
    # the impact report must explicitly say there's no data, so a deployment
    # that built an agent without a store can tell its skill metrics are dead.
    assert ev.record("nameless", "goal", "success") is None
    assert "no outcome data" in ev.impact_report("nameless")
