"""Tests for the persistent daemon runtime."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from hybridagent.agent import PraxisAgent
from hybridagent.broker import RiskClass
from hybridagent.daemon import Daemon, DaemonState, _find_port, _read_state, _write_state
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store
from hybridagent.planner import Plan, Planner, Step
from hybridagent.tools import Tool, ToolRegistry


@pytest.fixture
def tmp_store(tmp_path):
    return Store.open(tmp_path / "praxis.db")


@pytest.fixture
def echo_tool():
    def run(message: str, **kwargs) -> str:
        return f"echo:{message}"
    return Tool(
        name="echo", description="echo", risk=RiskClass.DRAFT,
        run=run, parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )


@pytest.fixture
def send_tool(tmp_store):
    def run_send(message: str, **kwargs) -> str:
        return f"sent:{message}"
    return Tool(
        name="send", description="send", risk=RiskClass.SEND,
        run=run_send, parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    )


class _EchoSendPlanner(Planner):
    """Test-only planner that deterministically routes goals to the echo or
    send tool registered in the test fixture."""

    def __init__(self, registry):
        super().__init__(registry)

    def plan(self, goal):
        if "send" in goal.lower() or "hello" in goal.lower():
            return Plan(goal=goal, steps=[Step("send it", "send", {"message": goal})])
        return Plan(goal=goal, steps=[Step("echo it", "echo", {"message": goal})])

    def read_tools_for(self, goal):
        return ["echo"]


@pytest.fixture
def mock_agent(tmp_store, echo_tool, send_tool):
    registry = ToolRegistry()
    registry.register(echo_tool)
    registry.register(send_tool)
    agent = PraxisAgent(
        registry=registry,
        llm=LLMClient(mode="mock"),
        store=tmp_store,
        planner=_EchoSendPlanner(registry),
    )
    agent.broker.policy.allowed_tools = set(registry.names())
    agent.broker.policy.autonomous_risks = {RiskClass.READ, RiskClass.DRAFT}
    return agent


def test_daemon_state_roundtrip(tmp_path):
    state = DaemonState(running=True, started_ts=123.0, cycles=5)
    _write_state(state)
    restored = _read_state()
    assert restored.running is True
    assert restored.started_ts == 123.0
    assert restored.cycles == 5


def test_find_port():
    port = _find_port("127.0.0.1", start=30000, end=30100)
    assert 30000 <= port <= 30100


def test_daemon_submit_and_tick(tmp_store, mock_agent):
    class EchoPlanner(Planner):
        def plan(self, goal):
            return Plan(goal=goal, steps=[Step("echo it", "echo", {"message": goal})])
        def read_tools_for(self, goal):
            return ["echo"]
    mock_agent.planner = EchoPlanner(mock_agent.registry)
    daemon = Daemon(store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1)
    assert daemon.manager is not None
    task_id = daemon.submit("test echo", max_attempts=1)
    assert daemon.manager.get(task_id).status == "pending"
    daemon.tick()
    task = daemon.manager.get(task_id)
    assert task is not None
    assert task.status == "completed"
    actions = " ".join(task.result.get("actions", []))
    assert "echo:test echo" in actions


def test_daemon_pauses_at_approval(tmp_store, mock_agent):
    original = mock_agent.planner

    class MockPlanner(Planner):
        def __init__(self, registry):
            super().__init__(registry)

        def plan(self, goal):
            return Plan(goal=goal, steps=[Step("send it", "send", {"message": goal})])

        def read_tools_for(self, goal):
            return ["echo"]

    mock_agent.planner = MockPlanner(mock_agent.registry)
    try:
        daemon = Daemon(store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1)
        assert daemon.manager is not None
        task_id = daemon.submit("hello", max_attempts=1)
        daemon.tick()
        task = daemon.manager.get(task_id)
        assert task is not None
        assert task.status in ("waiting_approval", "failed")
        if task.status == "waiting_approval":
            assert daemon.state.tasks_waiting_approval == 1
            assert len(mock_agent.broker.pending) == 1
    finally:
        mock_agent.planner = original


def test_daemon_resume_after_approval(tmp_store, mock_agent):
    class SendPlanner(Planner):
        def __init__(self, registry):
            super().__init__(registry)

        def plan(self, goal):
            return Plan(goal=goal, steps=[Step("send it", "send", {"message": goal})])

        def read_tools_for(self, goal):
            return ["echo"]

    mock_agent.planner = SendPlanner(mock_agent.registry)
    daemon = Daemon(store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1)
    assert daemon.manager is not None
    task_id = daemon.submit("hello", max_attempts=1)
    daemon.tick()
    task = daemon.manager.get(task_id)
    assert task is not None
    assert task.status in ("waiting_approval", "failed")
    if task.status == "waiting_approval":
        approval_id = list(mock_agent.broker.pending.keys())[0]
        mock_agent.approve(approval_id, approved_by="tester")
        daemon.resume(task_id)
        task = daemon.manager.get(task_id)
        assert task is not None
        assert task.status in ("completed", "failed")


def test_daemon_status_server(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        with urllib.request.urlopen(f"{base}/status") as resp:
            status = json.loads(resp.read().decode())
        assert status["running"] is False
        task_id = daemon.submit("test", max_attempts=1)
        daemon.tick()
        with urllib.request.urlopen(f"{base}/status") as resp:
            status = json.loads(resp.read().decode())
        assert status["pending_tasks"] == 0
        assert status["waiting_approval_tasks"] == 0
        log = urllib.request.urlopen(f"{base}/log").read().decode()
        assert "submitted task" in log
        assert task_id in log
    finally:
        daemon._stop_status_server()


def test_daemon_stop_via_http(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon.running = True
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        req = urllib.request.Request(f"{base}/stop", method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 202
        time.sleep(0.2)
        assert daemon.running is False
    finally:
        daemon._stop_status_server()


def test_daemon_start_runs_ticks_then_stops(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.05,
        idle_interval=0.05, max_consecutive_errors=5,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon.submit("test", max_attempts=1)
    thread = threading.Thread(target=daemon.start, daemon=True)
    thread.start()
    time.sleep(0.3)
    daemon.stop()
    thread.join(timeout=2)
    assert daemon.state.tasks_completed >= 1
    assert daemon.state.cycles >= 1


def test_daemon_orphan_recovery(tmp_store, mock_agent):
    tmp_store.add_task("orphan-1", "orphaned goal", status="running",
                       next_retry_ts=time.time() - 1)
    tmp_store.update_task("orphan-1", updated_ts=time.time() - 600)
    daemon = Daemon(store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1)
    assert daemon.manager is not None
    task = daemon.manager.get("orphan-1")
    assert task is not None
    assert task.status == "retry"


def test_daemon_chat_and_model_endpoints(tmp_store, mock_agent, monkeypatch, tmp_path):
    # Isolate config so /api/model reads an empty (mock) config, not the host's.
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path / ".praxis"))
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        # multi-turn chat round-trips through the mock LLM
        payload = json.dumps({"messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "hello praxis dashboard"},
        ]}).encode()
        req = urllib.request.Request(f"{base}/api/chat", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req) as resp:
            chat = json.loads(resp.read().decode())
        assert "hello praxis dashboard" in chat["text"]
        assert "model" in chat
        # model info is read-only and well-formed
        with urllib.request.urlopen(f"{base}/api/model") as resp:
            info = json.loads(resp.read().decode())
        assert "model" in info and "configured" in info
        # provider catalog exposes the expanded cloud picker
        with urllib.request.urlopen(f"{base}/api/providers") as resp:
            provs = json.loads(resp.read().decode())
        ids = [p["id"] for p in provs]
        for pid in ("ollama", "openai", "google", "groq", "mistral"):
            assert pid in ids
    finally:
        daemon._stop_status_server()


def _read_sse(port, received, deadline_s=5.0):
    """Connect to /events and collect raw SSE lines until the task event's data
    line (the one carrying the payload) arrives or the deadline passes."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=deadline_s)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        deadline = time.time() + deadline_s
        saw_task = False
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            received.append(text)
            if text.startswith("event: task"):
                saw_task = True
            elif saw_task and text.startswith("data:"):
                break
        conn.close()
    except Exception:  # pragma: no cover - reader is best-effort
        pass


