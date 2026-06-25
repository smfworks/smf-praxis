from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.compliance import ComplianceReporter


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_cycle_events_and_decision_ids_are_persisted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    report = agent.handle("Review recent mail and save a brief")
    assert report.cycle_id.startswith("cyc-")

    events = agent.store.list_compliance_events(report.cycle_id)
    types = [e["event_type"] for e in events]
    assert "cycle_start" in types
    assert "signals" in types
    assert "plan" in types
    assert "decision" in types
    assert "cycle_end" in types

    audits = agent.store.load_audit()
    assert all(a["decision_id"].startswith("dec-") for a in audits)
    assert all(a["cycle_id"] == report.cycle_id for a in audits)
    assert all(a["policy_rule"] for a in audits)


def test_pending_approval_has_rationale_and_evidence_bundle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    report = agent.handle("Prepare a customer follow-up email")
    approval_id = report.pending_approvals[0]["approval_id"]

    row = agent.store.get_approval(approval_id)
    assert row["cycle_id"] == report.cycle_id
    assert row["decision_id"].startswith("dec-")
    assert row["rationale"]
    assert row["evidence"]
    assert row["evidence"][0]["content_hash"]


def test_approval_records_operator_and_compliance_report_passes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    report = agent.handle("Prepare a customer follow-up email")
    approval_id = report.pending_approvals[0]["approval_id"]

    out = PraxisAgent.persistent().approve(
        approval_id, approved_by="michael", approval_notes="reviewed evidence")
    assert "SENT" in out
    row = agent.store.get_approval(approval_id)
    assert row["status"] == "approved"
    assert row["approved_by"] == "michael"
    assert row["approval_notes"] == "reviewed evidence"
    assert row["resolved_at"] is not None

    report = ComplianceReporter(agent.store).build()
    assert report.passed
    assert report.approved_consequential == 1
    assert "PASS" in ComplianceReporter.render(report)


def test_compliance_report_treats_rejected_as_benign(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    report = agent.handle("Prepare a customer follow-up email")
    approval_id = report.pending_approvals[0]["approval_id"]
    assert agent.store.resolve_approval(approval_id, "rejected")

    compliance = ComplianceReporter(agent.store).build()
    # Rejection is the compliant outcome — the action did not run.
    assert compliance.passed
    assert compliance.rejected_consequential == 1


def test_compliance_report_flags_failed_task(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    # Synthesize a failed task to trigger the new error-aware finding.
    agent.store.add_task("task-fail1", "broken goal")
    agent.store.update_task("task-fail1", status="failed", error="boom")
    compliance = ComplianceReporter(agent.store).build()
    assert not compliance.passed
    assert any("task" in f.message and f.severity == "high"
               for f in compliance.findings)
    assert compliance.failed_tasks == 1

