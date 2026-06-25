import time

import pytest

from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.persistence import Store


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_durable_memory_persists_across_agents(tmp_path, monkeypatch):
    # Regression: `praxis remember` used to drop facts at process exit.
    _isolate(tmp_path, monkeypatch)
    a1 = PraxisAgent.persistent()
    a1.learn("Michael prefers concise briefs", kind="preference", provenance="cli")
    # A fresh agent (new "process") rehydrates from disk.
    a2 = PraxisAgent.persistent()
    assert "Michael prefers concise briefs" in [it.text for it in a2.memory.durable]
    assert a2.memory.stats()["durable"] >= 1


def test_audit_trail_persists_across_agents(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    a1 = PraxisAgent.persistent()
    a1.handle("Review recent mail and save a brief")
    assert len(a1.broker.audit) > 0
    a2 = PraxisAgent.persistent()
    assert len(a2.broker.audit) >= len(a1.broker.audit)


def test_held_approval_survives_and_executes_in_new_agent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    a1 = PraxisAgent.persistent()
    r = a1.handle("Prepare a customer follow-up email")
    aid = r.pending_approvals[0]["approval_id"]

    # Simulate `praxis approve <id>` from a separate process.
    a2 = PraxisAgent.persistent()
    assert aid in a2.broker.pending
    out = a2.approve(aid)
    assert "SENT" in out

    # Consumed: a third agent no longer sees it pending.
    a3 = PraxisAgent.persistent()
    assert aid not in a3.broker.pending


def test_expired_approval_is_not_approvable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"send_email"}), store=store)
    d = broker.authorize("praxis", "send_email", RiskClass.SEND, {})
    broker.pending[d.approval_id].expires_at = time.time() - 1  # force expiry
    assert broker.approve(d.approval_id) is None
    assert d.approval_id not in broker.pending


def test_memory_with_no_store_stays_in_memory(tmp_path, monkeypatch):
    # The default (storeless) path must remain pure in-memory for determinism.
    _isolate(tmp_path, monkeypatch)
    a = PraxisAgent()                     # no store
    a.learn("ephemeral", kind="note", provenance="t")
    assert PraxisAgent().memory.stats()["durable"] == 0