def test_daemon_sse_delivers_to_each_client_and_no_leak(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1,
        idle_interval=0.1, status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    # Emitting with no subscribers must be a no-op (regression guard against the
    # old unbounded shared queue that grew forever when nobody was listening).
    daemon.emit_event("task", {"task_id": "ignored", "status": "running"})
    assert daemon._sse_client_count() == 0

    daemon.running = True  # required for the _serve_sse loop to stream
    daemon._start_status_server()
    try:
        # Two concurrent clients must each receive a full copy of every event
        # (the old single-Queue design split events across consumers).
        got_a: list[str] = []
        got_b: list[str] = []
        readers = [
            threading.Thread(target=_read_sse, args=(daemon.status_port, got_a), daemon=True),
            threading.Thread(target=_read_sse, args=(daemon.status_port, got_b), daemon=True),
        ]
        for t in readers:
            t.start()
        # Wait until both connections have registered before emitting (events are
        # fire-and-forget, so a pre-registration emit would be lost).
        for _ in range(60):
            if daemon._sse_client_count() >= 2:
                break
            time.sleep(0.05)
        assert daemon._sse_client_count() == 2

        daemon.emit_event("task", {"task_id": "task-xyz", "status": "running"})
        for t in readers:
            t.join(timeout=5)

        for blob in ("".join(got_a), "".join(got_b)):
            assert "event: connected" in blob
            assert "event: task" in blob
            assert "task-xyz" in blob
    finally:
        daemon._stop_status_server()
    # Shutdown sentinel must unblock and drop every subscriber (no connection leak).
    assert daemon._sse_client_count() == 0


def test_daemon_upload_is_post_only(tmp_store, mock_agent, tmp_path):
    work = tmp_path / "uploads"
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        work_dir=str(work), status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        boundary = "----praxisTestBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="hello.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello praxis\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"{base}/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req) as resp:
            out = json.loads(resp.read().decode())
        assert out["uploaded"] == 1
        assert out["files"] == ["hello.txt"]
        assert out["errors"] == []
        assert (work / "hello.txt").read_text() == "hello praxis"

        # A GET to /upload must no longer be routed to the upload handler.
        got_status = None
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/upload", method="GET"))
        except urllib.error.HTTPError as exc:
            got_status = exc.code
        assert got_status == 404
    finally:
        daemon._stop_status_server()


def test_dashboard_serves_upload_ui(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        with urllib.request.urlopen(f"{base}/") as resp:
            assert resp.headers.get_content_type() == "text/html"
            html = resp.read().decode("utf-8")
        # The Files panel + drag/drop picker must be wired into the dashboard...
        assert 'id="drop"' in html
        assert 'id="fileInput"' in html
        assert "function initUpload" in html
        assert "initUpload();" in html
        # ...and it must post to the upload endpoint.
        assert "/upload" in html
    finally:
        daemon._stop_status_server()


def test_daemon_upload_accepts_multiple_files(tmp_store, mock_agent, tmp_path):
    work = tmp_path / "multi"
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        work_dir=str(work), status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        boundary = "----praxisMulti"
        parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            f"{content}\r\n"
            for name, content in (("a.txt", "alpha"), ("b.txt", "beta"))
        ]
        body = ("".join(parts) + f"--{boundary}--\r\n").encode()
        req = urllib.request.Request(
            f"{base}/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req) as resp:
            out = json.loads(resp.read().decode())
        assert out["uploaded"] == 2
        assert sorted(out["files"]) == ["a.txt", "b.txt"]
        assert (work / "a.txt").read_text() == "alpha"
        assert (work / "b.txt").read_text() == "beta"
    finally:
        daemon._stop_status_server()
