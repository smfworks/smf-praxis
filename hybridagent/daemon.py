"""Persistent daemon runtime for Praxis.

The daemon turns the agent from a request/response tool into a long-running
worker that:

* pulls tasks from the durable ``TaskManager`` queue,
* executes read/draft steps autonomously,
* pauses at ``SEND``/``DESTRUCTIVE`` approvals (leaving the task in
  ``waiting_approval``),
* retries transient failures with exponential backoff,
* recovers orphaned tasks on startup,
* exposes a small in-process HTTP control plane and a single-page chat UI
  so users can interact with the agent conversationally.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
import re
from typing import Any

from . import config as cfg
from .agent import PraxisAgent
from .broker import RiskClass
from .llm import LLMClient
from .logging_util import get_logger
from .persistence import Store
from .task_manager import TaskManager

_T = Any

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8643
_PID_FILE = cfg.home_dir() / "daemon.pid"
_LOG_FILE = cfg.home_dir() / "daemon.log"
_STATE_FILE = cfg.home_dir() / "daemon.state.json"
_PID_PORT_FILE = cfg.home_dir() / "daemon.port"


@dataclass
class DaemonState:
    running: bool = False
    started_ts: float | None = None
    stopped_ts: float | None = None
    last_tick_ts: float | None = None
    cycles: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_waiting_approval: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_ts": self.started_ts,
            "stopped_ts": self.stopped_ts,
            "last_tick_ts": self.last_tick_ts,
            "cycles": self.cycles,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "tasks_waiting_approval": self.tasks_waiting_approval,
            "errors": self.errors[-20:],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DaemonState":
        return cls(
            running=d.get("running", False),
            started_ts=d.get("started_ts"),
            stopped_ts=d.get("stopped_ts"),
            last_tick_ts=d.get("last_tick_ts"),
            cycles=d.get("cycles", 0),
            tasks_completed=d.get("tasks_completed", 0),
            tasks_failed=d.get("tasks_failed", 0),
            tasks_waiting_approval=d.get("tasks_waiting_approval", 0),
            errors=d.get("errors", []),
        )


def _read_state() -> DaemonState:
    if _STATE_FILE.exists():
        try:
            return DaemonState.from_dict(json.loads(_STATE_FILE.read_text()))
        except Exception:
            pass
    return DaemonState()


def _write_state(state: DaemonState) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2, default=str))


def _find_port(host: str = _DEFAULT_HOST, start: int = _DEFAULT_PORT,
               end: int = _DEFAULT_PORT + 100) -> int:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"no free port on {host} between {start} and {end}")


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Praxis</title>
<style>
:root { color-scheme: light dark; --bg: #0b0d10; --panel: #151820; --text: #e8ebf0;
        --muted: #8b94a5; --accent: #5b8def; --ok: #3ccf6d; --warn: #f5a623; --bad: #ff4d4f;
        --border: #2a2f3a; }
body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); }
header { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem;
         border-bottom: 1px solid var(--border); background: var(--panel); }
header h1 { margin: 0; font-size: 1.25rem; letter-spacing: -0.02em; }
.badge { font-size: 0.75rem; padding: 0.25rem 0.6rem; border-radius: 999px; background: var(--border);
         color: var(--muted); }
.badge.ok { background: rgba(60,207,109,.15); color: var(--ok); }
.badge.warn { background: rgba(245,166,35,.15); color: var(--warn); }
.badge.bad { background: rgba(255,77,79,.15); color: var(--bad); }
main { display: grid; grid-template-columns: 1fr 22rem; gap: 1rem; padding: 1rem; max-width: 1400px; margin: 0 auto; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1rem; }
.panel h2 { margin: 0 0 0.75rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
#chat { display: flex; flex-direction: column; height: calc(100vh - 10rem); }
.messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.75rem; padding-right: 0.25rem; }
.message { display: flex; gap: 0.75rem; }
.message.user { flex-direction: row-reverse; }
.bubble { max-width: 80%; padding: 0.75rem 1rem; border-radius: 1rem; background: var(--border); font-size: 0.95rem; line-height: 1.45; white-space: pre-wrap; }
.message.user .bubble { background: var(--accent); color: #fff; }
.message.agent .bubble { border-top-left-radius: 0.25rem; }
.meta { font-size: 0.75rem; color: var(--muted); margin-top: 0.25rem; }
.input-row { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
#message { flex: 1; padding: 0.75rem 1rem; border-radius: 0.6rem; border: 1px solid var(--border); background: var(--bg);
           color: var(--text); font-size: 1rem; outline: none; }
#message:focus { border-color: var(--accent); }
button { padding: 0.6rem 1rem; border: none; border-radius: 0.6rem; background: var(--accent); color: #fff;
         font-size: 0.9rem; cursor: pointer; }
button.secondary { background: var(--border); color: var(--text); }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.mode { display: flex; gap: 0.25rem; margin-bottom: 0.5rem; }
.mode button { padding: 0.4rem 0.75rem; font-size: 0.8rem; }
.mode button.active { background: var(--accent); }
.task, .approval { padding: 0.6rem; border: 1px solid var(--border); border-radius: 0.5rem; margin-bottom: 0.5rem; }
.task.pending { border-left: 3px solid var(--warn); }
.task.completed { border-left: 3px solid var(--ok); }
.task.waiting_approval { border-left: 3px solid var(--bad); }
.task.failed { border-left: 3px solid var(--bad); }
.task-id { font-size: 0.75rem; color: var(--muted); }
.task-goal { font-size: 0.9rem; margin: 0.2rem 0; }
.task-status { font-size: 0.75rem; text-transform: uppercase; }
.actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; }
.actions button { padding: 0.35rem 0.7rem; font-size: 0.8rem; }
pre.logs { white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
           font-size: 0.8rem; color: var(--muted); max-height: 200px; overflow-y: auto; }
.empty { color: var(--muted); font-size: 0.9rem; }
.spinner { width: 1rem; height: 1rem; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%;
           animation: spin 1s linear infinite; display: inline-block; vertical-align: middle; margin-right: 0.4rem; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>Praxis</h1>
  <span id="status" class="badge">checking…</span>
</header>
<main>
  <section id="chat" class="panel">
    <div class="mode">
      <button id="mode-ask" class="active" onclick="setMode('ask')">Ask</button>
      <button id="mode-do" onclick="setMode('do')">Do</button>
    </div>
    <div id="messages" class="messages"></div>
    <form id="composer" class="input-row" onsubmit="send(event)">
      <input id="message" type="text" placeholder="Ask a question or give a goal…" autocomplete="off" />
      <button type="submit" id="send">Send</button>
    </form>
  </section>
  <aside>
    <div class="panel">
      <h2>Queue</h2>
      <div id="tasks"><div class="empty">No tasks yet.</div></div>
    </div>
    <div class="panel">
      <h2>Approvals</h2>
      <div id="approvals"><div class="empty">Nothing waiting approval.</div></div>
    </div>
    <div class="panel">
      <h2>Logs</h2>
      <pre id="logs" class="logs">--</pre>
    </div>
  </aside>
</main>
<script>
let mode = 'ask';
const messages = document.getElementById('messages');
const statusEl = document.getElementById('status');
function setMode(m) { mode = m; document.getElementById('mode-ask').classList.toggle('active', m==='ask');
  document.getElementById('mode-do').classList.toggle('active', m==='do');
  document.getElementById('message').placeholder = m==='ask' ? 'Ask a question…' : 'Tell Praxis what to do…'; }
function append(role, html, meta='') {
  const row = document.createElement('div'); row.className = 'message ' + role;
  row.innerHTML = '<div class="bubble">' + html + '<div class="meta">' + meta + '</div></div>';
  messages.appendChild(row); messages.scrollTop = messages.scrollHeight;
}
async function api(path, opts) { return fetch(path, opts).then(r => r.json().catch(() => ({}))); }
async function send(ev) {
  ev.preventDefault();
  const input = document.getElementById('message'); const text = input.value.trim(); if (!text) return;
  input.value = '';
  append('user', text);
  const sendBtn = document.getElementById('send'); sendBtn.disabled = true;
  append('agent', '<span class="spinner"></span>thinking…');
  if (mode === 'ask') {
    const res = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: text})});
    messages.lastChild.remove();
    append('agent', res.text || res.error || 'No answer.', (res.citations||[]).join(', '));
  } else {
    const res = await api('/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({goal: text, max_attempts: 3})});
    messages.lastChild.remove();
    append('agent', res.task_id ? 'Queued task ' + res.task_id : (res.error || 'Could not queue task.'));
  }
  sendBtn.disabled = false; refresh();
}
async function approve(id) {
  const res = await api('/api/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
  append('agent', res.approved ? 'Approved ' + id : ('Could not approve: ' + (res.error||'')));
  refresh();
}
async function refresh() {
  const st = await api('/status');
  statusEl.textContent = st.running ? 'running' : 'stopped';
  statusEl.className = 'badge ' + (st.running ? 'ok' : 'bad');
  const tasks = await api('/api/tasks');
  const taskEl = document.getElementById('tasks');
  taskEl.innerHTML = tasks.length ? '' : '<div class="empty">No tasks yet.</div>';
  tasks.forEach(t => {
    const div = document.createElement('div'); div.className = 'task ' + t.status;
    div.innerHTML = '<div class="task-id">' + t.task_id + '</div><div class="task-goal">' + escapeHtml(t.goal) + '</div>' +
      '<div class="task-status">' + t.status + (t.error ? ' — ' + escapeHtml(t.error) : '') + '</div>';
    taskEl.appendChild(div);
  });
  const appr = await api('/api/approvals');
  const apprEl = document.getElementById('approvals');
  apprEl.innerHTML = appr.length ? '' : '<div class="empty">Nothing waiting approval.</div>';
  appr.forEach(a => {
    const div = document.createElement('div'); div.className = 'approval';
    div.innerHTML = '<div class="task-id">' + a.approval_id + '</div><div class="task-goal">' + escapeHtml(a.tool) + '</div>' +
      '<div class="task-status">' + escapeHtml(a.preview || '') + '</div>' +
      '<div class="actions"><button onclick="approve(\'' + a.approval_id + '\')">Approve</button></div>';
    apprEl.appendChild(div);
  });
  const logs = await fetch('/log').then(r => r.text());
  document.getElementById('logs').textContent = logs || '--';
}
function escapeHtml(s) { return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>
"""


