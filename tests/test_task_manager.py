from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.persistence import Store
from hybridagent.task_manager import TaskManager


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_task_create_persists_across_managers(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    task = TaskManager(store).create("Review recent mail")
    assert task.task_id.startswith("task-")
    assert task.status == "pending"

    fresh = TaskManager(Store.open()).get(task.task_id)
    assert fresh.goal == "Review recent mail"
    assert fresh.status == "pending"


def test_task_run_completes_non_consequential_goal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    tm = TaskManager(agent.store)
    task = tm.create("Review recent mail and save a brief")
    done = tm.run_once(task.task_id, agent)
    assert done.status == "completed"
    assert done.attempts == 1
    row = agent.store.get_task(task.task_id)
    assert row["cycle_id"].startswith("cyc-")
    assert row["result"]["actions"]


def test_task_run_waits_for_approval_on_consequential_goal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    tm = TaskManager(agent.store)
    task = tm.create("Prepare a customer follow-up email")
    waiting = tm.run_once(task.task_id, agent)
    assert waiting.status == "waiting_approval"
    row = agent.store.get_task(task.task_id)
    assert row["result"]["pending_approvals"]


def test_task_cancel_is_persistent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    tm = TaskManager(store)
    task = tm.create("Something long running")
    assert tm.cancel(task.task_id) is True
    assert TaskManager(Store.open()).get(task.task_id).status == "cancelled"
    assert tm.cancel(task.task_id) is False


def test_task_records_compliance_events(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    tm = TaskManager(agent.store)
    task = tm.create("Review recent mail and save a brief")
    done = tm.run_once(task.task_id, agent)
    all_events = agent.store.list_compliance_events()
    cycle_events = agent.store.list_compliance_events(done.cycle_id)
    assert any(e["event_type"] == "task_started" for e in all_events)
    assert any(e["event_type"] == "task_finished" for e in cycle_events)
