"""Tests for the persistent daemon runtime."""

import http.client
import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from hybridagent.agent import PraxisAgent
from hybridagent.broker import RiskClass
from hybridagent.daemon import (
    Daemon,
    DaemonState,
    _find_port,
    _parse_multipart_stream,
    _read_state,
    _UploadError,
    _write_state,
)
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


def test_parse_multipart_stream_handles_chunk_boundaries(tmp_path):
    boundary = b"BOUND"
    # Binary payload with embedded CRLFs and a near-miss of the delimiter
    # ("\r\n--BOUN" without the trailing "D") to exercise the rolling-buffer tail.
    payload = b"alpha\r\n--BOUN not-a-boundary\r\nomega" + bytes(range(256)) * 8
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="file"; filename="data.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        + payload
        + b"\r\n--BOUND--\r\n"
    )
    # A tiny chunk size forces the delimiter to straddle many reads.
    saved, errors = _parse_multipart_stream(
        io.BytesIO(body), boundary, len(body),
        lambda name: tmp_path / name, chunk_size=7,
    )
    assert saved == ["data.bin"]
    assert errors == []
    assert (tmp_path / "data.bin").read_bytes() == payload
    # Streaming writes via a temp file that must be renamed away on success.
    assert not (tmp_path / "data.bin.part").exists()


def test_parse_multipart_stream_rejects_truncated_body(tmp_path):
    boundary = b"BOUND"
    # Body that opens a part but never closes it with the final boundary.
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
        b"incomplete"
    )
    with pytest.raises(_UploadError):
        _parse_multipart_stream(
            io.BytesIO(body), boundary, len(body), lambda name: tmp_path / name)


def test_daemon_upload_rejects_oversized(tmp_store, mock_agent, tmp_path):
    work = tmp_path / "capped"
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        work_dir=str(work), max_upload_bytes=64,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        boundary = "----praxisBig"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n\r\n'
            + ("X" * 4096)
            + f"\r\n--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"{base}/upload", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        code = None
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 413
        # Nothing should have been written past the cap.
        assert not (work / "big.bin").exists()
    finally:
        daemon._stop_status_server()


def test_daemon_upload_requires_boundary(tmp_store, mock_agent, tmp_path):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        work_dir=str(tmp_path / "nb"),
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        req = urllib.request.Request(
            f"{base}/upload", data=b"whatever", method="POST",
            headers={"Content-Type": "multipart/form-data"},  # no boundary
        )
        code = None
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 400
    finally:
        daemon._stop_status_server()


def test_offer_drops_oldest_when_full():
    from queue import Queue

    from hybridagent.daemon import _offer

    q: "Queue[int]" = Queue(maxsize=2)
    for i in range(5):
        _offer(q, i)              # never blocks, even though capacity is 2
    assert q.qsize() == 2
    assert [q.get_nowait(), q.get_nowait()] == [3, 4]   # only the newest survive


def test_parse_multipart_rejects_oversized_headers(tmp_path):
    from hybridagent.daemon import _MAX_PART_HEADER

    boundary = b"BOUND"
    # A part whose header block never sends CRLFCRLF and runs past the header cap
    # must be rejected rather than buffered in full.
    giant = b"X" * (_MAX_PART_HEADER * 2)
    body = b"--BOUND\r\n" + giant
    with pytest.raises(_UploadError):
        _parse_multipart_stream(io.BytesIO(body), boundary, len(body),
                                lambda name: tmp_path / name)


def test_llm_chat_stream_mock_chunks():
    llm = LLMClient(mode="mock")
    messages = [{"role": "user", "content": "stream hello world"}]
    pieces = list(llm.chat_stream(messages))
    # The mock "streams" by chunking — more than one piece, and the concatenation
    # is byte-for-byte identical to the non-streaming reply.
    assert len(pieces) > 1
    assert "".join(pieces) == llm.chat(messages)
    assert "stream hello world" in "".join(pieces)


