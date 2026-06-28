"""D1 dashboard: durable, replayable run traces + Run Graph endpoints/assets."""
import json
import socket
import urllib.error
import urllib.request

from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store
from hybridagent.plan_execute import PlanExecutor, PlanStep
from hybridagent.tools import Tool, ToolRegistry


# ------------------------------------------------------------------ store
def test_run_event_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.start_run("r1", "do a thing", kind="plan")
    a = s.add_run_event("r1", "plan", {"goal": "g", "nodes": [{"id": "s1"}]})
    b = s.add_run_event("r1", "step_done", {"id": "s1", "tool": "get"}, node_id="s1")
    assert (a, b) == (1, 2)  # per-run sequence numbers
    s.finish_run("r1", "completed")

    runs = s.list_runs()
    assert runs[0]["run_id"] == "r1"
    assert runs[0]["status"] == "completed"
    assert runs[0]["event_count"] == 2
    assert s.get_run("r1")["goal"] == "do a thing"

    ev = s.list_run_events("r1")
    assert [e["kind"] for e in ev] == ["plan", "step_done"]
    assert ev[1]["data"]["tool"] == "get" and ev[1]["node_id"] == "s1"


def test_start_run_is_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    s.start_run("r1", "first")
    s.start_run("r1", "second")  # must not duplicate or clobber
    assert len(s.list_runs()) == 1
    assert s.get_run("r1")["goal"] == "first"


# ------------------------------------------------------------- on_event hook
def test_plan_executor_forwards_on_event(tmp_path):
    reg = ToolRegistry()
    reg.register(Tool("get_data", RiskClass.READ, "read", lambda **k: "data"))
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"get_data"}))
    got: list = []
    steps = [PlanStep(id="s1", intent="read", tool="get_data", args={})]
    PlanExecutor(reg, broker,
                 on_event=lambda k, d: got.append((k, d))).execute("g", steps=steps)
    kinds = [k for k, _ in got]
    assert kinds[0] == "plan" and "step_done" in kinds and "final" in kinds
    plan = next(d for k, d in got if k == "plan")
    assert plan["nodes"][0]["id"] == "s1" and plan["nodes"][0]["depends_on"] == []


def test_on_event_failure_never_breaks_execution(tmp_path):
    reg = ToolRegistry()
    reg.register(Tool("get_data", RiskClass.READ, "read", lambda **k: "data"))
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"get_data"}))

    def boom(_k, _d):
        raise RuntimeError("ui blew up")

    steps = [PlanStep(id="s1", intent="read", tool="get_data", args={})]
    report = PlanExecutor(reg, broker, on_event=boom).execute("g", steps=steps)
    assert report.status == "completed"  # callback errors are swallowed


# ------------------------------------------------------------- daemon trace
def test_daemon_agent_run_records_durable_trace(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    result = d.agent_run("follow up with the customer")
    run_id = result["run_id"]
    assert run_id

    runs = d.list_runs_trace()["runs"]
    assert any(r["run_id"] == run_id for r in runs)

    trace = d.get_run_trace(run_id)
    kinds = [e["kind"] for e in trace["events"]]
    assert "plan" in kinds and "final" in kinds
    assert trace["run"]["status"] == result["status"]
    plan = next(e for e in trace["events"] if e["kind"] == "plan")
    assert "nodes" in plan["data"]


# ------------------------------------------------------------- live HTTP
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_traces_and_static_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            f"{url}/api/agent/run",
            data=json.dumps({"goal": "follow up with the customer"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            run = json.loads(r.read())
        assert run["run_id"]

        with urllib.request.urlopen(f"{url}/api/traces", timeout=10) as r:
            traces = json.loads(r.read())
        assert any(x["run_id"] == run["run_id"] for x in traces["runs"])

        with urllib.request.urlopen(
                f"{url}/api/traces/{run['run_id']}", timeout=10) as r:
            one = json.loads(r.read())
        assert one["run"]["run_id"] == run["run_id"] and one["events"]

        # Modular-shell static asset is served with a JS content type.
        with urllib.request.urlopen(f"{url}/web/run-graph.js", timeout=10) as r:
            body = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
        assert "EventSource" in body and "javascript" in ctype

        # Path traversal out of the web bundle is refused.
        try:
            code = urllib.request.urlopen(
                f"{url}/web/../persistence.py", timeout=10).getcode()
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404

        # The dashboard shell references the module + panel.
        with urllib.request.urlopen(f"{url}/", timeout=10) as r:
            html = r.read().decode()
        assert "/web/run-graph.js" in html and "Run Graph" in html
    finally:
        d._stop_status_server()
