from hybridagent import config as cfg
from hybridagent.orchestrator import AgentPool, Orchestrator, PredictiveRouter
from hybridagent.persistence import Store


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_predictive_router_roles():
    r = PredictiveRouter()
    assert r.route("audit this policy for HIPAA risk") == "compliance"
    assert r.route("draft a follow-up email") == "drafter"
    assert r.route("predict likely next steps") == "predictor"
    assert r.route("research this project") == "researcher"


def test_agent_pool_registers_scoped_agent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pool = AgentPool(Store.open())
    spec = pool.ensure("drafter")
    assert spec.agent_id == "agent-drafter"
    assert "create_email_draft" in spec.tools
    assert "delete_file" not in spec.tools
    assert pool.list()[0].role == "drafter"


def test_orchestrator_runs_subagent_and_records_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    run = Orchestrator(store).run("research recent mail and save a brief",
                                  role="researcher")
    assert run.run_id.startswith("run-")
    assert run.status == "completed"
    rows = store.list_subagent_runs()
    assert rows[0]["run_id"] == run.run_id
    assert rows[0]["cycle_id"].startswith("cyc-")


def test_drafter_subagent_holds_send_for_approval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    run = Orchestrator(store).run("draft a customer follow-up email", role="drafter")
    assert run.status == "waiting_approval"
    approvals = store.list_approvals()
    assert approvals and approvals[0]["tool"] == "send_email"


def test_subagent_events_are_compliance_logged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    run = Orchestrator(store).run("research mail", role="researcher")
    all_events = store.list_compliance_events()
    cycle_events = store.list_compliance_events(run.cycle_id)
    assert any(e["event_type"] == "subagent_started" for e in all_events)
    assert any(e["event_type"] == "subagent_finished" for e in cycle_events)