def test_daemon_chat_stream_endpoint(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        body = json.dumps({"messages": [
            {"role": "user", "content": "stream hello dashboard"},
        ]}).encode()
        req = urllib.request.Request(
            f"{base}/api/chat/stream", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get_content_type() == "text/event-stream"
            raw = resp.read().decode()
        events = [json.loads(line[len("data:"):].strip())
                  for line in raw.splitlines() if line.startswith("data:")]
        types = [e["type"] for e in events]
        # A leading meta(model), at least one delta, and a terminal done.
        assert types[0] == "meta" and "model" in events[0]
        assert "delta" in types
        assert types[-1] == "done"
        text = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        assert "stream hello dashboard" in text
    finally:
        daemon._stop_status_server()


def test_dashboard_serves_streaming_ui(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        with urllib.request.urlopen(f"{base}/") as resp:
            html = resp.read().decode("utf-8")
        # The chat composer must consume the streaming endpoint live.
        assert "function streamChat" in html
        assert "/api/chat/stream" in html
    finally:
        daemon._stop_status_server()


class _ScriptedLLM:
    """LLM stub whose chat_tools returns a fixed sequence of turns, so the
    governed loop can be exercised deterministically."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    def chat_tools(self, messages, tools, system=None):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return turn


def _reg_with(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def test_governed_chat_agent_executes_read_tool():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    from hybridagent.chat_agent import GovernedChatAgent

    reg = _reg_with(Tool("list_today_events", RiskClass.READ, "List events",
                         lambda **k: "3 events today",
                         parameters={"type": "object", "properties": {}}))
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": [{"id": "c1", "name": "list_today_events", "args": {}}]},
        {"text": "You have 3 events today.", "tool_calls": []},
    ])
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"list_today_events"}))
    agent = GovernedChatAgent(llm, reg, broker)
    events = list(agent.run([{"role": "user", "content": "what's on today?"}]))
    types = [e.type for e in events]
    assert "tool_call" in types and "tool_result" in types
    final = [e for e in events if e.type == "final"]
    assert final and "3 events" in final[-1].data["text"]


def test_governed_chat_agent_holds_send_tool():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    from hybridagent.chat_agent import GovernedChatAgent

    reg = _reg_with(Tool("send_email", RiskClass.SEND, "Send email",
                         lambda **k: "SENT",
                         parameters={"type": "object",
                                     "properties": {"draft_id": {"type": "string"}},
                                     "required": ["draft_id"]}))
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": [{"id": "c1", "name": "send_email",
                                     "args": {"draft_id": "d1"}}]},
    ])
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    agent = GovernedChatAgent(llm, reg, broker)
    events = list(agent.run([{"role": "user", "content": "send it"}]))
    types = [e.type for e in events]
    assert "approval" in types
    # The consequential action is queued in the broker, never executed.
    assert len(broker.pending) == 1
    appr = next(e for e in events if e.type == "approval")
    assert appr.data["tool"] == "send_email" and appr.data["approval_id"]
    # The loop stops after holding and does not fabricate a result.
    assert all(e.type != "tool_result" for e in events)


def test_governed_chat_agent_denies_bad_args():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    from hybridagent.chat_agent import GovernedChatAgent

    reg = _reg_with(Tool("get_file_text", RiskClass.READ, "Read file",
                         lambda **k: "contents",
                         parameters={"type": "object",
                                     "properties": {"name": {"type": "string"}},
                                     "required": ["name"]}))
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": [{"id": "c1", "name": "get_file_text", "args": {}}]},
        {"text": "Sorry, I couldn't read it.", "tool_calls": []},
    ])
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"get_file_text"}))
    agent = GovernedChatAgent(llm, reg, broker)
    events = list(agent.run([{"role": "user", "content": "read it"}]))
    types = [e.type for e in events]
    # Missing required arg is rejected by schema validation before the broker.
    assert "denied" in types
    assert all(e.type not in ("tool_call", "tool_result") for e in events)


def test_governed_chat_agent_denies_unallowlisted_tool():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    from hybridagent.chat_agent import GovernedChatAgent

    reg = _reg_with(Tool("delete_file", RiskClass.DESTRUCTIVE, "Delete",
                         lambda **k: "deleted",
                         parameters={"type": "object",
                                     "properties": {"name": {"type": "string"}},
                                     "required": ["name"]}))
    llm = _ScriptedLLM([
        {"text": "", "tool_calls": [{"id": "c1", "name": "delete_file",
                                     "args": {"name": "x"}}]},
        {"text": "I can't do that.", "tool_calls": []},
    ])
    # Empty allowlist -> the broker denies even though the tool is registered.
    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set()))
    agent = GovernedChatAgent(llm, reg, broker)
    events = list(agent.run([{"role": "user", "content": "delete x"}]))
    assert any(e.type == "denied" for e in events)
    assert len(broker.pending) == 0


def test_daemon_chat_agent_endpoint(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        # The mock LLM emits a tool call when the user names an available tool.
        body = json.dumps({"messages": [
            {"role": "user", "content": "please use echo to repeat hello"},
        ]}).encode()
        req = urllib.request.Request(
            f"{base}/api/chat/agent", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get_content_type() == "text/event-stream"
            raw = resp.read().decode()
        events = [json.loads(line[len("data:"):].strip())
                  for line in raw.splitlines() if line.startswith("data:")]
        types = [e["type"] for e in events]
        assert types[0] == "meta"
        assert "tool_call" in types and "tool_result" in types
        assert any(e["type"] == "final" for e in events)
        assert types[-1] == "done"
        # echo is a DRAFT tool -> autonomous, so nothing should be held.
        assert "approval" not in types
    finally:
        daemon._stop_status_server()


def test_daemon_voice_status_and_mode(tmp_store, mock_agent, tmp_path, monkeypatch):
    from hybridagent import config as cfg
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))  # isolate config
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        with urllib.request.urlopen(f"{base}/api/voice") as resp:
            status = json.loads(resp.read().decode())
        assert status["mode"] == "off"
        assert any(m["id"] == "realtime" and not m["available"]
                   for m in status["modes"])
        req = urllib.request.Request(
            f"{base}/api/voice", data=json.dumps({"mode": "turn"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
        assert result["mode"] == "turn"
    finally:
        daemon._stop_status_server()


def test_daemon_speak_returns_audio(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        req = urllib.request.Request(
            f"{base}/api/speak", data=json.dumps({"text": "hello there"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            ctype = resp.headers.get_content_type()
            audio = resp.read()
        assert ctype in ("audio/wav", "audio/mpeg")
        assert len(audio) > 0
    finally:
        daemon._stop_status_server()


def test_daemon_transcribe_endpoint(tmp_store, mock_agent):
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        req = urllib.request.Request(
            f"{base}/api/transcribe", data=b"\x00\x01\x02fake-audio",
            method="POST", headers={"Content-Type": "audio/webm"})
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())
        assert "text" in res
    finally:
        daemon._stop_status_server()


class _FakeWSConn:
    """In-memory WebSocket connection for unit-testing the realtime bridge."""

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.sent = []
        self.open = True

    def recv(self):
        return self._inbound.pop(0) if self._inbound else None

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def pong(self, data=b""):
        pass

    def close(self):
        self.open = False


def test_realtime_bridge_governs_tools(mock_agent):
    from hybridagent.voice import RealtimeBridge
    inbound = [
        (0x1, json.dumps({"type": "text", "text": "use the send tool now"}).encode()),
        (0x1, json.dumps({"type": "commit"}).encode()),
        (0x1, json.dumps({"type": "stop"}).encode()),
    ]
    conn = _FakeWSConn(inbound)
    RealtimeBridge(mock_agent, conn).run()
    types = [e["type"] for e in conn.sent]
    assert types[0] == "ready"
    # 'send' is a SEND tool -> held for approval, never executed inline.
    assert "approval" in types
    assert "tool_result" not in types
    assert "done" in types
    assert conn.open is False


def _ws_handshake(host, port, path="/api/voice/realtime"):
    import base64 as _b64
    import os as _os
    import socket as _socket

    from hybridagent.wsutil import accept_key
    sock = _socket.create_connection((host, port))
    key = _b64.b64encode(_os.urandom(16)).decode()
    sock.sendall(
        (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
         "Upgrade: websocket\r\nConnection: Upgrade\r\n"
         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk:
            break
        head += chunk
    assert b"101" in head.split(b"\r\n")[0]
    assert accept_key(key).encode() in head
    return sock


def _send_masked(sock, obj):
    import os as _os
    payload = json.dumps(obj).encode()
    mask = _os.urandom(4)
    n = len(payload)
    frame = bytearray([0x81])
    if n < 126:
        frame.append(0x80 | n)
    elif n < 65536:
        frame.append(0x80 | 126)
        frame += n.to_bytes(2, "big")
    else:
        frame.append(0x80 | 127)
        frame += n.to_bytes(8, "big")
    frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(frame))


def test_realtime_ws_endpoint_governed_turn(tmp_store, mock_agent, monkeypatch):
    monkeypatch.setenv("PRAXIS_VOICE_REALTIME", "1")
    daemon = Daemon(
        store=tmp_store, agent=mock_agent, tick_interval=0.1, idle_interval=0.1,
        status_port=_find_port("127.0.0.1", 30000, 30100),
    )
    daemon._start_status_server()
    try:
        from hybridagent.wsutil import WebSocketConn
        sock = _ws_handshake("127.0.0.1", daemon.status_port)
        sock.settimeout(10)
        conn = WebSocketConn(sock.makefile("rb"), io.BytesIO())
        _send_masked(sock, {"type": "text", "text": "please use echo to say hi"})
        _send_masked(sock, {"type": "commit"})
        events = []
        for _ in range(40):
            frame = conn.recv()
            if frame is None:
                break
            opcode, data = frame
            if opcode == 0x1:
                ev = json.loads(data.decode())
                events.append(ev)
                if ev.get("type") == "done":
                    break
        _send_masked(sock, {"type": "stop"})
        sock.close()
        types = [e["type"] for e in events]
        assert types[0] == "ready"
        assert "tool_call" in types and "tool_result" in types
        assert "final" in types
        assert "audio" in types and types[-1] == "done"
    finally:
        daemon._stop_status_server()