class _StatusHandler(BaseHTTPRequestHandler):
    # Clients that hold an open SSE connection. Closed when the daemon stops.
    sse_clients: list["_StatusHandler"] = []

    def __init__(self, daemon: "Daemon", *args, **kwargs) -> None:
        self.daemon = daemon
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:
        try:
            if self.path == "/stop":
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"stopping": true}')
                threading.Thread(target=self.daemon.stop, daemon=True).start()
                return
            if self.path == "/submit":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                payload = json.loads(body)
                task_id = self.daemon.submit(payload["goal"], max_attempts=payload.get("max_attempts", 3))
                result = json.dumps({"task_id": task_id}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(result)
                return
            if self.path == "/api/ask":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                payload = json.loads(body)
                answer = self.daemon.ask(payload.get("question", ""), k=payload.get("k", 5))
                self._json_response({
                    "text": answer.text,
                    "abstained": answer.abstained,
                    "citations": answer.citations,
                    "sources_used": answer.sources_used,
                    "contradictions": [
                        {"a": c.a_source, "b": c.b_source, "score": c.score, "why": c.explanation}
                        for c in getattr(answer, "contradictions", [])
                    ],
                })
                return
            if self.path == "/api/approve":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                payload = json.loads(body)
                approval_id = payload.get("approval_id", "")
                approved = self.daemon.approve(approval_id)
                self._json_response({"approved": bool(approved), "approval_id": approval_id})
                return
            self.send_response(404)
            self.end_headers()
        except Exception as exc:
            self._error_response(exc)

    def do_GET(self) -> None:
        try:
            if self.path == "/status":
                body = json.dumps({
                    "running": self.daemon.running,
                    "port": self.daemon.status_port,
                    "state": self.daemon.state.to_dict(),
                    "pending_tasks": len(self.daemon.manager.list(status="pending")),
                    "running_tasks": len(self.daemon.manager.list(status="running")),
                    "waiting_approval_tasks": len(
                        self.daemon.manager.list(status="waiting_approval")
                    ),
                }, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/log":
                body = self.daemon.recent_logs().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
            elif self.path == "/":
                body = _DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif self.path == "/api/tasks":
                body = json.dumps(self.daemon.list_tasks(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/approvals":
                body = json.dumps(self.daemon.list_approvals(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/events":
                self._serve_sse()
                return
            elif self.path == "/upload":
                self._handle_upload()
                return
            else:
                body = b"not found"
                self.send_response(404)
        except Exception as exc:
            body = f"error: {exc}".encode()
            self.send_response(500)
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        _StatusHandler.sse_clients.append(self)
        self.wfile.write(b"event: connected\ndata: \"ok\"\n\n")
        try:
            while self.daemon.running and not self.wfile.closed:
                event = self.daemon.event_queue.get(timeout=10)
                if event is not None:
                    self.wfile.write(f"event: {event.get('type', 'message')}\n".encode())
                    self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, Empty, OSError):
            pass
        finally:
            try:
                _StatusHandler.sse_clients.remove(self)
            except ValueError:
                pass

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"expected multipart/form-data")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # Very small multipart parser: find boundary and file part.
        boundary = content_type.split("boundary=", 1)[1].encode()
        parts = body.split(b"--" + boundary)
        uploaded = 0
        saved_names: list[str] = []
        errors: list[str] = []
        for part in parts:
            if b'Content-Disposition' not in part or b'filename="' not in part:
                continue
            header, _, data = part.partition(b"\r\n\r\n")
            # Strip trailing CRLF before the boundary delimiter.
            data = data.rsplit(b"\r\n", 1)[0]
            name_match = re.search(rb'filename="([^"]+)"', header)
            if not name_match:
                continue
            filename = name_match.group(1).decode("utf-8", errors="replace")
            # Sanitize filename: basename only, no path traversal.
            filename = Path(filename).name
            if not filename:
                continue
            dest = self.daemon.work_dir_upload(filename)
            try:
                dest.write_bytes(data)
                uploaded += 1
                saved_names.append(filename)
            except Exception as exc:
                errors.append(f"{filename}: {exc}")
        self._json_response({"uploaded": uploaded, "files": saved_names, "errors": errors})

    def _json_response(self, payload: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, default=str).encode())

    def _error_response(self, exc: Exception) -> None:
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": str(exc)}).encode())

    def log_message(self, *args, **kwargs) -> None:
        # Suppress default HTTP logging noise.
        pass


class Daemon:
    """Long-running Praxis worker.

    Construct with ``store``/``agent``/``manager`` or use ``Daemon.from_env()``."""

    def __init__(
        self,
        store: Store | None = None,
        agent: PraxisAgent | None = None,
        manager: TaskManager | None = None,
        llm: LLMClient | None = None,
        tick_interval: float = 5.0,
        idle_interval: float = 30.0,
        max_consecutive_errors: int = 10,
        status_host: str = _DEFAULT_HOST,
        status_port: int | None = None,
        work_dir: str | None = None,
    ) -> None:
        self.store = store
        self.agent = agent
        self.manager = manager or (TaskManager(store) if store else None)
        self.llm = llm or LLMClient()
        self.tick_interval = tick_interval
        self.idle_interval = idle_interval
        self.max_consecutive_errors = max_consecutive_errors
        self.status_host = status_host
        self.status_port = status_port or _find_port(status_host)
        self.work_dir = work_dir
        self.log = get_logger("praxis.daemon")
        self.running = False
        self._stop_event = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._consecutive_errors = 0
        self._log_buffer: list[str] = []
        self.event_queue: Queue[dict[str, _T]] = Queue()
        self.state = _read_state()
        self.state.running = False
        self._setup_signal_handlers()

    def work_dir_upload(self, filename: str) -> Path:
        base = self.work_dir or os.environ.get("PRAXIS_WORK_DIR") or os.getcwd()
        root = Path(base).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / Path(filename).name

    def emit_event(self, event_type: str, payload: dict[str, _T]) -> None:
        event = {"type": event_type, "ts": time.time(), "payload": payload}
        self.event_queue.put(event)
        for client in list(_StatusHandler.sse_clients):
            try:
                client.wfile.write(f"event: {event_type}\n".encode())
                client.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                client.wfile.flush()
            except Exception:
                pass

    @classmethod
    def from_env(cls, work_dir: str | None = None,
                 autonomous_risks: set[RiskClass] | None = None,
                 status_port: int | None = None) -> "Daemon":
        store = Store.open()
        agent = PraxisAgent.persistent(llm=LLMClient(), work_dir=work_dir)
        if autonomous_risks is not None:
            agent.broker.policy.autonomous_risks = set(autonomous_risks)
        return cls(store=store, agent=agent, status_port=status_port)

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except Exception:
                pass

    def _on_signal(self, _signum, _frame) -> None:
        self.log.info("shutdown signal received")
        self.stop()

    def _ensure_agent(self) -> None:
        if self.agent is not None:
            return
        self.agent = PraxisAgent.persistent(llm=self.llm, work_dir=self.work_dir)
        self.store = self.agent.store
        if self.manager is None:
            self.manager = TaskManager(self.store)
        # Ensure broker allowlist covers whatever registry the agent built.
        self.agent.broker.policy.allowed_tools.update(self.agent.registry.names())

    def _start_status_server(self) -> None:
        daemon = self

        class Handler(_StatusHandler):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(daemon, *args, **kwargs)

        self._server = ThreadingHTTPServer((self.status_host, self.status_port), Handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="praxis-status"
        )
        self._server_thread.start()

    def _stop_status_server(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._server_thread = None

    def _log(self, level: str, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {level} {message}"
        self._log_buffer.append(line)
        self._log_buffer = self._log_buffer[-500:]
        getattr(self.log, level.lower(), self.log.info)(message)

    def recent_logs(self, lines: int = 100) -> str:
        return "\n".join(self._log_buffer[-lines:])

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> int:
        """Run the daemon loop synchronously until ``stop()`` is called."""
        if self.running:
            return 1
        self._ensure_agent()
        self.running = True
        self._stop_event.clear()
        self.state = DaemonState(running=True, started_ts=time.time())
        _write_pid(self.status_port)
        self._start_status_server()
        self._log("info", f"daemon started on {self.status_host}:{self.status_port}")
        try:
            while self.running and not self._stop_event.is_set():
                self.tick()
                if self._consecutive_errors >= self.max_consecutive_errors:
                    self._log("error", "too many consecutive errors; shutting down")
                    break
                self._wait_for_next_tick()
        finally:
            self._shutdown()
        return 0

    def _wait_for_next_tick(self) -> None:
        if not self.running:
            return
        # If there is pending work, tick quickly; otherwise idle.
        has_work = self.manager is not None and bool(
            self.manager.list(status="pending") or self.manager.list(status="retry")
        )
        interval = self.tick_interval if has_work else self.idle_interval
        self._stop_event.wait(interval)

    def _shutdown(self) -> None:
        self.running = False
        self.state.running = False
        self.state.stopped_ts = time.time()
        _write_state(self.state)
        _remove_pid()
        self._stop_status_server()
        self._log("info", "daemon stopped")

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self._stop_event.set()

    # ---------------------------------------------------------------------- tick
    def tick(self) -> None:
        """Process one ready task. If none are ready, run a heartbeat cycle."""
        if self.agent is None:
            self._ensure_agent()
        self.state.last_tick_ts = time.time()
        self.state.cycles += 1
        try:
            task = self._next_task()
            if task is None:
                self._log("debug", "no ready tasks; running heartbeat")
                self.agent.heartbeat(refresh_wiki=False)
                self._consecutive_errors = 0
                _write_state(self.state)
                return
            self._run_task(task)
            self._consecutive_errors = 0
        except Exception as exc:
            self._consecutive_errors += 1
            msg = f"tick error: {exc}"
            self.state.errors.append(msg)
            self._log("error", msg)
        _write_state(self.state)

    def _next_task(self) -> Any | None:
        if self.manager is None:
            return None
        for status in ("running", "pending", "retry"):
            tasks = self.manager.list(status=status, limit=10)
            for t in tasks:
                row = self.manager.store.get_task(t.task_id)
                if row.get("next_retry_ts") and row["next_retry_ts"] > time.time():
                    continue
                return t
        return None

    def _run_task(self, task) -> None:
        self.emit_event("task", {"task_id": task.task_id, "status": "running", "goal": task.goal})
        self._log("info", f"running task {task.task_id}: {task.goal}")
        updated = self.manager.run_once(task.task_id, self.agent)
        if updated.status == "completed":
            self.state.tasks_completed += 1
            self._log("info", f"task {task.task_id} completed")
        elif updated.status == "failed":
            self.state.tasks_failed += 1
            self._log("error", f"task {task.task_id} failed: {updated.error}")
        elif updated.status == "waiting_approval":
            self.state.tasks_waiting_approval += 1
            self._log("info", f"task {task.task_id} waiting approval")
        else:
            self._log("info", f"task {task.task_id} -> {updated.status}")
        assert self.manager is not None
        row = self.manager.store.get_task(task.task_id) or {}
        self.emit_event("task", {
            "task_id": task.task_id,
            "status": updated.status,
            "goal": task.goal,
            "output": row.get("output", ""),
            "error": row.get("error", ""),
        })

    # -------------------------------------------------------------- external API
    def submit(self, goal: str, max_attempts: int = 3) -> str:
        """Enqueue a new task. Safe to call from the CLI while the daemon runs."""
        if self.agent is None:
            self._ensure_agent()
        task = self.manager.create(goal, max_attempts=max_attempts)
        self._log("info", f"submitted task {task.task_id}")
        return task.task_id

    def ask(self, question: str, k: int = 5) -> Any:
        """Answer a question grounded in the agent's KB and memory."""
        self._ensure_agent()
        assert self.agent is not None
        return self.agent.ask(question, k=k, refresh_wiki=False)

    def approve(self, approval_id: str) -> bool:
        """Approve a pending consequential action and resume any waiting task."""
        self._ensure_agent()
        assert self.agent is not None
        approved = self.agent.broker.approve(approval_id, approved_by="web-ui")
        if approved is None:
            return False
        # Resume any task whose stored result references this approval.
        assert self.manager is not None
        for task in self.manager.list(status="waiting_approval", limit=100):
            assert self.manager is not None
            row = self.manager.store.get_task(task.task_id)
            result = row.get("result") or {}
            pending = result.get("pending_approvals", [])
            if any(pa.get("approval_id") == approval_id for pa in pending):
                self._log("info", f"resuming task {task.task_id} after approval")
                threading.Thread(target=self.resume, args=(task.task_id,), daemon=True).start()
        return True

    def list_tasks(self) -> list[dict]:
        """Return durable task queue snapshot for the dashboard."""
        if self.manager is None:
            return []
        out = []
        for status in ("pending", "running", "waiting_approval", "retry", "completed", "failed"):
            for t in self.manager.list(status=status, limit=50):
                row = {"task_id": t.task_id, "goal": t.goal, "status": t.status,
                       "output": t.output, "error": t.error}
                out.append(row)
        # Most recent first.
        return sorted(out, key=lambda r: r["task_id"], reverse=True)

    def list_approvals(self) -> list[dict]:
        """Return pending broker approvals for the dashboard."""
        self._ensure_agent()
        assert self.agent is not None
        return [
            {
                "approval_id": a.approval_id,
                "tool": a.tool,
                "preview": a.preview,
                "rationale": a.rationale,
                "cycle_id": a.cycle_id,
            }
            for a in self.agent.broker.pending.values()
        ]

    def resume(self, task_id: str) -> Any:
        """Re-process a task that is waiting approval. Called after a human approves
        held actions so the daemon can continue the cycle."""
        if self.agent is None:
            self._ensure_agent()
        assert self.agent is not None
        assert self.manager is not None
        task = self.manager.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != "waiting_approval":
            return task
        # Execute any fully-approved actions for this task directly before
        # re-running the agent, because re-running the planner would re-queue
        # the same consequential approval rather than completing it.
        report_actions = []
        for pa in list(self.agent.broker.pending.values()):
            if pa.cycle_id != task.cycle_id:
                continue
            tool = self.agent.registry.get(pa.tool)
            if tool is None:
                continue
            try:
                result = tool.run(**pa.args)
            except Exception as exc:
                assert self.store is not None
                self.store.update_task(
                    task_id, status="failed", error=f"{pa.tool} failed: {exc}",
                )
                return self.manager.get(task_id)
            report_actions.append(f"[send] {pa.tool} -> {result}")
        # Mark the task completed once the approved action has run.
        assert self.store is not None
        self.store.update_task(
            task_id, status="completed", error="",
            result_json=json.dumps({
                "cycle_id": task.cycle_id,
                "actions": report_actions,
                "pending_approvals": [],
            })
        )
        return self.manager.get(task_id)


def _write_pid(port: int) -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(f"{os.getpid()}\n")
    _PID_PORT_FILE.write_text(f"{port}\n")


def _remove_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
        _PID_PORT_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _read_pid() -> int | None:
    if _PID_PORT_FILE.exists():
        try:
            return int(_PID_PORT_FILE.read_text().strip())
        except Exception:
            return None
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text().strip())
    except Exception:
        return None


def daemon_status(host: str = _DEFAULT_HOST) -> dict[str, Any]:
    """Fetch status from a running daemon. Returns a dict whether or not the
    daemon is reachable."""
    port = _read_pid()
    if port is None:
        return {"running": False, "reason": "no pid file"}
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/status", timeout=5
        ) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        return {"running": False, "reason": str(exc), "port": port}


def daemon_logs(host: str = _DEFAULT_HOST, lines: int = 100) -> str:
    port = _read_pid()
    if port is None:
        return "daemon not running (no pid file)"
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/log", timeout=5
        ) as resp:
            return resp.read().decode()
    except Exception as exc:
        return f"could not fetch logs: {exc}"
