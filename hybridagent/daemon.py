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

import io
import json
import os
import re
import signal
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs as parse_query
from urllib.parse import urlsplit as split_url

from . import config as cfg
from .agent import ApprovalExecution, PraxisAgent
from .api_contract import (
    API_VERSION,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_IDEMPOTENCY_RECEIPTS,
    MAX_JSON_BODY_BYTES,
    error_envelope,
    etag,
    normalize_limit,
    page_items,
    resource_version,
    success_envelope,
)
from .broker import PendingApproval, RiskClass
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

# How long an idle SSE connection waits before emitting a keep-alive comment.
# Bounds the worst-case shutdown latency for a connection blocked on its queue.
_SSE_HEARTBEAT_SECONDS = 15.0
# Per-connection SSE event backlog cap. A stalled client drops its oldest queued
# events rather than growing without bound (live events are advisory, not durable).
_SSE_QUEUE_MAXSIZE = 1024

# Maximum size (bytes) accepted for a single multipart upload request. Overridable
# per-Daemon or via the PRAXIS_MAX_UPLOAD_BYTES environment variable.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB
# Read granularity for streaming an upload body to disk.
_UPLOAD_CHUNK = 65536
# Cap a single part's header block so a body that never sends the header/body
# separator (CRLFCRLF) cannot buffer the whole (capped) request in memory.
_MAX_PART_HEADER = 64 * 1024  # 64 KiB


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _offer(q: "Queue[Any]", item: Any) -> None:
    """Put ``item`` on a bounded queue, dropping the oldest entry if it is full.

    Live SSE events are advisory: a stalled or dead subscriber must never block
    the emitter, so we make room by discarding the oldest queued event.
    """
    try:
        q.put_nowait(item)
    except Full:
        try:
            q.get_nowait()
        except Empty:
            pass
        try:
            q.put_nowait(item)
        except Full:
            pass


# Persona for the conversational chat surface (/api/chat). Kept short; the rich
# governance/agent behavior lives in the task pipeline, this is a direct,
# helpful conversation with the configured model.
_CHAT_SYSTEM = (
    "You are Praxis, a hybrid autonomous AI colleague. Be helpful, accurate, and "
    "concise. Use Markdown (headings, **bold**, lists, and fenced code blocks) to "
    "format answers clearly. If you are unsure, say so rather than inventing facts."
)

# Persona for the governed tool-calling surface (/api/chat/agent). The model may
# call tools to gather context and prepare work; the broker decides what runs.
_AGENT_SYSTEM = (
    "You are Praxis, a governed autonomous colleague. You can call tools to gather "
    "context and prepare work. Read and draft tools run automatically; send and "
    "destructive actions are held for the user's approval — never claim a held or "
    "denied action succeeded. Prefer calling a tool over guessing, ground your "
    "answers in tool results, and when an action is held or denied, say so plainly "
    "and continue with whatever you can safely do. Format answers with Markdown. "
    "For read-only tasks like summarizing or extracting information from a web "
    "page, use only browser_navigate, browser_find, and browser_read; do not click, "
    "type, scroll, or submit forms unless the user explicitly asks you to interact "
    "with the page."
)


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


_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Praxis</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0a0c10; --bg2: #0e1117; --panel: #14181f; --panel2: #1a1f29;
  --text: #e8ebf0; --muted: #8b94a5; --faint: #5b6472;
  --accent: #5b8def; --accent2: #7c5cff; --ok: #3ccf6d; --warn: #f5a623; --bad: #ff5a5f;
  --border: #232936; --shadow: 0 10px 30px rgba(0,0,0,.35);
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; color: var(--text);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(124,92,255,.12), transparent 60%),
    radial-gradient(1000px 500px at -10% 110%, rgba(91,141,239,.10), transparent 55%),
    var(--bg);
}
header {
  display: flex; align-items: center; gap: 1rem; padding: .85rem 1.25rem;
  border-bottom: 1px solid var(--border); background: rgba(14,17,23,.7);
  backdrop-filter: blur(12px); position: sticky; top: 0; z-index: 20;
}
.brand { display: flex; align-items: center; gap: .6rem; font-weight: 650; font-size: 1.15rem; letter-spacing: -.02em; }
.logo {
  width: 1.5rem; height: 1.5rem; border-radius: .5rem;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  box-shadow: 0 4px 14px rgba(124,92,255,.5);
}
.spacer { flex: 1; }
.pill {
  display: inline-flex; align-items: center; gap: .4rem; font-size: .78rem;
  padding: .3rem .7rem; border-radius: 999px; background: var(--panel2);
  border: 1px solid var(--border); color: var(--muted); white-space: nowrap;
}
.pill .dot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--faint); }
.pill.modelpill { color: var(--text); }
.pill.modelpill .dot { background: linear-gradient(135deg, var(--accent), var(--accent2)); }
.badge { font-size: .75rem; padding: .28rem .6rem; border-radius: 999px; background: var(--border); color: var(--muted); }
.badge.ok { background: rgba(60,207,109,.15); color: var(--ok); }
.badge.bad { background: rgba(255,90,95,.15); color: var(--bad); }
.pill.conn-live { color: var(--ok); }
.pill.conn-live .dot { background: var(--ok); }
.pill.conn-reconnecting { color: #f5a623; }
.pill.conn-reconnecting .dot { background: #f5a623; animation: connpulse 1s ease-in-out infinite; }
.pill.conn-offline { color: var(--bad); }
.pill.conn-offline .dot { background: var(--bad); }
@keyframes connpulse { 50% { opacity: .35; } }
.toasts { position: fixed; bottom: 1.1rem; right: 1.1rem; display: flex; flex-direction: column; gap: .5rem; z-index: 9999; }
.toast { padding: .55rem .85rem; border-radius: .6rem; font-size: .82rem; background: var(--panel2); color: var(--text); border: 1px solid var(--border); box-shadow: var(--shadow); opacity: 0; transform: translateY(8px); transition: opacity .25s ease, transform .25s ease; max-width: 22rem; }
.toast.show { opacity: 1; transform: none; }
.toast.warn { border-color: #f5a623; }
.toast.error { border-color: var(--bad); }
.toast.ok { border-color: var(--ok); }
.px-err { padding: .7rem; color: var(--bad); font-size: .82rem; }
.px-err .px-retry { margin-left: .4rem; cursor: pointer; background: var(--panel2); color: var(--text); border: 1px solid var(--border); border-radius: .4rem; padding: .15rem .5rem; font-size: .78rem; }

main { display: grid; grid-template-columns: 15rem 1fr 22rem; gap: 1rem; padding: 1rem; max-width: 1600px; margin: 0 auto; }
@media (max-width: 1200px) { main { grid-template-columns: 13rem 1fr 20rem; } }
@media (max-width: 980px) {
  main { grid-template-columns: 1fr; }
  aside { order: 2; }
  #historyRail { order: 3; height: auto; max-height: 18rem; }
}
.panel { background: linear-gradient(180deg, var(--panel), var(--bg2)); border: 1px solid var(--border); border-radius: 1rem; box-shadow: var(--shadow); }
.panel.pad { padding: 1rem; }
.panel h2 { margin: 0 0 .7rem; font-size: .72rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); }

/* chat column */
#chat { display: flex; flex-direction: column; height: calc(100vh - 6.5rem); min-height: 30rem; overflow: hidden; }
.chat-top { display: flex; align-items: center; gap: .75rem; padding: .8rem 1rem; border-bottom: 1px solid var(--border); }
.segmented { display: inline-flex; background: var(--bg); border: 1px solid var(--border); border-radius: .7rem; padding: .2rem; gap: .15rem; }
.segmented button { border: none; background: transparent; color: var(--muted); padding: .4rem .85rem; font-size: .82rem; border-radius: .5rem; cursor: pointer; transition: all .15s ease; }
.segmented button.active { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; box-shadow: 0 4px 12px rgba(91,141,239,.35); }
.segmented button:hover:not(.active) { color: var(--text); }
.chat-top .hint { font-size: .78rem; color: var(--faint); flex: 1; }
.ghost { border: 1px solid var(--border); background: var(--bg); color: var(--muted); padding: .4rem .7rem; border-radius: .6rem; font-size: .78rem; cursor: pointer; }
.ghost:hover { color: var(--text); border-color: var(--faint); }

.messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 1rem; padding: 1.25rem; scroll-behavior: smooth; }
.messages::-webkit-scrollbar { width: 10px; }
.messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }
.welcome { margin: auto; text-align: center; color: var(--muted); max-width: 26rem; }
.welcome h3 { color: var(--text); font-weight: 600; margin: 0 0 .4rem; }
.welcome p { font-size: .9rem; line-height: 1.5; }
.chips { display: flex; flex-wrap: wrap; gap: .5rem; justify-content: center; margin-top: 1rem; }
.chip { font-size: .8rem; padding: .45rem .8rem; border-radius: 999px; border: 1px solid var(--border); background: var(--panel2); color: var(--text); cursor: pointer; transition: all .15s; }
.chip:hover { border-color: var(--accent); transform: translateY(-1px); }

.msg { display: flex; gap: .8rem; align-items: flex-start; animation: rise .25s ease; }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.msg.user { flex-direction: row-reverse; }
.avatar { width: 2rem; height: 2rem; border-radius: .65rem; flex: none; display: grid; place-items: center; font-size: .8rem; font-weight: 700; }
.msg.agent .avatar { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; }
.msg.user .avatar { background: var(--panel2); border: 1px solid var(--border); color: var(--muted); }
.bubble-wrap { max-width: min(78%, 52rem); display: flex; flex-direction: column; gap: .25rem; }
.msg.user .bubble-wrap { align-items: flex-end; }
.bubble { padding: .8rem 1rem; border-radius: 1rem; font-size: .94rem; line-height: 1.6; overflow-wrap: anywhere; }
.msg.agent .bubble { background: var(--panel2); border: 1px solid var(--border); border-top-left-radius: .3rem; }
.msg.user .bubble { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; border-top-right-radius: .3rem; }
.meta { font-size: .7rem; color: var(--faint); padding: 0 .3rem; }

/* markdown */
.bubble p { margin: .2rem 0; }
.bubble p:first-child { margin-top: 0; }
.bubble p:last-child { margin-bottom: 0; }
.bubble h1, .bubble h2, .bubble h3, .bubble h4 { margin: .7rem 0 .35rem; line-height: 1.25; }
.bubble h1 { font-size: 1.3rem; } .bubble h2 { font-size: 1.15rem; } .bubble h3 { font-size: 1.02rem; } .bubble h4 { font-size: .95rem; }
.bubble ul, .bubble ol { margin: .35rem 0; padding-left: 1.3rem; }
.bubble li { margin: .15rem 0; }
.bubble a { color: #9db8ff; text-decoration: none; border-bottom: 1px solid rgba(157,184,255,.35); }
.bubble a:hover { border-bottom-color: #9db8ff; }
.bubble code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .86em; background: rgba(124,92,255,.14); padding: .12em .38em; border-radius: .35rem; }
.bubble blockquote { margin: .4rem 0; padding: .2rem .9rem; border-left: 3px solid var(--accent); color: var(--muted); }
.bubble hr { border: none; border-top: 1px solid var(--border); margin: .7rem 0; }
pre.code { position: relative; background: #0b0e14; border: 1px solid var(--border); border-radius: .7rem; padding: .85rem .9rem; overflow-x: auto; margin: .5rem 0; }
pre.code code { display: block; background: none; padding: 0; font-size: .84rem; line-height: 1.5; color: #d7dcea; }
pre.code .copy { position: absolute; top: .45rem; right: .45rem; font-size: .68rem; padding: .25rem .5rem; border-radius: .4rem; border: 1px solid var(--border); background: var(--panel2); color: var(--muted); cursor: pointer; opacity: 0; transition: opacity .15s; }
pre.code:hover .copy { opacity: 1; }

/* typing */
.typing .bubble { display: inline-flex; gap: .3rem; align-items: center; }
.typing .d { width: .45rem; height: .45rem; border-radius: 50%; background: var(--muted); animation: blink 1.2s infinite; }
.typing .d:nth-child(2) { animation-delay: .2s; } .typing .d:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 60%, 100% { opacity: .3; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-3px); } }

/* agent tool-step cards */
.steps { display: flex; flex-direction: column; gap: .35rem; }
.step { font-size: .82rem; padding: .45rem .6rem; border-radius: .55rem; border: 1px solid var(--border); background: var(--bg2); color: var(--muted); animation: rise .2s ease; }
.step b { color: var(--text); font-weight: 600; }
.step .rk { font-size: .64rem; text-transform: uppercase; letter-spacing: .04em; color: var(--faint); border: 1px solid var(--border); padding: .04rem .35rem; border-radius: 999px; margin-left: .3rem; }
.step .muted { color: var(--faint); }
.step.run { border-left: 3px solid var(--accent); }
.step.ok { border-left: 3px solid var(--ok); }
.step.hold { border-left: 3px solid var(--warn); }
.step.deny { border-left: 3px solid var(--bad); }

/* composer */
.composer { border-top: 1px solid var(--border); padding: .85rem 1rem; display: flex; gap: .6rem; align-items: flex-end; background: rgba(10,12,16,.4); }
#message { flex: 1; resize: none; max-height: 9rem; min-height: 2.7rem; padding: .7rem .9rem; border-radius: .8rem; border: 1px solid var(--border); background: var(--bg); color: var(--text); font: inherit; font-size: .95rem; line-height: 1.5; outline: none; transition: border-color .15s; }
#message:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(91,141,239,.15); }
.send-btn { display: grid; place-items: center; width: 2.7rem; height: 2.7rem; border: none; border-radius: .8rem; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; cursor: pointer; flex: none; box-shadow: 0 6px 16px rgba(91,141,239,.35); transition: transform .12s; }
.send-btn:hover { transform: translateY(-1px); }
.send-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }
.mic-btn { display: grid; place-items: center; width: 2.7rem; height: 2.7rem; border: 1px solid var(--border); border-radius: .8rem; background: var(--bg); color: var(--text); cursor: pointer; flex: none; font-size: 1.05rem; transition: all .12s; }
.mic-btn:hover { border-color: var(--accent); }
.mic-btn.recording { background: rgba(255,90,95,.18); border-color: var(--bad); color: var(--bad); animation: micpulse 1.2s infinite; }
@keyframes micpulse { 0%,100% { box-shadow: 0 0 0 0 rgba(255,90,95,.45); } 50% { box-shadow: 0 0 0 7px rgba(255,90,95,0); } }
.vmodes { display: flex; gap: .3rem; }
.vmode { flex: 1; border: 1px solid var(--border); background: var(--bg); color: var(--muted); padding: .4rem .3rem; border-radius: .6rem; font-size: .78rem; cursor: pointer; text-align: center; transition: all .12s; }
.vmode:hover:not(:disabled) { color: var(--text); border-color: var(--faint); }
.vmode.active { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; border-color: transparent; }
.vmode:disabled { opacity: .45; cursor: not-allowed; }

/* chat history rail */
.hist { display: flex; flex-direction: column; height: calc(100vh - 6.5rem); min-height: 30rem; overflow: hidden; }
.hist-head { display: flex; align-items: center; gap: .5rem; padding: .85rem .9rem; border-bottom: 1px solid var(--border); }
.hist-head h2 { margin: 0; flex: 1; }
.newchat { border: 1px solid var(--border); background: var(--bg); color: var(--text); padding: .35rem .6rem; border-radius: .6rem; font-size: .76rem; cursor: pointer; white-space: nowrap; transition: border-color .15s, color .15s; }
.newchat:hover { border-color: var(--accent); color: #fff; }
.hist-list { flex: 1; overflow-y: auto; padding: .5rem; display: flex; flex-direction: column; gap: .3rem; }
.hist-list::-webkit-scrollbar { width: 10px; }
.hist-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }
.hist-item { display: flex; align-items: center; gap: .4rem; padding: .5rem .55rem; border-radius: .55rem; border: 1px solid transparent; cursor: pointer; transition: background .12s, border-color .12s; }
.hist-item:hover { background: var(--panel2); }
.hist-item.active { background: var(--panel2); border-color: var(--accent); }
.hist-item .ht { flex: 1; min-width: 0; }
.hist-item .title { font-size: .82rem; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hist-item .when { font-size: .68rem; color: var(--faint); margin-top: .1rem; }
.hist-item .del { opacity: 0; border: none; background: transparent; color: var(--faint); cursor: pointer; font-size: .82rem; line-height: 1; padding: .15rem .3rem; border-radius: .4rem; flex: none; transition: opacity .12s, color .12s; }
.hist-item:hover .del { opacity: 1; }
.hist-item .del:hover { color: var(--bad); background: var(--bg); }
.hist-empty { color: var(--faint); font-size: .8rem; text-align: center; padding: 1.2rem .5rem; }

/* sidebar */
aside { display: flex; flex-direction: column; gap: 1rem; }
.field { display: flex; flex-direction: column; gap: .4rem; }
.field label { font-size: .72rem; color: var(--muted); }
select, .txt { width: 100%; padding: .55rem .7rem; border-radius: .6rem; border: 1px solid var(--border); background: var(--bg); color: var(--text); font: inherit; font-size: .85rem; outline: none; }
select:focus, .txt:focus { border-color: var(--accent); }
.row { display: flex; gap: .5rem; margin-top: .6rem; }
.row .txt { flex: 1; }
.primary { border: none; border-radius: .6rem; padding: .55rem .9rem; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; font-size: .82rem; cursor: pointer; }
.primary:hover { filter: brightness(1.07); }
.hint { font-size: .72rem; color: var(--faint); margin-top: .5rem; }
.task, .approval { padding: .6rem .7rem; border: 1px solid var(--border); border-radius: .6rem; margin-bottom: .5rem; background: var(--bg2); }
.task.pending { border-left: 3px solid var(--warn); }
.task.completed { border-left: 3px solid var(--ok); }
.task.running { border-left: 3px solid var(--accent); }
.task.waiting_approval, .task.failed { border-left: 3px solid var(--bad); }
.task-id { font-size: .7rem; color: var(--faint); }
.task-goal { font-size: .86rem; margin: .15rem 0; }
.task-status { font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.approval .primary { margin-top: .4rem; padding: .35rem .7rem; font-size: .76rem; }
.approval-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .45rem; }
.approval .primary { margin-top: 0; }
.approval .deny {
  margin-top: 0; padding: .35rem .7rem; font-size: .76rem; cursor: pointer;
  border-radius: .45rem; border: 1px solid rgba(255,90,95,.35);
  background: rgba(255,90,95,.12); color: var(--bad);
}
.approval .deny:hover { background: rgba(255,90,95,.22); }
#tickErrors {
  max-width: 22rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: .72rem; padding: .28rem .6rem; border-radius: 999px;
  background: rgba(255,90,95,.12); color: var(--bad); border: 1px solid rgba(255,90,95,.3);
  cursor: help;
}
#tickErrors[hidden] { display: none !important; }
pre.logs { white-space: pre-wrap; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .74rem; color: var(--faint); max-height: 11rem; overflow-y: auto; margin: 0; }
.empty { color: var(--faint); font-size: .85rem; padding: .15rem 0; }
/* Visible keyboard focus inside modal overlays (a11y). */
[role="dialog"] :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
/* Loading skeletons — a calmer first paint than bare "Loading…" text. */
.skel { display: flex; flex-direction: column; gap: .45rem; padding: .25rem 0; }
.skel span { height: .7rem; border-radius: .4rem; background: linear-gradient(90deg, var(--bg2) 25%, var(--border) 37%, var(--bg2) 63%); background-size: 400% 100%; animation: pxshim 1.4s ease infinite; }
.skel span:nth-child(2) { width: 78%; }
.skel span:nth-child(3) { width: 56%; }
@keyframes pxshim { 0% { background-position: 100% 0; } 100% { background-position: -100% 0; } }
@media (prefers-reduced-motion: reduce) { .skel span, .msg, .step, .typing .d, .mic-btn.recording, .pill.conn-reconnecting .dot { animation: none; } }
/* First-run call-to-action when running the offline mock model. */
.cta { margin-top: .5rem; padding: .5rem .6rem; font-size: .76rem; line-height: 1.4; color: var(--text); background: var(--bg2); border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: .5rem; }
.cta code { background: var(--bg); padding: .05rem .3rem; border-radius: .3rem; font-size: .9em; }

/* file upload */
.dropzone { border: 1.5px dashed var(--border); border-radius: .7rem; padding: 1.1rem .8rem; text-align: center; cursor: pointer; transition: all .15s ease; background: var(--bg); }
.dropzone:hover, .dropzone:focus-visible { border-color: var(--accent); outline: none; }
.dropzone.dragover { border-color: var(--accent); background: rgba(91,141,239,.08); box-shadow: 0 0 0 3px rgba(91,141,239,.15); }
.dz-icon { font-size: 1.35rem; line-height: 1; color: var(--accent); }
.dz-text { font-size: .85rem; margin-top: .3rem; color: var(--text); }
.dz-link { color: var(--accent); text-decoration: underline; }
.dz-sub { font-size: .72rem; color: var(--faint); margin-top: .2rem; }
#uploads { margin-top: .6rem; }
.uprow { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem; font-size: .8rem; padding: .45rem .55rem; border: 1px solid var(--border); border-radius: .5rem; margin-top: .5rem; background: var(--bg2); }
.uprow .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uprow .sz { color: var(--faint); font-size: .72rem; }
.uprow .st { font-size: .72rem; min-width: 2.6rem; text-align: right; }
.uprow .st.ok { color: var(--ok); } .uprow .st.bad { color: var(--bad); } .uprow .st.pending { color: var(--muted); }
.upbar { flex-basis: 100%; height: 3px; border-radius: 2px; background: var(--border); overflow: hidden; }
.upbar > i { display: block; height: 100%; width: 0; background: linear-gradient(135deg, var(--accent), var(--accent2)); transition: width .2s ease; }

#toast { position: fixed; bottom: 1.25rem; left: 50%; transform: translateX(-50%) translateY(2rem); background: var(--panel2); border: 1px solid var(--border); color: var(--text); padding: .6rem 1rem; border-radius: .7rem; box-shadow: var(--shadow); font-size: .85rem; opacity: 0; transition: all .25s ease; pointer-events: none; z-index: 50; }
#toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
</style>
<link rel="stylesheet" href="/web/run-graph.css" />
<link rel="stylesheet" href="/web/board.css" />
<link rel="stylesheet" href="/web/safety.css" />
<link rel="stylesheet" href="/web/metrics.css" />
<link rel="stylesheet" href="/web/inference.css" />
<link rel="stylesheet" href="/web/memory.css" />
<link rel="stylesheet" href="/web/knowledge.css" />
<link rel="stylesheet" href="/web/palette.css" />
<link rel="stylesheet" href="/web/settings.css" />
<link rel="stylesheet" href="/web/onboard.css" />
<link rel="stylesheet" href="/web/friendliness.css" />
<link rel="stylesheet" href="/web/shell.css" />
<link rel="stylesheet" href="/web/cron.css" />
<link rel="stylesheet" href="/web/home.css" />
<link rel="stylesheet" href="/web/growth.css" />
<link rel="manifest" href="/web/manifest.webmanifest" />
<meta name="theme-color" content="#5b8def" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<script>
/* Shared SSE bus: ONE EventSource for the whole dashboard. Six panels each
 * opening their own /events stream saturated the browser's 6-connection-per-host
 * limit and starved every /api fetch (panels stuck on "Loading…"). A single
 * stream fanned out to per-event subscribers keeps connections free for fetches. */
window.PraxisBus = (function () {
  var src = null, handlers = {}, bound = {};
  var statusFns = [], state = "connecting";
  function setState(s) {
    if (s === state) return;
    state = s;
    for (var i = 0; i < statusFns.length; i++) { try { statusFns[i](state); } catch (_) {} }
  }
  function bind(type) {
    if (!src || bound[type]) return;
    bound[type] = true;
    src.addEventListener(type, function (e) {
      var hs = handlers[type] || [];
      for (var i = 0; i < hs.length; i++) { try { hs[i](e); } catch (_) {} }
    });
  }
  function ensure() {
    if (src || typeof EventSource === "undefined") return;
    try { src = new EventSource("/events"); } catch (_) { src = null; setState("offline"); return; }
    src.onopen = function () { setState("live"); };
    src.onerror = function () { setState(src && src.readyState === 2 ? "offline" : "reconnecting"); };
    Object.keys(handlers).forEach(bind);
  }
  return {
    on: function (type, fn) {
      (handlers[type] = handlers[type] || []).push(fn);
      ensure();
      bind(type);
    },
    onStatus: function (fn) {
      statusFns.push(fn);
      ensure();
      try { fn(state); } catch (_) {}
    },
    state: function () { return state; }
  };
})();

/* Transient toast notifications (errors + connection changes). */
window.PraxisToast = function (msg, kind) {
  var box = document.getElementById("toasts");
  if (!box) return;
  var t = document.createElement("div");
  t.className = "toast " + (kind || "info");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(function () { t.classList.add("show"); }, 10);
  setTimeout(function () {
    t.classList.remove("show");
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
  }, 4200);
};

/* Inline panel error: replace a still-loading mount with a retry prompt so a
 * failed fetch never leaves a panel frozen on "Loading…". Real content is kept. */
window.PraxisPanelError = function (mount, label, retry) {
  if (!mount || mount.querySelector(".px-err")) return;
  var txt = (mount.textContent || "").trim();
  if (txt && txt.indexOf("Loading") === -1) return;
  mount.innerHTML = '<div class="px-err">\u26a0 Couldn\u2019t load ' +
    (label || "this panel") + '. <button type="button" class="px-retry">Retry</button></div>';
  var b = mount.querySelector(".px-retry");
  if (b) b.onclick = function () { mount.innerHTML = '<div class="skel" aria-hidden="true"><span></span><span></span><span></span></div>'; if (retry) retry(); };
};

/* Connection-status pill in the header + toasts on drop/recover. */
document.addEventListener("DOMContentLoaded", function () {
  var pill = document.getElementById("connPill");
  var txt = document.getElementById("connText");
  var labels = { live: "live", reconnecting: "reconnecting\u2026", offline: "offline", connecting: "connecting\u2026" };
  var wasDown = false;
  window.PraxisBus.onStatus(function (s) {
    if (pill) pill.className = "pill conn conn-" + s;
    if (txt) txt.textContent = labels[s] || s;
    if (s === "reconnecting" || s === "offline") {
      if (!wasDown) window.PraxisToast("Live updates lost \u2014 reconnecting\u2026", "warn");
      wasDown = true;
    } else if (s === "live") {
      if (wasDown) window.PraxisToast("Reconnected", "ok");
      wasDown = false;
    }
  });
});

/* A11y: Escape closes any open panel overlay (they share the `*-overlay.show`
 * pattern and a close() that only toggles `show`); a MutationObserver tags each
 * overlay as a modal dialog for screen readers as it is created. */
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  var open = document.querySelectorAll(
    ".wb-overlay.show,.if-overlay.show,.mem-overlay.show,.mx-overlay.show,.rg-overlay.show,.sf-overlay.show");
  for (var i = 0; i < open.length; i++) open[i].classList.remove("show");
});
(function () {
  var lastFocused = null;
  var OPEN_SEL = ".wb-overlay.show,.if-overlay.show,.mem-overlay.show," +
    ".mx-overlay.show,.rg-overlay.show,.sf-overlay.show";

  function focusables(root) {
    var list = root.querySelectorAll(
      "a[href],button:not([disabled]),input:not([disabled])," +
      "select:not([disabled]),textarea:not([disabled])," +
      "[tabindex]:not([tabindex='-1'])");
    return Array.prototype.slice.call(list).filter(function (el) {
      return el.offsetParent !== null;        // visible only
    });
  }
  function onShow(ov) {
    lastFocused = document.activeElement;
    if (ov.contains(lastFocused)) return;      // already managing its own focus
    // Focus the persistent box node (not a child control): panels re-render the
    // box's innerHTML on live updates, which would blow away focus on a child.
    var box = ov.querySelector("[class$='-box']") || ov;
    if (!box.hasAttribute("tabindex")) box.setAttribute("tabindex", "-1");
    box.focus();
  }
  function onHide() {
    if (lastFocused && typeof lastFocused.focus === "function") lastFocused.focus();
    lastFocused = null;
  }
  function watch(ov) {
    var was = ov.classList.contains("show");
    new MutationObserver(function () {
      var now = ov.classList.contains("show");
      if (now === was) return;                 // ignore non-toggling class churn
      was = now;
      if (now) onShow(ov); else onHide();
    }).observe(ov, { attributes: true, attributeFilter: ["class"] });
  }
  function tag(n) {
    if (n && n.nodeType === 1 && /(^|\s)[a-z]+-overlay(\s|$)/.test(n.className || "")) {
      n.setAttribute("role", "dialog");
      n.setAttribute("aria-modal", "true");
      watch(n);
    }
  }
  function start() {
    if (!document.body) return;
    try {
      var ov = document.body.querySelectorAll("[class$='-overlay'],[class*='-overlay ']");
      for (var k = 0; k < ov.length; k++) tag(ov[k]);
      var obs = new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var added = muts[i].addedNodes || [];
          for (var j = 0; j < added.length; j++) tag(added[j]);
        }
      });
      obs.observe(document.body, { childList: true });
    } catch (_) {}
  }
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);

  // Focus-trap: keep Tab focus inside whichever panel overlay is open.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Tab") return;
    var ov = document.querySelector(OPEN_SEL);
    if (!ov) return;
    var f = focusables(ov);
    if (!f.length) { e.preventDefault(); return; }
    var first = f[0], last = f[f.length - 1], a = document.activeElement;
    if (e.shiftKey && (a === first || !ov.contains(a))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (a === last || !ov.contains(a))) { e.preventDefault(); first.focus(); }
  });
})();
</script>
<script src="/web/run-graph.js" defer></script>
<script src="/web/board.js" defer></script>
<script src="/web/safety.js" defer></script>
<script src="/web/metrics.js" defer></script>
<script src="/web/inference.js" defer></script>
<script src="/web/memory.js" defer></script>
<script src="/web/knowledge.js" defer></script>
<script src="/web/palette.js" defer></script>
<script src="/web/settings.js" defer></script>
<script src="/web/onboard.js" defer></script>
<script src="/web/friendliness.js" defer></script>
<script src="/web/shell.js" defer></script>
<script src="/web/cron.js" defer></script>
<script src="/web/home.js" defer></script>
<script src="/web/growth.js" defer></script>
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/web/sw.js').catch(function () {});
  });
}
</script>
</head>
<body>
<div id="toasts" class="toasts" aria-live="polite"></div>
<header>
  <div class="brand"><span class="logo"></span> Praxis</div>
  <span class="pill modelpill"><span class="dot"></span><span id="modelBadge">—</span></span>
  <span class="spacer"></span>
  <button id="apprBadge" type="button" hidden title="Jump to approvals"></button>
  <button id="cmdk" class="badge" type="button" title="Command palette (Ctrl/Cmd+K)">⌘K</button>
  <button id="settingsBtn" class="badge" type="button" title="Settings">⚙</button>
  <span id="connPill" class="pill conn conn-connecting" title="Live update stream"><span class="dot"></span><span id="connText">connecting…</span></span>
  <span id="status" class="badge">checking…</span>
  <span id="tickErrors" class="badge bad" hidden title=""></span>
</header>
<div id="healthBanner" role="status" aria-live="polite"></div>

<main>
  <nav id="historyRail" class="hist panel">
    <div class="hist-head">
      <h2>Chats</h2>
      <button class="newchat" type="button" onclick="newChat()" title="Start a new chat">＋ New</button>
    </div>
    <div id="histList" class="hist-list"></div>
  </nav>

  <section id="chat" class="panel">
    <div class="chat-top">
      <div class="mode-bar">
        <button type="button" id="modeAuto" class="mode-auto" onclick="setMode('auto')" title="Auto-route from your message">Auto</button>
        <div class="mode-more-wrap">
          <button type="button" id="modeMore" class="mode-more" onclick="toggleModeMenu()" aria-haspopup="true" aria-expanded="false">More ▾</button>
          <div id="modeMenu" class="mode-menu" role="menu">
            <div class="mm-hint">Force a specific mode</div>
            <button type="button" data-mode="chat" role="menuitem" onclick="setMode('chat')">Chat</button>
            <button type="button" data-mode="ask" role="menuitem" onclick="setMode('ask')">Look up</button>
            <button type="button" data-mode="research" role="menuitem" onclick="setMode('research')">Research</button>
            <button type="button" data-mode="do" role="menuitem" onclick="setMode('do')">Work on this</button>
            <button type="button" data-mode="agent" role="menuitem" onclick="setMode('agent')">Tools</button>
          </div>
        </div>
        <span class="hint" id="modeHint">Auto — Praxis picks Look up / Research / Work on this from your message.</span>
      </div>
      <button class="ghost" onclick="newChat()" title="Start a new chat">New chat</button>
    </div>
    <div id="messages" class="messages"></div>
    <form class="composer" onsubmit="sendMessage(event)">
      <textarea id="message" rows="1" placeholder="Message Praxis… Auto routes Look up, Research, and Work on this." autocomplete="off"></textarea>
      <button class="mic-btn" id="mic" type="button" title="Push to talk" onclick="toggleMic()" hidden>🎙</button>
      <button class="send-btn" id="send" type="submit" title="Send">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
      </button>
    </form>
    <div id="intentChip" aria-live="polite"></div>
  </section>

  <aside id="deckRail" class="deck-rail" aria-label="Command deck panels">
    <div class="rail-primary">
      <div class="rail-block compact">
        <h2>Model</h2>
        <div class="field">
          <label>Provider</label>
          <select id="prov"></select>
        </div>
        <div class="row">
          <input id="modelInput" class="txt" list="modelList" placeholder="model id" />
          <datalist id="modelList"></datalist>
          <button class="primary" onclick="applyModel()">Use</button>
        </div>
        <div class="hint" id="keyHint"></div>
        <div class="cta" id="firstRunCta" hidden>⚡ You're on the offline <strong>mock</strong> model. <button type="button" class="cta-btn" onclick="if(window.PraxisOnboard)PraxisOnboard.open()">Set up Praxis</button> to connect a live model.</div>
      </div>
      <div class="rail-block">
        <h2>Approvals</h2>
        <div id="approvals"><div class="empty">Nothing waiting approval.</div></div>
      </div>
    </div>

    <div class="rail-tabs" role="tablist" aria-label="Deck sections">
      <button type="button" role="tab" data-rail="ops" class="active" aria-selected="true">Ops<span class="rail-dot" aria-hidden="true"></span></button>
      <button type="button" role="tab" data-rail="work" aria-selected="false">Work<span class="rail-dot" aria-hidden="true"></span></button>
      <button type="button" role="tab" data-rail="mind" aria-selected="false">Mind<span class="rail-dot" aria-hidden="true"></span></button>
      <button type="button" role="tab" data-rail="more" aria-selected="false">More<span class="rail-dot" aria-hidden="true"></span></button>
    </div>

    <div class="rail-body">
      <div class="rail-pane active" data-pane="ops" role="tabpanel">
        <div class="rail-section">
          <h2>Queue</h2>
          <div id="tasks"><div class="empty">No tasks yet.</div></div>
        </div>
        <div class="rail-section">
          <h2>Schedule</h2>
          <div id="cron-list"><div class="empty">Loading schedules…</div></div>
          <form id="cronForm">
            <input id="cronGoal" class="txt" placeholder="Goal (e.g. draft morning status note)" autocomplete="off" />
            <div class="row">
              <input id="cronSched" class="txt" placeholder="Schedule (0 9 * * 1-5 or daily@09:00)" value="0 9 * * 1-5" />
              <button class="primary" type="submit">Add</button>
            </div>
          </form>
        </div>
        <div class="rail-section">
          <h2>Files</h2>
          <div id="drop" class="dropzone" tabindex="0" role="button" aria-label="Upload files">
            <input id="fileInput" type="file" multiple hidden />
            <div class="dz-icon">⬆</div>
            <div class="dz-text">Drop files here or <span class="dz-link">browse</span></div>
            <div class="dz-sub">Saved to the agent's work directory</div>
          </div>
          <div id="uploads"></div>
        </div>
        <div class="rail-section">
          <h2>Voice</h2>
          <div class="vmodes" id="vmodes"></div>
          <div class="hint" id="voiceHint">Voice is off.</div>
        </div>
      </div>

      <div class="rail-pane" data-pane="work" role="tabpanel">
        <div class="rail-section">
          <h2>Run Graph</h2>
          <div id="runlist"><div class="empty">No runs yet.</div></div>
        </div>
        <div class="rail-section">
          <h2>Work Board</h2>
          <div id="board-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
      </div>

      <div class="rail-pane" data-pane="mind" role="tabpanel">
        <div class="rail-section">
          <h2>You</h2>
          <div id="growth-model"><div class="empty">Loading persona…</div></div>
        </div>
        <div class="rail-section">
          <h2>Skills</h2>
          <div id="growth-skills"><div class="empty">Loading skills…</div></div>
        </div>
        <div class="rail-section">
          <h2>Evolution inbox</h2>
          <div id="growth-evolve"><div class="empty">No proposals yet.</div></div>
        </div>
        <div class="rail-section">
          <h2>Agent rooms</h2>
          <div id="growth-rooms"><div class="empty">Loading rooms…</div></div>
        </div>
        <div class="rail-section">
          <h2>Computer use</h2>
          <div id="browser-snap"><div class="empty">No page loaded yet.</div></div>
        </div>
        <div class="rail-section">
          <h2>Memory</h2>
          <div id="memory-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
        <div class="rail-section">
          <h2>Knowledge</h2>
          <div id="knowledge-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
      </div>

      <div class="rail-pane" data-pane="more" role="tabpanel">
        <div class="rail-section">
          <h2>Safety Center</h2>
          <div id="safety-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
        <div class="rail-section">
          <h2>Inference</h2>
          <div id="inference-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
        <div class="rail-section">
          <h2>Metrics</h2>
          <div id="metrics-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
        </div>
        <div class="rail-section">
          <h2>Activity log</h2>
          <pre id="logs" class="logs">—</pre>
        </div>
      </div>
    </div>
  </aside>
</main>
<div id="toast" role="status" aria-live="polite"></div>

<script>
let mode = 'auto';
let isSending = false;
let conversations = [];
let activeId = null;
const HIST_KEY = 'praxis.chats.v1';
try { if(!sessionStorage.getItem('praxis.ttft.t0')) sessionStorage.setItem('praxis.ttft.t0', String(Date.now())); } catch(_){}
let providers = [];
const HINTS = {
  auto: 'Auto — Praxis picks Look up / Research / Work on this from your message.',
  chat: 'Conversational chat — Praxis can call tools such as fetch_url when helpful.',
  ask: 'Grounded Q&A over the knowledge base — cites sources or abstains.',
  research: 'Live web research — searches the internet, reads results, and answers with citations.',
  do: 'Queue an autonomous task for the agent to work.',
  agent: 'Agentic tools — Praxis calls tools through the governance broker (read/draft run; send/destructive need approval).'
};
const messagesEl = document.getElementById('messages');

function api(path, opts) { return fetch(path, opts).then(r => r.json().catch(() => ({}))); }

/* ---------- markdown ---------- */
let _codeBlocks = [];
function escapeHtml(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function inline(s){
  s = s.replace(/`([^`]+)`/g, (m,c)=>'<code>'+c+'</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s = s.replace(/__([^_]+)__/g,'<strong>$1</strong>');
  s = s.replace(/(^|[^\*])\*([^*]+)\*/g,'$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function renderCodeBlock(i){
  const code = _codeBlocks[i]; if(code === undefined) return '';
  return '<pre class="code"><button class="copy" type="button" onclick="copyCode(this)">Copy</button><code>'+escapeHtml(code)+'</code></pre>';
}
function renderBlocks(text){
  const lines = text.split('\n'); let html=''; let i=0;
  const isSpecial = l => /^@@CB\d+@@$/.test(l) || /^(#{1,6})\s+/.test(l) || /^\s*>\s?/.test(l) || /^\s*[-*+]\s+/.test(l) || /^\s*\d+\.\s+/.test(l) || /^\s*$/.test(l);
  while(i < lines.length){
    const line = lines[i];
    let m;
    if(m = line.match(/^@@CB(\d+)@@$/)){ html += renderCodeBlock(+m[1]); i++; continue; }
    if(m = line.match(/^(#{1,6})\s+(.*)$/)){ const lvl=m[1].length; html += '<h'+lvl+'>'+inline(m[2])+'</h'+lvl+'>'; i++; continue; }
    if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)){ html += '<hr>'; i++; continue; }
    if(/^\s*>\s?/.test(line)){ const buf=[]; while(i<lines.length && /^\s*>\s?/.test(lines[i])){ buf.push(lines[i].replace(/^\s*>\s?/,'')); i++; } html += '<blockquote>'+renderBlocks(buf.join('\n'))+'</blockquote>'; continue; }
    if(/^\s*[-*+]\s+/.test(line)){ const it=[]; while(i<lines.length && /^\s*[-*+]\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*[-*+]\s+/,''))+'</li>'); i++; } html += '<ul>'+it.join('')+'</ul>'; continue; }
    if(/^\s*\d+\.\s+/.test(line)){ const it=[]; while(i<lines.length && /^\s*\d+\.\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*\d+\.\s+/,''))+'</li>'); i++; } html += '<ol>'+it.join('')+'</ol>'; continue; }
    if(/^\s*$/.test(line)){ i++; continue; }
    const para=[]; while(i<lines.length && !isSpecial(lines[i])){ para.push(lines[i]); i++; }
    html += '<p>'+inline(para.join('\n')).replace(/\n/g,'<br>')+'</p>';
  }
  return html;
}
function renderMarkdown(src){
  _codeBlocks = [];
  src = (src||'').replace(/\r\n/g,'\n');
  src = src.replace(/```[\w-]*\n?([\s\S]*?)```/g, (m,code)=>{ const idx=_codeBlocks.length; _codeBlocks.push(code.replace(/\n$/,'')); return '\n@@CB'+idx+'@@\n'; });
  src = escapeHtml(src);
  return renderBlocks(src);
}
function copyCode(btn){
  const code = btn.parentElement.querySelector('code');
  navigator.clipboard.writeText(code.innerText).then(()=>{ btn.textContent='Copied'; setTimeout(()=>btn.textContent='Copy',1200); });
}

/* ---------- messages ---------- */
function clearWelcome(){ const w = messagesEl.querySelector('.welcome'); if(w) w.remove(); }
function fmtTime(ts){ const d = ts ? new Date(ts) : new Date(); return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
function appendUser(text, ts){
  clearWelcome();
  const row = document.createElement('div'); row.className = 'msg user';
  row.innerHTML = '<div class="avatar">You</div><div class="bubble-wrap"><div class="bubble"></div><div class="meta">'+fmtTime(ts)+'</div></div>';
  row.querySelector('.bubble').textContent = text;
  messagesEl.appendChild(row); scrollDown();
}
function appendAgent(text, meta, ts){
  clearWelcome();
  const row = document.createElement('div'); row.className = 'msg agent';
  row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="bubble"></div><div class="meta"></div></div>';
  row.querySelector('.bubble').innerHTML = renderMarkdown(text);
  row.querySelector('.meta').textContent = (meta ? meta+' · ' : '') + fmtTime(ts);
  messagesEl.appendChild(row); scrollDown();
}
function appendTyping(){
  clearWelcome();
  const row = document.createElement('div'); row.className = 'msg agent typing';
  row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="bubble"><span class="d"></span><span class="d"></span><span class="d"></span></div></div>';
  messagesEl.appendChild(row); scrollDown(); return row;
}
function scrollDown(){ messagesEl.scrollTop = messagesEl.scrollHeight; }

function setMode(m){
  mode = m || 'auto';
  const autoBtn = document.getElementById('modeAuto');
  if(autoBtn) autoBtn.classList.toggle('inactive', mode !== 'auto');
  const menu = document.getElementById('modeMenu');
  if(menu){
    menu.querySelectorAll('button[data-mode]').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-mode') === mode);
    });
    menu.classList.remove('show');
  }
  const more = document.getElementById('modeMore');
  if(more){
    more.classList.remove('open');
    more.setAttribute('aria-expanded', 'false');
  }
  const hint = document.getElementById('modeHint');
  if(hint) hint.textContent = HINTS[mode] || HINTS.auto;
  const ph = {
    auto: 'Message Praxis… Auto routes Look up, Research, and Work on this.',
    do: 'Describe a goal to queue…',
    ask: 'Ask a grounded question…',
    research: 'Ask anything — Praxis will search the web…',
    agent: 'Ask Praxis to do something — it can call tools…',
    chat: 'Chat with Praxis — it can call tools when helpful.'
  };
  const ta = document.getElementById('message');
  if(ta) ta.placeholder = ph[mode] || ph.auto;
  updateIntentChip();
}
function toggleModeMenu(){
  const menu = document.getElementById('modeMenu');
  const more = document.getElementById('modeMore');
  if(!menu) return;
  const open = !menu.classList.contains('show');
  menu.classList.toggle('show', open);
  if(more){
    more.classList.toggle('open', open);
    more.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
}
document.addEventListener('click', function(e){
  const wrap = document.querySelector('.mode-more-wrap');
  if(wrap && !wrap.contains(e.target)){
    const menu = document.getElementById('modeMenu');
    const more = document.getElementById('modeMore');
    if(menu) menu.classList.remove('show');
    if(more){ more.classList.remove('open'); more.setAttribute('aria-expanded', 'false'); }
  }
});
function updateIntentChip(){
  const el = document.getElementById('intentChip');
  if(!el) return;
  const ta = document.getElementById('message');
  const text = (ta && ta.value) || '';
  const labels = (window.PraxisIntent && window.PraxisIntent.labels) || {
    auto:'Auto', chat:'Chat', ask:'Look up', research:'Research', do:'Work on this', agent:'Tools'
  };
  if(mode !== 'auto'){
    el.innerHTML = 'Mode: <b>' + escapeHtml(labels[mode] || mode) + '</b>';
    return;
  }
  if(!text.trim()){ el.innerHTML = ''; return; }
  const resolved = (window.PraxisIntent && window.PraxisIntent.detect)
    ? window.PraxisIntent.detect(text)
    : 'chat';
  el.innerHTML = 'Will use <b>' + escapeHtml(labels[resolved] || resolved) + '</b>';
}
function resolveSendMode(text){
  if(window.PraxisIntent && window.PraxisIntent.resolveMode){
    return window.PraxisIntent.resolveMode(mode, text);
  }
  return mode === 'auto' ? 'chat' : mode;
}
function newChat(){
  activeId = null;
  messagesEl.innerHTML = '';
  showWelcome();
  renderHistList();
  const ta = document.getElementById('message'); if(ta) ta.focus();
}
window.setMode = setMode;
window.updateIntentChip = updateIntentChip;

/* ---------- chat history (left rail) ---------- */
const HIST_MAX = 50;
function uid(){ return Date.now().toString(36) + Math.random().toString(36).slice(2,7); }
function loadConversations(){
  try { conversations = JSON.parse(localStorage.getItem(HIST_KEY)) || []; }
  catch(_){ conversations = []; }
  if(!Array.isArray(conversations)) conversations = [];
}
function pruneConversations(){
  if(conversations.length <= HIST_MAX) return;
  const keep = new Set([activeId]);
  [...conversations].sort((a,b)=>b.updated-a.updated).slice(0, HIST_MAX).forEach(c=>keep.add(c.id));
  conversations = conversations.filter(c=>keep.has(c.id));
}
function persistConversations(){
  pruneConversations();
  try { localStorage.setItem(HIST_KEY, JSON.stringify(conversations)); } catch(_){}
}
function ensureConversation(){
  let c = conversations.find(x=>x.id===activeId);
  if(!c){
    c = { id: uid(), title: '', created: Date.now(), updated: Date.now(), messages: [] };
    conversations.push(c); activeId = c.id;
  }
  return c;
}
function convTitle(c){
  if(c.title) return c.title;
  const u = (c.messages||[]).find(m=>m.role==='user');
  return u ? u.content.replace(/\s+/g,' ').trim().slice(0,60) : 'New chat';
}
function relTime(ts){
  const d = (Date.now()-ts)/1000;
  if(d < 60) return 'just now';
  if(d < 3600) return Math.floor(d/60)+'m ago';
  if(d < 86400) return Math.floor(d/3600)+'h ago';
  return new Date(ts).toLocaleDateString([], {month:'short', day:'numeric'});
}
function renderHistList(){
  const el = document.getElementById('histList'); if(!el) return;
  if(!conversations.length){ el.innerHTML = '<div class="hist-empty">No conversations yet.</div>'; return; }
  el.innerHTML = '';
  [...conversations].sort((a,b)=>b.updated-a.updated).forEach(c=>{
    const item = document.createElement('div');
    item.className = 'hist-item' + (c.id===activeId ? ' active' : '');
    item.onclick = () => switchConversation(c.id);
    const t = document.createElement('div'); t.className = 'ht';
    const title = document.createElement('div'); title.className = 'title';
    title.textContent = convTitle(c); title.title = convTitle(c);
    const when = document.createElement('div'); when.className = 'when'; when.textContent = relTime(c.updated);
    t.append(title, when);
    const del = document.createElement('button'); del.className = 'del'; del.type = 'button';
    del.title = 'Delete chat'; del.setAttribute('aria-label', 'Delete chat'); del.textContent = '✕';
    del.onclick = (e) => { e.stopPropagation(); deleteConversation(c.id); };
    item.append(t, del);
    el.appendChild(item);
  });
}
function switchConversation(id){
  const c = conversations.find(x=>x.id===id); if(!c) return;
  activeId = id;
  messagesEl.innerHTML = '';
  if(c.messages.length){
    c.messages.forEach(m => m.role==='user'
      ? appendUser(m.content, m.ts)
      : appendAgent(m.content, m.model || '', m.ts));
  } else { showWelcome(); }
  renderHistList();
}
function deleteConversation(id){
  conversations = conversations.filter(c=>c.id!==id);
  persistConversations();
  if(activeId === id){
    const next = [...conversations].sort((a,b)=>b.updated-a.updated)[0];
    if(next) switchConversation(next.id); else newChat();
  } else {
    renderHistList();
  }
}
function showWelcome(){
  messagesEl.innerHTML = '<div class="welcome"><h3>Talk to Praxis</h3><p>Chat with your configured model, ask grounded questions, or queue autonomous tasks. Switch models any time from the panel on the right.</p><div class="chips">'
    + ['Explain the governance broker','Draft a customer follow-up email','Summarize my open tasks'].map(c=>'<button class="chip" onclick="useChip(this)">'+c+'</button>').join('')
    + '</div></div>';
}
function useChip(el){ const ta = document.getElementById('message'); ta.value = el.textContent; ta.focus(); autoGrow(ta); }

function setBusy(b){ document.getElementById('send').disabled = b; }

async function streamChat(conv, wire, typing){
  let acc = '', model = '', bubbleEl = null, metaEl = null, raf = false, finished = false;
  function ensureBubble(){
    if(bubbleEl) return;
    if(typing){ typing.remove(); typing = null; }
    const row = document.createElement('div'); row.className = 'msg agent';
    row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="bubble"></div><div class="meta"></div></div>';
    messagesEl.appendChild(row);
    bubbleEl = row.querySelector('.bubble'); metaEl = row.querySelector('.meta');
  }
  function flush(){ ensureBubble(); bubbleEl.innerHTML = renderMarkdown(acc || ''); metaEl.textContent = (model ? model+' · ' : '') + fmtTime(); scrollDown(); }
  function paint(){ if(raf) return; raf = true; requestAnimationFrame(()=>{ raf = false; flush(); }); }
  try {
    const resp = await fetch('/api/chat/stream', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({messages: wire})});
    if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buf = '';
    while(!finished){
      const {value, done} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) !== -1){
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const dline = frame.split('\n').find(l => l.startsWith('data:'));
        if(!dline) continue;
        let ev; try { ev = JSON.parse(dline.slice(5).trim()); } catch(_){ continue; }
        if(ev.type === 'meta'){ model = ev.model || ''; }
        else if(ev.type === 'delta'){ acc += ev.text || ''; paint(); }
        else if(ev.type === 'error'){ acc += (acc ? '\n\n' : '') + '⚠️ ' + (ev.error || 'stream error'); paint(); }
        else if(ev.type === 'done'){ finished = true; break; }
      }
    }
  } catch(e){ acc = acc || ('Error: ' + e); }
  if(!acc) acc = 'No response.';
  flush();
  conv.messages.push({role:'assistant', content: acc, model: model, ts: Date.now()});
  conv.updated = Date.now(); persistConversations(); renderHistList();
  speak(acc);
}

async function agentChat(conv, wire, typing){
  let model = '', steps = null, finalText = '', finished = false;
  const cards = {};
  let _stepSeq = 0;
  function cardKey(ev){
    // Prefer approval_id / tool call identity so concurrent same-name tools
    // don't overwrite each other's cards.
    return ev.approval_id || (ev.tool + '#' + (_stepSeq++));
  }
  function ensureSteps(){
    if(steps) return;
    if(typing){ typing.remove(); typing = null; }
    const row = document.createElement('div'); row.className = 'msg agent';
    row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="steps"></div></div>';
    messagesEl.appendChild(row); steps = row.querySelector('.steps');
  }
  function addStep(html, cls){ ensureSteps(); const s = document.createElement('div'); s.className = 'step ' + (cls||''); s.innerHTML = html; steps.appendChild(s); scrollDown(); return s; }
  function setCard(tool, html, cls){ const c = cards[tool]; if(c){ c.className = 'step ' + cls; c.innerHTML = html; } else { cards[tool] = addStep(html, cls); } }
  try {
    const resp = await fetch('/api/chat/agent', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({messages: wire})});
    if(!resp.ok || !resp.body) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while(!finished){
      const {value, done} = await reader.read(); if(done) break;
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) !== -1){
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const dl = frame.split('\n').find(l => l.startsWith('data:')); if(!dl) continue;
        let ev; try { ev = JSON.parse(dl.slice(5).trim()); } catch(_){ continue; }
        if(ev.type === 'meta'){ model = ev.model || ''; }
        else if(ev.type === 'recall'){
          var mem = (ev.memory||[]).length, sk = (ev.skills||[]).length;
          if(mem || sk){
            var parts = [];
            if(mem) parts.push(mem + ' memor' + (mem===1?'y':'ies'));
            if(sk) parts.push(sk + ' skill' + (sk===1?'':'s'));
            var detail = (ev.memory||[]).map(function(m){return m.text;}).concat((ev.skills||[]).map(function(s){return 'skill: '+s.name;})).join(' · ');
            addStep('🧠 <b>Recalled</b> <span class="muted">'+escapeHtml(parts.join(', '))+'</span><div class="muted" style="margin-top:.2rem;font-size:.74rem">'+escapeHtml(detail.slice(0,260))+'</div>', 'ok');
          }
        }
        else if(ev.type === 'tool_call'){ var k = cardKey(ev); cards[ev.tool] = cards[k] = addStep('🔧 <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> <span class="muted">running…</span>', 'run'); if(window.PraxisPresence) window.PraxisPresence.thinking(ev.tool||'tool'); }
        else if(ev.type === 'tool_result'){ setCard(ev.tool, '✅ <b>'+escapeHtml(ev.tool)+'</b> <span class="muted">'+escapeHtml(ev.preview||'')+'</span>', 'ok'); }
        else if(ev.type === 'approval'){ setCard(ev.tool, '⏸ <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> held for your approval<div class="muted" style="margin-top:.25rem;font-size:.74rem">Nothing was sent yet — use the card below or Approvals: <b>A</b> once · <b>C</b> this chat · <b>D</b> deny.</div>', 'hold'); if(window.PraxisInlineApproval) window.PraxisInlineApproval.mount(ev); if(window.PraxisOutcome&&window.PraxisOutcome.attach){ window.PraxisOutcome.attach({title:'Action held', status:'held', ran:ev.tool||'tool', changed:'No external side effect yet', next:'Approve inline or in Approvals (A/C). Praxis resumes this chat.'}); } if(window.PraxisFriendly&&window.PraxisFriendly.markTour) window.PraxisFriendly.markTour('hold'); if(window.PraxisPresence) window.PraxisPresence.waiting(ev.tool||'approval'); refresh(); }
        else if(ev.type === 'denied'){ setCard(ev.tool||'tool', '⛔ <b>'+escapeHtml(ev.tool||'tool')+'</b> denied <span class="muted">'+escapeHtml(ev.reason||'')+'</span>', 'deny'); }
        else if(ev.type === 'final'){ finalText = ev.text || ''; }
        else if(ev.type === 'error'){ finalText = (finalText ? finalText+'\n\n' : '') + '⚠️ ' + (ev.error || 'error'); }
        else if(ev.type === 'done'){ finished = true; break; }
      }
    }
  } catch(e){ finalText = finalText || ('Error: ' + e); }
  if(typing){ typing.remove(); typing = null; }
  const out = finalText || '(no response)';
  appendAgent(out, model);
  conv.messages.push({role:'assistant', content: out, model: model, ts: Date.now()});
  conv.updated = Date.now(); persistConversations(); renderHistList();
  speak(out);
  if(window.PraxisPresence) window.PraxisPresence.idle('');
  if(window.PraxisFirstWin && window.PraxisFriendly){
    var tour = window.PraxisFriendly.loadTour();
    var n = Object.keys((tour&&tour.done)||{}).length;
    if(n >= 1) window.PraxisFirstWin.mark();
  }
  try {
    if(!sessionStorage.getItem('praxis.ttft.sent')){
      var t0 = Number(sessionStorage.getItem('praxis.ttft.t0')||0);
      if(t0){ fetch('/api/growth/ttft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seconds:(Date.now()-t0)/1000})}); sessionStorage.setItem('praxis.ttft.sent','1'); }
    }
  } catch(_){}
}

function playAudioB64(b64, mime){
  try {
    const bin = atob(b64); const arr = new Uint8Array(bin.length);
    for(let i=0;i<bin.length;i++) arr[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([arr], {type: mime || 'audio/wav'}));
    const a = new Audio(url); a.onended = () => URL.revokeObjectURL(url); _rtPlaying = a; a.play().catch(()=>{});
  } catch(_){}
}
let _rtPlaying = null;
function stopPlayback(){ try { if(_rtPlaying){ _rtPlaying.pause(); _rtPlaying = null; } } catch(_){} }
/* Persistent realtime session: one WebSocket for the whole conversation, with
   live PCM16 mic streaming (push-to-talk). The governed event protocol is shared
   with the loopback and OpenAI bridges, so the UI is identical either way. */
let _rtWs = null, _rtReady = false, _rtTurn = null, _rtPendingTyping = null;
let _rtAwait = null, _rtAwaitResolve = null, _rtAwaitTimer = null;
let _rtAudioCtx = null, _rtNode = null, _rtSource = null, _rtSink = null, _rtStream = null, _rtStreaming = false;

function rtSocketReady(){ return !!(_rtWs && _rtWs.readyState === 1 && _rtReady); }

function rtEnsureSocket(){
  return new Promise((resolve, reject) => {
    if(_rtWs && (_rtWs.readyState === 0 || _rtWs.readyState === 1)){
      if(_rtReady) return resolve();
      const t = setInterval(() => { if(_rtReady){ clearInterval(t); resolve(); } }, 40);
      setTimeout(() => { clearInterval(t); _rtReady ? resolve() : reject(new Error('realtime not ready')); }, 8000);
      return;
    }
    let ws;
    try { ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://') + location.host + '/api/voice/realtime'); }
    catch(e){ return reject(e); }
    _rtWs = ws; _rtReady = false;
    ws.onmessage = (e) => { let ev; try { ev = JSON.parse(e.data); } catch(_){ return; } rtOnEvent(ev, resolve); };
    ws.onerror = () => { if(_rtTurn) rtTurnError('Realtime connection failed.'); };
    ws.onclose = () => { _rtReady = false; _rtWs = null; if(_rtTurn) rtFinishTurn(); else rtResolveAwait(); };
    setTimeout(() => { if(!_rtReady && _rtWs === ws) reject(new Error('realtime handshake timeout')); }, 8000);
  });
}

function rtArmTurn(){
  _rtAwait = new Promise(res => { _rtAwaitResolve = res; });
  clearTimeout(_rtAwaitTimer);
  _rtAwaitTimer = setTimeout(() => { if(_rtTurn) rtFinishTurn(); else rtResolveAwait(); }, 30000);
}
function rtResolveAwait(){ clearTimeout(_rtAwaitTimer); if(_rtAwaitResolve){ const r = _rtAwaitResolve; _rtAwaitResolve = null; r(); } }

function rtEnsureTurn(){
  if(_rtTurn) return;
  _rtTurn = { steps: null, finalText: '', cards: {}, userShown: false, typing: _rtPendingTyping };
  _rtPendingTyping = null;
}
function rtAddStep(html, cls){
  const T = _rtTurn;
  if(!T.steps){ if(T.typing){ T.typing.remove(); T.typing = null; } const row = document.createElement('div'); row.className = 'msg agent'; row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="steps"></div></div>'; messagesEl.appendChild(row); T.steps = row.querySelector('.steps'); }
  const s = document.createElement('div'); s.className = 'step ' + (cls||''); s.innerHTML = html; T.steps.appendChild(s); scrollDown(); return s;
}
function rtSetCard(tool, html, cls){ const c = _rtTurn.cards[tool]; if(c){ c.className = 'step ' + cls; c.innerHTML = html; } else { _rtTurn.cards[tool] = rtAddStep(html, cls); } }
function rtSetUserTranscript(text){
  if(!text || _rtTurn.userShown) return;
  _rtTurn.userShown = true; appendUser(text);
  const conv = ensureConversation(); conv.messages.push({role:'user', content:text, ts: Date.now()});
  conv.updated = Date.now(); persistConversations(); renderHistList();
}
function rtTurnError(msg){ rtEnsureTurn(); _rtTurn.finalText = (_rtTurn.finalText?_rtTurn.finalText+'\n\n':'') + '⚠️ ' + msg; rtFinishTurn(); }
function rtFinishTurn(){
  const T = _rtTurn; if(!T){ rtResolveAwait(); return; } _rtTurn = null;
  if(T.typing){ T.typing.remove(); T.typing = null; }
  const out = T.finalText || '(no response)';
  appendAgent(out, 'realtime');
  const conv = ensureConversation(); conv.messages.push({role:'assistant', content: out, model:'realtime', ts: Date.now()});
  conv.updated = Date.now(); persistConversations(); renderHistList();
  setBusy(false); rtResolveAwait();
}

function rtOnEvent(ev, readyResolve){
  if(ev.type === 'ready'){ _rtReady = true; if(readyResolve) readyResolve(); return; }
  rtEnsureTurn();
  const T = _rtTurn;
  if(ev.type === 'transcript'){ rtSetUserTranscript(ev.text || ''); }
  else if(ev.type === 'delta'){ T.finalText += (ev.text || ''); }
  else if(ev.type === 'tool_call'){ T.cards[ev.tool] = rtAddStep('🔧 <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> <span class="muted">running…</span>', 'run'); }
  else if(ev.type === 'tool_result'){ rtSetCard(ev.tool, '✅ <b>'+escapeHtml(ev.tool)+'</b> <span class="muted">'+escapeHtml(ev.preview||'')+'</span>', 'ok'); }
  else if(ev.type === 'approval'){ rtSetCard(ev.tool, '⏸ <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> held for your approval', 'hold'); refresh(); }
  else if(ev.type === 'denied'){ rtSetCard(ev.tool||'tool', '⛔ <b>'+escapeHtml(ev.tool||'tool')+'</b> denied <span class="muted">'+escapeHtml(ev.reason||'')+'</span>', 'deny'); }
  else if(ev.type === 'final'){ T.finalText = ev.text || T.finalText; }
  else if(ev.type === 'audio'){ playAudioB64(ev.data, ev.mime); }
  else if(ev.type === 'interrupted'){ stopPlayback(); }
  else if(ev.type === 'error'){ T.finalText = (T.finalText?T.finalText+'\n\n':'') + '⚠️ ' + (ev.error || 'error'); }
  else if(ev.type === 'done'){ rtFinishTurn(); }
}

async function rtSendText(text, typing){
  _rtPendingTyping = typing || appendTyping(); setBusy(true);
  try { await rtEnsureSocket(); }
  catch(e){ if(_rtPendingTyping){ _rtPendingTyping.remove(); _rtPendingTyping = null; } appendAgent('Realtime error: '+e); setBusy(false); return; }
  rtArmTurn();
  _rtWs.send(JSON.stringify({type:'text', text: text}));
  _rtWs.send(JSON.stringify({type:'commit'}));
  await _rtAwait;
}

/* ---- PCM16 capture: downsample to mono 24kHz, little-endian, base64 ---- */
function rtDownsample(buffer, srcRate, dstRate){
  if(dstRate >= srcRate) return buffer;
  const ratio = srcRate/dstRate, newLen = Math.round(buffer.length/ratio), result = new Float32Array(newLen);
  let oR = 0, oB = 0;
  while(oR < newLen){ const next = Math.round((oR+1)*ratio); let acc = 0, cnt = 0; for(let i=oB;i<next && i<buffer.length;i++){ acc += buffer[i]; cnt++; } result[oR] = cnt ? acc/cnt : 0; oR++; oB = next; }
  return result;
}
function rtPcm16Base64(float32, srcRate){
  const ds = rtDownsample(float32, srcRate, 24000);
  const buf = new ArrayBuffer(ds.length*2), view = new DataView(buf);
  for(let i=0;i<ds.length;i++){ let s = Math.max(-1, Math.min(1, ds[i])); view.setInt16(i*2, s<0 ? s*0x8000 : s*0x7FFF, true); }
  const bytes = new Uint8Array(buf); let bin = ''; for(let i=0;i<bytes.length;i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function rtSendPcm(float32, srcRate){
  if(!rtSocketReady() || !float32 || !float32.length) return;
  const data = rtPcm16Base64(float32, srcRate);
  if(data) _rtWs.send(JSON.stringify({type:'audio', data: data, mime:'audio/pcm;rate=24000'}));
}

async function rtBeginMic(){
  if(_rtStreaming) return;
  await rtEnsureSocket();
  _rtStream = await navigator.mediaDevices.getUserMedia({audio:true});
  _rtAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if(_rtAudioCtx.state === 'suspended'){ try { await _rtAudioCtx.resume(); } catch(_){} }
  _rtSource = _rtAudioCtx.createMediaStreamSource(_rtStream);
  _rtNode = _rtAudioCtx.createScriptProcessor(4096, 1, 1);
  _rtSink = _rtAudioCtx.createGain(); _rtSink.gain.value = 0;   // silent sink, no feedback
  _rtNode.onaudioprocess = (e) => { rtSendPcm(e.inputBuffer.getChannelData(0), _rtAudioCtx.sampleRate); };
  _rtSource.connect(_rtNode); _rtNode.connect(_rtSink); _rtSink.connect(_rtAudioCtx.destination);
  _rtStreaming = true;
  stopPlayback(); if(rtSocketReady()) _rtWs.send(JSON.stringify({type:'interrupt'}));  // barge-in
  const mic = document.getElementById('mic'); if(mic) mic.classList.add('recording');
}
function rtStopAudioGraph(){
  _rtStreaming = false;
  try { if(_rtNode){ _rtNode.onaudioprocess = null; _rtNode.disconnect(); } } catch(_){}
  try { if(_rtSource) _rtSource.disconnect(); } catch(_){}
  try { if(_rtSink) _rtSink.disconnect(); } catch(_){}
  try { if(_rtStream) _rtStream.getTracks().forEach(t => t.stop()); } catch(_){}
  try { if(_rtAudioCtx) _rtAudioCtx.close(); } catch(_){}
  _rtNode = _rtSource = _rtSink = _rtStream = _rtAudioCtx = null;
  const mic = document.getElementById('mic'); if(mic) mic.classList.remove('recording');
}
async function rtEndMic(){
  if(!_rtStreaming) return;
  rtStopAudioGraph();
  if(rtSocketReady()){ setBusy(true); _rtPendingTyping = appendTyping(); rtArmTurn(); _rtWs.send(JSON.stringify({type:'commit'})); }
}
function rtTeardown(){
  rtStopAudioGraph();
  if(_rtWs){ try { _rtWs.send(JSON.stringify({type:'stop'})); } catch(_){} try { _rtWs.close(); } catch(_){} }
  _rtWs = null; _rtReady = false; _rtTurn = null; rtResolveAwait();
}
window.addEventListener('beforeunload', rtTeardown);
/* Test hook: stream synthetic PCM through the real encode->send path (no mic). */
window.__praxisVoice = {
  ensureSocket: rtEnsureSocket,
  sendPcm: (arr, rate) => rtSendPcm(Float32Array.from(arr), rate || 24000),
  commit: () => { if(rtSocketReady()){ setBusy(true); _rtPendingTyping = appendTyping(); rtArmTurn(); _rtWs.send(JSON.stringify({type:'commit'})); return _rtAwait; } },
  sendText: rtSendText,
  status: () => ({ open: !!(_rtWs && _rtWs.readyState===1), ready: _rtReady, streaming: _rtStreaming })
};

async function sendMessage(ev){
  ev.preventDefault();
  const ta = document.getElementById('message');
  const text = ta.value.trim(); if(!text) return;
  ta.value=''; autoGrow(ta); updateIntentChip();
  appendUser(text);
  const typing = appendTyping(); setBusy(true); isSending = true;
  const effective = resolveSendMode(text);
  function softErr(e){
    return (window.PraxisFriendly && window.PraxisFriendly.error)
      ? window.PraxisFriendly.error(e) : ('Error: '+e);
  }
  function attachOutcome(o){
    if(window.PraxisOutcome && window.PraxisOutcome.attach) window.PraxisOutcome.attach(o);
  }
  function markTour(step){
    if(window.PraxisFriendly && window.PraxisFriendly.markTour) window.PraxisFriendly.markTour(step);
  }
  try {
    if(voiceMode === 'realtime'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
      await rtSendText(text, typing);
    } else if(effective === 'chat' || effective === 'agent'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
      const wire = conv.messages.map(m=>({role:m.role, content:m.content}));
      await agentChat(conv, wire, typing);
      // Draft/hold missions often start from chat/agent mode.
      if(/draft|email|follow-up/i.test(text)) markTour('hold');
    } else if(effective === 'ask'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      const res = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: text})});
      typing.remove();
      if(res.error && !res.text){
        appendAgent(softErr(res.error));
      } else {
        const out = res.text || 'No answer.';
        const cites = (res.citations||[]);
        const meta = cites.join(', ');
        appendAgent(out, meta);
        attachOutcome({
          title: 'Look up complete',
          status: cites.length ? 'answered' : 'ok',
          mode: 'Look up',
          goal: text,
          citations: cites.length ? (cites.length + ' citation(s)') : 'none (may have abstained)',
          next: cites.length
            ? 'Open sources if you need the full note; ask a follow-up to go deeper.'
            : 'Add knowledge in the Knowledge panel, then retry for grounded answers.'
        });
        markTour('ask');
      }
      conv.messages.push({role:'assistant', content: (res.text || res.error || 'No answer.'), model: ((res.citations||[]).join(', ') || 'ask'), ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
    } else if(effective === 'research'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      const res = await api('/api/research', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query: text})});
      typing.remove();
      if(res.error && !res.text){
        appendAgent(softErr(res.error));
      } else {
        let body = res.text || 'No answer.';
        if(res.results && res.results.length){
          body += '\n\n**Sources**\n' + res.results.map((r,i)=>(i+1)+'. ['+(r.title||r.url)+']('+r.url+')').join('\n');
        }
        const n = (res.citations||[]).length || (res.results||[]).length;
        const meta = n + ' cited';
        appendAgent(body, meta);
        attachOutcome({
          title: 'Research complete',
          status: 'answered',
          mode: 'Research',
          goal: text,
          citations: n ? (n + ' source(s)') : 'none returned',
          next: 'Try mission 2 (draft email) to see how consequential sends pause for approval.'
        });
        markTour('research');
      }
      conv.messages.push({role:'assistant', content: (res.text || res.error || 'No answer.'), model: (((res.citations||[]).length)+' cited') || 'research', ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
    } else {
      // do — queue autonomous work
      const res = await api('/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({goal: text, max_attempts: 3})});
      typing.remove();
      const ok = !!res.task_id;
      if(!ok){
        appendAgent(softErr(res.error || 'Could not queue task.'));
      } else {
        appendAgent('Queued task **'+res.task_id+'** — watch the Queue panel.');
        attachOutcome({
          title: 'Queued work',
          status: 'pending',
          mode: 'Work on this',
          goal: text,
          task_id: res.task_id,
          ran: 'Submitted to the autonomous task queue',
          changed: 'Nothing consequential yet — drafts may appear; sends stay held',
          next: 'Watch Queue + Approvals. Approve with A (once) or C (this chat) when a send is held.'
        });
        markTour('do');
      }
      refresh();
    }
  } catch(e){ typing.remove(); appendAgent(softErr(e)); }
  setBusy(false); isSending = false;
}

async function resumeChat(){
  // Re-submit the current conversation after a held action was approved for this
  // chat/always/once, so the agent continues without the user typing anything.
  // NOTE: must use activeId (conversation id for the open chat).
  const conv = conversations.find(c => c.id === activeId);
  if(!conv || !conv.messages.length || isSending) return;
  // After a hold the last message is already an assistant "held for approval"
  // notice — still re-run so the model can complete the approved action.
  const typing = appendTyping(); setBusy(true); isSending = true;
  try {
    const wire = conv.messages.map(m => ({role:m.role, content:m.content}));
    await agentChat(conv, wire, typing);
  } catch(e){ typing.remove(); appendAgent((window.PraxisFriendly&&window.PraxisFriendly.error)?window.PraxisFriendly.error(e):('Error: '+e)); }
  setBusy(false); isSending = false;
}

/* ---------- composer behavior ---------- */
function autoGrow(ta){ ta.style.height='auto'; ta.style.height = Math.min(ta.scrollHeight, 144)+'px'; }
window.autoGrow = autoGrow;
const _ta = document.getElementById('message');
_ta.addEventListener('input', ()=>{ autoGrow(_ta); updateIntentChip(); });
_ta.addEventListener('keydown', e => { if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); document.getElementById('send').click(); } });

/* ---------- model picker ---------- */
async function loadProviders(){
  providers = await api('/api/providers');
  const sel = document.getElementById('prov');
  sel.innerHTML = providers.map(p=>'<option value="'+p.id+'">'+escapeHtml(p.label)+'</option>').join('');
  sel.onchange = onProvChange;
  // Sync picker with the actually configured model instead of defaulting to the first provider.
  const active = await api('/api/model');
  if(active.provider && providers.some(p=>p.id===active.provider)){
    sel.value = active.provider;
  }
  onProvChange();
  if(active.model && active.provider === sel.value){
    document.getElementById('modelInput').value = active.model;
  }
  // Refresh the top-left badge too.
  document.getElementById('modelBadge').textContent = active.model || 'mock';
  var cta = document.getElementById('firstRunCta');
  if(cta) cta.hidden = !!active.configured;
}
function onProvChange(){
  const p = providers.find(x=>x.id===document.getElementById('prov').value); if(!p) return;
  const active = document.getElementById('modelInput').value.trim();
  // Preserve the current input value if it already belongs to this provider's model list; otherwise default to the first model.
  document.getElementById('modelList').innerHTML = (p.models||[]).map(m=>'<option value="'+escapeHtml(m)+'"></option>').join('');
  const list = (p.models||[]);
  if(!list.includes(active)){
    document.getElementById('modelInput').value = list[0] || '';
  }
  document.getElementById('keyHint').textContent = p.needs_key ? ('Set env var '+p.key_env+' before using this provider.') : 'No API key required.';
}
async function applyModel(){
  const provider = document.getElementById('prov').value;
  const model = document.getElementById('modelInput').value.trim();
  if(!model){ showToast('Enter a model id.'); return; }
  const res = await api('/api/model', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider, model})});
  if(res.error){ showToast('Could not switch: '+res.error); } else { showToast('Model set → '+res.model); loadModel(); }
}
async function loadModel(){ const m = await api('/api/model'); document.getElementById('modelBadge').textContent = m.model || 'mock'; var cta = document.getElementById('firstRunCta'); if(cta) cta.hidden = !!m.configured; }

let _toastT;
function showToast(msg){ const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(_toastT); _toastT = setTimeout(()=>t.classList.remove('show'), 2600); }

/* ---------- file upload ---------- */
function humanSize(n){ if(n < 1024) return n+' B'; if(n < 1048576) return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(1)+' MB'; }
function uploadOne(file){
  const up = document.getElementById('uploads');
  const row = document.createElement('div'); row.className = 'uprow';
  const nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = file.name; nm.title = file.name;
  const sz = document.createElement('span'); sz.className = 'sz'; sz.textContent = humanSize(file.size);
  const st = document.createElement('span'); st.className = 'st pending'; st.textContent = '0%';
  const bar = document.createElement('div'); bar.className = 'upbar'; const fill = document.createElement('i'); bar.appendChild(fill);
  row.append(nm, sz, st, bar); up.prepend(row);
  const fd = new FormData(); fd.append('file', file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = e => {
    if(e.lengthComputable){ const pct = Math.round(e.loaded / e.total * 100); fill.style.width = pct+'%'; st.textContent = pct+'%'; }
  };
  xhr.onload = () => {
    let res = {}; try { res = JSON.parse(xhr.responseText || '{}'); } catch(_) {}
    const errs = res.errors || [];
    if(xhr.status >= 200 && xhr.status < 300 && (res.uploaded || 0) > 0 && !errs.length){
      fill.style.width = '100%'; st.className = 'st ok'; st.textContent = '✓ saved';
    } else {
      st.className = 'st bad'; st.textContent = '✗ failed';
      st.title = errs.join('; ') || res.error || ('HTTP '+xhr.status);
      showToast('Upload failed: '+file.name);
    }
  };
  xhr.onerror = () => { st.className = 'st bad'; st.textContent = '✗ failed'; showToast('Upload failed: '+file.name); };
  xhr.send(fd);
}
function uploadFiles(files){ Array.from(files).forEach(uploadOne); }
function initUpload(){
  const dz = document.getElementById('drop'); const fi = document.getElementById('fileInput');
  if(!dz || !fi) return;
  dz.addEventListener('click', () => fi.click());
  dz.addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); fi.click(); } });
  fi.addEventListener('change', () => { if(fi.files.length){ uploadFiles(fi.files); fi.value = ''; } });
  ['dragenter','dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('dragover'); }));
  ['dragleave','dragend','drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('dragover'); }));
  dz.addEventListener('drop', e => { const dt = e.dataTransfer; if(dt && dt.files && dt.files.length) uploadFiles(dt.files); });
  // Prevent the browser from navigating away when a file is dropped off-target.
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());
}

/* ---------- voice ---------- */
let voiceMode = 'off';
let _recorder = null, _chunks = [], _recording = false;
async function loadVoice(){
  const v = await api('/api/voice');
  voiceMode = v.mode || 'off';
  const el = document.getElementById('vmodes'); if(!el) return;
  el.innerHTML = '';
  (v.modes||[]).forEach(m => {
    const b = document.createElement('button'); b.type = 'button';
    b.className = 'vmode' + (m.id===voiceMode ? ' active' : '');
    b.textContent = m.label; b.disabled = !m.available; b.title = m.reason || '';
    b.onclick = () => setVoiceMode(m.id);
    el.appendChild(b);
  });
  const cur = (v.modes||[]).find(m => m.id===voiceMode) || {};
  document.getElementById('voiceHint').textContent = voiceMode==='off'
    ? 'Voice is off.' : (cur.reason || 'Push-to-talk in; spoken replies out.');
  updateMicVisibility();
}
async function setVoiceMode(mode){
  const v = await api('/api/voice', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode})});
  if(v.error){ showToast('Voice: '+v.error); return; }
  voiceMode = v.mode || 'off';
  if(voiceMode !== 'realtime') rtTeardown();
  showToast('Voice → '+voiceMode); loadVoice();
}
function updateMicVisibility(){
  const mic = document.getElementById('mic'); if(!mic) return;
  mic.hidden = (voiceMode === 'off') || !(navigator.mediaDevices && window.MediaRecorder);
}
async function toggleMic(){
  if(voiceMode === 'realtime'){
    try { if(_rtStreaming){ await rtEndMic(); } else { await rtBeginMic(); } }
    catch(e){ rtStopAudioGraph(); showToast('Mic error: '+e); }
    return;
  }
  if(_recording){ stopMic(); return; }
  if(!(navigator.mediaDevices && window.MediaRecorder)){ showToast('Mic not supported here'); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    _recorder = new MediaRecorder(stream); _chunks = [];
    _recorder.ondataavailable = e => { if(e.data && e.data.size) _chunks.push(e.data); };
    _recorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      document.getElementById('mic').classList.remove('recording');
      const blob = new Blob(_chunks, {type: _recorder.mimeType || 'audio/webm'});
      await transcribeBlob(blob);
    };
    _recorder.start(); _recording = true;
    document.getElementById('mic').classList.add('recording');
  } catch(e){ showToast('Mic error: '+e); }
}
function stopMic(){ if(_recorder && _recording){ _recording = false; try { _recorder.stop(); } catch(_){} } }
async function transcribeBlob(blob){
  showToast('Transcribing…');
  try {
    const resp = await fetch('/api/transcribe', {method:'POST', headers:{'Content-Type': blob.type || 'audio/webm'}, body: blob});
    const res = await resp.json().catch(()=>({}));
    const ta = document.getElementById('message');
    if(res.text){ ta.value = (ta.value ? ta.value + ' ' : '') + res.text; autoGrow(ta); ta.focus(); }
    if((res.detail||'').includes('offline')) showToast('STT offline preview — set agents.voice.stt for real transcription');
  } catch(e){ showToast('Transcribe failed: '+e); }
}
async function speak(text){
  if(voiceMode === 'off' || !text) return;
  try {
    const resp = await fetch('/api/speak', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
    if(!resp.ok) return;
    const mime = resp.headers.get('Content-Type') || 'audio/wav';
    const url = URL.createObjectURL(new Blob([await resp.arrayBuffer()], {type: mime}));
    const audio = new Audio(url); audio.onended = () => URL.revokeObjectURL(url);
    audio.play().catch(()=>{});
  } catch(_){}
}

/* ---------- sidebar refresh ---------- */
async function refresh(){
  const st = await api('/status');
  const s = document.getElementById('status');
  s.textContent = st.running ? 'running' : 'idle'; s.className = 'badge ' + (st.running ? 'ok' : '');
  const tasks = await api('/api/tasks');
  const taskEl = document.getElementById('tasks');
  taskEl.innerHTML = (tasks && tasks.length) ? '' : '<div class="empty">No tasks yet.</div>';
  (tasks||[]).forEach(t => {
    const div = document.createElement('div'); div.className = 'task ' + t.status;
    div.innerHTML = '<div class="task-id">'+escapeHtml(t.task_id)+'</div><div class="task-goal"></div><div class="task-status">'+escapeHtml(t.status)+'</div><div class="task-out"></div>';
    div.querySelector('.task-goal').textContent = t.goal + (t.error ? ' — '+t.error : '');
    const outEl = div.querySelector('.task-out');
    if(outEl && t.output) outEl.textContent = t.output;
    if(t.output && window.PraxisOutcome && (t.status === 'completed' || t.status === 'waiting_approval' || t.status === 'failed')){
      const holder = document.createElement('div');
      holder.innerHTML = window.PraxisOutcome.renderHtml({
        title: 'Task outcome',
        status: t.status,
        goal: t.goal,
        task_id: t.task_id,
        output: t.output
      });
      if(holder.firstChild) div.appendChild(holder.firstChild);
    }
    taskEl.appendChild(div);
  });
  if(window.PraxisShell) window.PraxisShell.signal('ops', !!(tasks&&tasks.length));
  const appr = await api('/api/approvals');
  const apprEl = document.getElementById('approvals');
  apprEl.innerHTML = (appr && appr.length) ? '' : '<div class="empty">Nothing waiting approval.</div>';
  const badge = document.getElementById('apprBadge');
  if(badge){
    if(appr && appr.length){
      badge.hidden = false;
      badge.textContent = appr.length + ' approval' + (appr.length === 1 ? '' : 's');
      badge.classList.add('has');
      badge.onclick = function(){ const el = document.getElementById('approvals'); if(el) el.scrollIntoView({behavior:'smooth', block:'center'}); };
    } else {
      badge.hidden = true;
      badge.classList.remove('has');
      badge.textContent = '';
      badge.onclick = null;
    }
  }
  (appr||[]).forEach(a => {
    const div = document.createElement('div'); div.className = 'approval';
    div.innerHTML = '<div class="task-id">'+escapeHtml(a.approval_id)+'</div>'
      + '<div class="task-goal"></div><div class="task-status"></div>'
      + '<div class="approval-actions">'
      + '<button class="primary once" type="button">Approve once <span class="kbd">A</span></button>'
      + '<button class="primary chat" type="button">This chat <span class="kbd">C</span></button>'
      + '<button class="primary always" type="button">Always run '+escapeHtml(a.tool)+'</button>'
      + '<button class="deny" type="button">Deny <span class="kbd">D</span></button>'
      + '</div>'
      + '<div class="appr-hint">Shortcuts: A once · C this chat · D deny (when focus is not in a field)</div>';
    div.querySelector('.task-goal').textContent = a.tool;
    div.querySelector('.task-status').textContent = a.preview || a.rationale || '';
    div.querySelector('button.once').onclick = () => approve(a.approval_id, 'once');
    div.querySelector('button.chat').onclick = () => approve(a.approval_id, 'chat');
    div.querySelector('button.always').onclick = () => approve(a.approval_id, 'always');
    div.querySelector('button.deny').onclick = () => denyApproval(a.approval_id);
    apprEl.appendChild(div);
  });
  // Surface recent tick/provider errors so silent flake is visible.
  const errEl = document.getElementById('tickErrors');
  if(errEl){
    const errs = (st.state && st.state.errors) || [];
    if(errs.length){
      errEl.hidden = false;
      errEl.textContent = errs[errs.length-1];
      errEl.title = errs.slice(-5).join('\n');
    } else {
      errEl.hidden = true;
      errEl.textContent = '';
    }
  }
  const logs = await fetch('/log').then(r => r.text()).catch(()=> '');
  document.getElementById('logs').textContent = logs || '—';
}
async function approve(id, mode='once'){
  // Distinct approver identity for four-eyes; persisted so dual-approval needs
  // two different people (or two different stored identities).
  let approved_by = '';
  try { approved_by = localStorage.getItem('praxis.approver') || ''; } catch(_){}
  if(!approved_by){
    try {
      approved_by = (prompt('Your name/id for this approval (needed for dual-approval):') || '').trim();
    } catch(_){ approved_by = ''; }
    if(approved_by){
      try { localStorage.setItem('praxis.approver', approved_by); } catch(_){}
    }
  }
  if(!approved_by) approved_by = 'web-ui';
  const res = await api('/api/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id, mode: mode, approved_by})});
  if(res.approved){
    showToast('Approved '+id+(mode && mode!=='once' ? ' ('+mode+')' : ''));
    // Fallback if SSE resume is slow/missed — chat/always/once all emit resume.
    if(mode === 'once' || mode === 'chat' || mode === 'always'){
      setTimeout(() => { try { resumeChat(); } catch(_){} }, 150);
    }
  } else {
    showToast('Could not approve'+(res.error?': '+res.error:''));
  }
  refresh();
}
async function denyApproval(id){
  const res = await api('/api/deny', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
  showToast(res.denied ? ('Denied '+id) : ('Could not deny'+(res.error?': '+res.error:'')));
  refresh();
}
function connectEvents(){
  try {
    window.PraxisBus.on('task', (e) => {
      try {
        const ev = JSON.parse(e.data); const p = ev.payload || {};
        showToast('Task '+(p.task_id||'')+' → '+(p.status||''));
      } catch(_) {}
      refresh();
    });
    window.PraxisBus.on('resume', (e) => {
      // Server says a held action was approved for this chat/always. Re-submit
      // the current conversation so the agent can continue without the user
      // typing anything.
      try {
        const ev = JSON.parse(e.data); const p = ev.payload || {};
        showToast((p.tool||'Action')+' approved — continuing with what changed next…'); if(window.PraxisOutcome&&window.PraxisOutcome.attach){ window.PraxisOutcome.attach({title:'Approved — resuming', status:'ok', ran:p.tool||'tool', changed:'Approval granted; re-running this chat so the tool can complete', next:'Wait for the next assistant message with the tool result.'}); }
      } catch(_) {}
      resumeChat();
    });
    // Shared bus auto-reconnects on error; the 4s poll below stays as a fallback.
  } catch(_) { /* SSE unsupported — polling still keeps the UI fresh. */ }
}

/* ---------- boot ---------- */
loadConversations();
const _recent = [...conversations].sort((a,b)=>b.updated-a.updated)[0];
if(_recent) switchConversation(_recent.id); else newChat();
loadProviders();
loadModel();
refresh();
connectEvents();
initUpload();
loadVoice();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


class _UploadError(Exception):
    """Raised when a multipart/form-data body is malformed or truncated."""


def _parse_multipart_stream(
    rfile: io.BufferedIOBase,
    boundary: bytes,
    length: int,
    dest_for: Callable[[str], Path],
    *,
    chunk_size: int = _UPLOAD_CHUNK,
) -> tuple[list[str], list[str]]:
    """Stream a multipart/form-data body to disk without buffering whole files.

    Reads exactly ``length`` bytes from ``rfile`` (the connection may be
    keep-alive, so we must never read past the declared body), scans for the
    ``boundary`` delimiter with a rolling buffer kept to roughly ``chunk_size``
    bytes, and writes each file part to ``dest_for(filename)`` via a temporary
    ``.part`` file renamed into place once the part completes. Non-file fields
    are consumed and ignored. Returns ``(saved_names, errors)`` and raises
    :class:`_UploadError` if the body is structurally malformed or truncated.
    """
    needle = b"\r\n--" + boundary
    keep = len(needle) - 1
    buf = bytearray()
    remaining = length

    def more() -> bool:
        nonlocal remaining
        if remaining <= 0:
            return False
        chunk = rfile.read(min(chunk_size, remaining))
        if not chunk:
            remaining = 0
            return False
        remaining -= len(chunk)
        buf.extend(chunk)
        return True

    opening = b"--" + boundary
    while len(buf) < len(opening) and more():
        pass
    if buf[: len(opening)] != opening:
        raise _UploadError("missing opening boundary")
    del buf[: len(opening)]

    saved_names: list[str] = []
    errors: list[str] = []
    while True:
        while len(buf) < 2 and more():
            pass
        if buf[:2] == b"--":
            break  # closing delimiter: no more parts
        if buf[:2] == b"\r\n":
            del buf[:2]
        while (b"\r\n\r\n" not in buf and len(buf) <= _MAX_PART_HEADER
               and more()):
            pass
        if b"\r\n\r\n" not in buf:
            raise _UploadError("unterminated or oversized part headers")
        split = buf.index(b"\r\n\r\n")
        header = bytes(buf[:split])
        del buf[: split + 4]

        name_match = re.search(rb'filename="([^"]*)"', header)
        filename = ""
        if name_match is not None:
            filename = Path(name_match.group(1).decode("utf-8", "replace")).name

        dest_file = None
        tmp: Path | None = None
        final: Path | None = None
        write_error: str | None = None
        if name_match is not None and filename:
            try:
                final = dest_for(filename)
                tmp = final.with_name(final.name + ".part")
                dest_file = open(tmp, "wb")
            except OSError as exc:
                write_error = f"{filename}: {exc}"
                dest_file = None

        # Stream the part body to disk until the next boundary delimiter,
        # retaining a short tail so a delimiter split across reads is not missed.
        while True:
            idx = buf.find(needle)
            if idx != -1:
                out = bytes(buf[:idx])
                del buf[: idx + len(needle)]
                done = True
            else:
                cut = len(buf) - keep
                out = bytes(buf[:cut]) if cut > 0 else b""
                if cut > 0:
                    del buf[:cut]
                done = False
            if out and dest_file is not None:
                try:
                    dest_file.write(out)
                except OSError as exc:
                    write_error = f"{filename}: {exc}"
                    dest_file.close()
                    dest_file = None
                    if tmp is not None:
                        tmp.unlink(missing_ok=True)
            if done:
                break
            if not more():
                if dest_file is not None:
                    dest_file.close()
                    if tmp is not None:
                        tmp.unlink(missing_ok=True)
                raise _UploadError("unterminated part body")

        if dest_file is not None:
            dest_file.close()
            assert tmp is not None and final is not None
            try:
                os.replace(tmp, final)
                saved_names.append(filename)
            except OSError as exc:
                errors.append(f"{filename}: {exc}")
                tmp.unlink(missing_ok=True)
        elif write_error is not None:
            errors.append(write_error)

    return saved_names, errors


class _StatusHandler(BaseHTTPRequestHandler):
    # Bound every socket read so a client that sends a large Content-Length but
    # then stalls (or a slow-loris) can't wedge a ThreadingHTTPServer worker
    # thread forever — the read raises socket.timeout and the handler's
    # try/except returns an error response instead of blocking indefinitely.
    timeout = 30

    def __init__(self, daemon: "Daemon", *args, **kwargs) -> None:
        self.daemon = daemon
        super().__init__(*args, **kwargs)

    def _read_body(self, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        """Read the request body, clamped to ``max_bytes``, so an inflated
        Content-Length can't drive an unbounded allocation. Reads exactly the
        declared length (clamped); the socket timeout guards a stalled sender."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(min(length, max_bytes))

    def _require_auth(self) -> bool:
        """Non-loopback clients must present the shared token when configured."""
        from . import auth_gate
        if self._is_loopback():
            return True
        if not auth_gate.configured_token():
            return True
        if auth_gate.token_matches(auth_gate.extract_token(self.headers)):
            return True
        self._json_response(
            {"error": "unauthorized", "auth_required": True,
             "hint": "Set Authorization: Bearer <token> or X-Praxis-Token"},
            status=401)
        return False

    def _require_v1_auth(self, *, mutation: bool = False) -> bool:
        """Accept a professional session or the legacy deployment token."""
        from . import auth_gate
        from .authn import SessionManager, session_token_from_cookie

        self.authenticated_session = None
        cookie_token = session_token_from_cookie(self.headers.get("Cookie", ""))
        if cookie_token:
            self.daemon._ensure_agent()
            assert self.daemon.store is not None
            session = SessionManager(self.daemon.store).authenticate(
                cookie_token,
                mutation=mutation,
                csrf_token=self.headers.get("X-CSRF-Token", ""),
            )
            if session is not None:
                self.authenticated_session = session
                return True
        if not cookie_token and (self._is_loopback() or not auth_gate.configured_token()):
            return True
        if (auth_gate.configured_token()
                and auth_gate.token_matches(auth_gate.extract_token(self.headers))):
            return True
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        self._v1_response(
            error_envelope(
                "unauthorized", "Authentication is required",
                request_id=request_id,
                details={"hint": "Set Authorization: Bearer *** or X-Praxis-Token"},
            ), status=401)
        return False

    def _professional_authorize(self, action: str) -> str | None:
        """Authorize a session-backed action and return its tenant scope."""
        from .authz import AccessContext, AuthorizationPolicy
        from .organizations import OrganizationDirectory

        session = getattr(self, "authenticated_session", None)
        if session is None:
            return ""  # legacy token/loopback compatibility scope
        assert self.daemon.store is not None
        membership = OrganizationDirectory(self.daemon.store).membership(
            session.organization_id, session.user_id)
        if membership is None or membership.status != "active":
            return None
        decision = AuthorizationPolicy().authorize(
            AccessContext(session.user_id, session.organization_id,
                          frozenset(membership.roles), "service_delivery"),
            action, resource_organization_id=session.organization_id)
        return session.organization_id if decision.allowed else None

    def _read_v1_json_object(self) -> dict | None:
        """Read one bounded v1 JSON object with stable transport errors."""
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        try:
            declared_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            declared_length = -1
        if declared_length < 0 or declared_length > MAX_JSON_BODY_BYTES:
            # Darwin resets a TCP connection closed with unread request bytes, which can
            # discard the structured 413 response. Drain only a bounded amount: this
            # preserves the response for normal oversize mistakes without allowing an
            # attacker-controlled Content-Length to trigger an unbounded read.
            drain_remaining = min(max(declared_length, 0), MAX_JSON_BODY_BYTES * 4)
            prior_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(0.1)
                while drain_remaining:
                    chunk = self.rfile.read(min(drain_remaining, 64 * 1024))
                    if not chunk:
                        break
                    drain_remaining -= len(chunk)
            except OSError:
                pass
            finally:
                self.connection.settimeout(prior_timeout)
            self._v1_response(error_envelope(
                "payload_too_large",
                f"JSON body must be at most {MAX_JSON_BODY_BYTES} bytes",
                request_id=request_id), status=413)
            return None
        try:
            payload = json.loads(self.rfile.read(declared_length).decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._v1_response(error_envelope(
                "invalid_json", "Request body must be valid JSON",
                request_id=request_id), status=400)
            return None
        if not isinstance(payload, dict):
            self._v1_response(error_envelope(
                "invalid_request", "Request body must be a JSON object",
                request_id=request_id), status=400)
            return None
        return payload

    def _professional_workspace_scope(
        self, action: str,
    ) -> tuple[str, str] | None:
        """Authorize and resolve the workspace selected by a professional request."""
        organization_id = self._professional_authorize(action)
        if organization_id is None:
            self._v1_response(error_envelope(
                "forbidden", "The authenticated role cannot perform this action",
                request_id=uuid.uuid4().hex), status=403)
            return None
        session = getattr(self, "authenticated_session", None)
        if session is None:
            return organization_id, ""  # legacy token/loopback compatibility
        workspace_id = str(self.headers.get("X-Praxis-Workspace-ID") or "").strip()
        if not workspace_id:
            self._v1_response(error_envelope(
                "workspace_required", "X-Praxis-Workspace-ID is required",
                request_id=uuid.uuid4().hex), status=400)
            return None
        assert self.daemon.store is not None
        from .workspaces import WorkspaceDirectory
        if WorkspaceDirectory(self.daemon.store).get(
                organization_id, workspace_id) is None:
            self._v1_response(error_envelope(
                "workspace_not_found", "Workspace was not found",
                request_id=uuid.uuid4().hex), status=404)
            return None
        return organization_id, workspace_id

    def do_POST(self) -> None:
        try:
            if self.path == "/stop":
                if not self._require_auth():
                    return
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"stopping": true}')
                threading.Thread(target=self.daemon.stop, daemon=True).start()
                return
            if self.path == "/api/v1/auth/session":
                if not self._require_auth():
                    return
                self._handle_v1_session_issue()
                return
            if self.path == "/api/v1/auth/logout":
                if not self._require_v1_auth(mutation=True):
                    return
                self._handle_v1_session_logout()
                return
            if (self.path.startswith("/api/v1/approvals/")
                    and self.path.endswith("/approve")):
                if not self._require_v1_auth(mutation=True):
                    return
                self._handle_v1_approval()
                return
            if self.path == "/api/v1/board/cards":
                if not self._require_v1_auth(mutation=True):
                    return
                scope = self._professional_workspace_scope("write")
                if scope is None:
                    return
                self._handle_v1_board_create(*scope)
                return
            if self.path == "/api/v1/workspaces":
                if not self._require_v1_auth(mutation=True):
                    return
                organization_id = self._professional_authorize("write")
                if organization_id is None:
                    self._v1_response(error_envelope(
                        "forbidden", "The authenticated role cannot create workspaces",
                        request_id=uuid.uuid4().hex), status=403)
                    return
                self._handle_v1_workspace_create(organization_id)
                return
            if self.path == "/api/v1/workspace/timeline":
                if not self._require_v1_auth(mutation=True):
                    return
                scope = self._professional_workspace_scope("write")
                if scope is None:
                    return
                self._handle_v1_timeline_append(*scope)
                return
            if self.path == "/api/v1/workspace/rooms":
                if not self._require_v1_auth(mutation=True):
                    return
                scope = self._professional_workspace_scope("manage_external_room")
                if scope is None:
                    return
                self._handle_v1_room_create(*scope)
                return
            # Auth gate for all other mutating routes when bound beyond loopback.
            if self.path not in ("/api/auth/login",) and not self._require_auth():
                return
            if self.path == "/api/auth/login":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                from . import auth_gate
                tok = str(payload.get("token") or "")
                ok = auth_gate.token_matches(tok)
                self._json_response({"ok": ok, "auth_required": auth_gate.auth_required(
                    self.daemon.status_host)})
                return
            if self.path == "/submit":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                payload = json.loads(body)
                try:
                    task_id = self.daemon.submit(
                        payload["goal"], max_attempts=payload.get("max_attempts", 3))
                except RuntimeError as exc:
                    self._json_response({"error": str(exc), "blocked": True})
                    return
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
            if self.path == "/api/research":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.research(
                    payload.get("query", ""),
                    max_results=int(payload.get("max_results", 5) or 5)))
                return
            if self.path == "/api/agent/run":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.agent_run(
                    payload.get("goal", ""),
                    max_replans=int(payload.get("max_replans", 1))))
                return
            if self.path == "/api/approve":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                payload = json.loads(body)
                approval_id = payload.get("approval_id", "")
                mode = payload.get("mode", "once")
                approved = self.daemon.approve(
                    approval_id, mode=mode,
                    approved_by=str(payload.get("approved_by") or ""))
                self._json_response({"approved": bool(approved), "approval_id": approval_id, "mode": mode})
                return
            if self.path == "/api/deny":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                aid = payload.get("approval_id", "")
                self._json_response({"denied": self.daemon.deny_approval(aid),
                                     "approval_id": aid})
                return
            if self.path == "/api/killswitch":
                # Safety-critical: only accept from loopback when no auth exists.
                if not self._is_loopback():
                    self._json_response(
                        {"error": "kill-switch changes are only allowed from localhost"},
                        status=403)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.killswitch_set(
                    bool(payload.get("engaged", False))))
                return
            if self.path == "/api/compliance":
                if not self._is_loopback():
                    self._json_response(
                        {"error": "compliance changes are only allowed from localhost"},
                        status=403)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.compliance_set(
                    str(payload.get("mode", "")),
                    ttl_seconds=payload.get("ttl_seconds")))
                return
            if self.path == "/api/secrets":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                action = str(payload.get("action", ""))
                # Secrets mutations only from localhost — defense in depth for a
                # 0.0.0.0-bound container (the dashboard has no auth yet).
                if action in ("set", "delete", "migrate") and not self._is_loopback():
                    self._json_response(
                        {"error": "secret changes are only allowed from localhost"},
                        status=403)
                    return
                if action == "set":
                    self._json_response(self.daemon.secrets_set(
                        str(payload.get("provider", "")),
                        str(payload.get("key", ""))))
                elif action == "delete":
                    self._json_response(self.daemon.secrets_delete(
                        str(payload.get("provider", ""))))
                elif action == "migrate":
                    self._json_response(self.daemon.secrets_migrate())
                else:
                    self._json_response({"error": f"unknown action '{action}'"},
                                        status=400)
                return
            if self.path == "/api/onboard":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                # A pasted key is a secret -> accept it only from localhost.
                if payload.get("api_key") and not self._is_loopback():
                    self._json_response(
                        {"error": "pasting a key is only allowed from localhost"},
                        status=403)
                    return
                self._json_response(self.daemon.onboard_apply(
                    str(payload.get("provider", "")),
                    str(payload.get("model", "")),
                    base_url=payload.get("base_url"),
                    api_key=payload.get("api_key"),
                    use_env_ref=bool(payload.get("use_env_ref", True))))
                return
            if self.path == "/api/budget":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                if payload.get("reset"):
                    self._json_response(self.daemon.budget_reset())
                else:
                    self._json_response(self.daemon.budget_set(
                        float(payload.get("limit_usd", 0) or 0)))
                return
            if self.path == "/api/memory":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.memory_add(
                    payload.get("tier", "durable"), payload.get("text", ""),
                    payload.get("provenance", "dashboard")))
                return
            if self.path == "/api/memory/delete":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.memory_delete(
                    int(payload.get("id", 0) or 0)))
                return
            if self.path == "/api/consolidation/run":
                self._json_response(self.daemon.consolidation_run())
                return
            if self.path == "/api/sources":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.sources_add(
                    payload.get("uri", ""), ns=payload.get("ns", "kb"),
                    title=payload.get("title", ""),
                    refresh_hours=payload.get("refresh_hours")))
                return
            if self.path == "/api/sources/delete":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.sources_delete(
                    payload.get("source_id", "")))
                return
            if self.path == "/api/sources/refresh":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.sources_refresh(
                    payload.get("source_id", "")))
                return
            if self.path == "/api/cron":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.cron_create(
                    payload.get("goal", ""), payload.get("schedule", ""),
                    name=payload.get("name", ""), mode=payload.get("mode", "do"),
                    deliver=payload.get("deliver", "local")))
                return
            if self.path == "/api/cron/delete":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.cron_delete(
                    payload.get("job_id", "")))
                return
            if self.path == "/api/cron/toggle":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.cron_set_enabled(
                    payload.get("job_id", ""), bool(payload.get("enabled", True))))
                return
            if self.path == "/api/board/create":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.board_create(
                    payload.get("title", ""), payload.get("goal", "")))
                return
            if self.path == "/api/board/move":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.board_move(
                    payload.get("card_id", ""), payload.get("lane", "")))
                return
            if self.path == "/api/board/run":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.board_run(payload.get("card_id", "")))
                return
            if self.path == "/api/board/delete":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.board_delete(
                    payload.get("card_id", "")))
                return
            if self.path == "/api/chat/agent":
                self._handle_chat_agent()
                return
            if self.path == "/api/chat/stream":
                self._handle_chat_stream()
                return
            if self.path == "/api/chat":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                messages = payload.get("messages") or []
                if not isinstance(messages, list):
                    messages = []
                chat_result = self.daemon.chat(messages, system=payload.get("system"))
                self._json_response(chat_result)
                return
            if self.path == "/api/model":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                try:
                    model_result = self.daemon.switch_model(
                        payload.get("provider", ""), payload.get("model", ""),
                        base_url=payload.get("base_url"))
                except ValueError as exc:
                    self._json_response({"error": str(exc)}, status=400)
                    return
                self._json_response(model_result)
                return
            if self.path == "/api/voice":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                try:
                    voice_result = self.daemon.set_voice_mode(payload.get("mode", ""))
                except ValueError as exc:
                    self._json_response({"error": str(exc)}, status=400)
                    return
                self._json_response(voice_result)
                return
            if self.path == "/api/transcribe":
                self._handle_transcribe()
                return
            if self.path == "/api/speak":
                self._handle_speak()
                return
            if self.path == "/upload":
                self._handle_upload()
                return
            # ---- preeminence sprint: persona / growth / pulse / channels ----
            if self.path == "/api/persona":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.persona_set(payload))
                return
            if self.path == "/api/pulse":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.pulse(
                    target=payload.get("target")))
                return
            if self.path == "/api/growth/evolve":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.growth_evolve(
                    limit=int(payload.get("limit", 3) or 3)))
                return
            if self.path == "/api/growth/apply":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.growth_apply(
                    str(payload.get("id") or "")))
                return
            if self.path == "/api/growth/reject":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.growth_reject(
                    str(payload.get("id") or "")))
                return
            if self.path == "/api/growth/ttft":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.record_ttft(
                    float(payload.get("seconds") or 0)))
                return
            if self.path == "/api/channels/telegram":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                action = str(payload.get("action") or "configure")
                if action == "disable":
                    self._json_response(self.daemon.telegram_disable())
                else:
                    self._json_response(self.daemon.telegram_configure(
                        bot_token=str(payload.get("bot_token") or ""),
                        chat_id=str(payload.get("chat_id") or ""),
                        enabled=bool(payload.get("enabled", True)),
                        use_env_ref=bool(payload.get("use_env_ref", False))))
                return
            if self.path == "/api/channels/telegram/webhook":
                length = int(self.headers.get("Content-Length", 0) or 0)
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.telegram_webhook(payload))
                return
            if self.path == "/api/channels/slack/events":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode() or "{}")
                self._json_response(self.daemon.slack_events(
                    payload, raw=raw, headers=self.headers))
                return
            self.send_response(404)
            self.end_headers()
        except Exception as exc:
            self._error_response(exc)

    def do_GET(self) -> None:
        try:
            parsed = split_url(self.path)
            if parsed.path == "/api/v1/board/cards":
                if not self._require_v1_auth():
                    return
                scope = self._professional_workspace_scope("read")
                if scope is None:
                    return
                self._handle_v1_board_list(parse_query(parsed.query), *scope)
                return
            if parsed.path == "/api/v1/workspaces":
                if not self._require_v1_auth():
                    return
                organization_id = self._professional_authorize("read")
                if organization_id is None:
                    self._v1_response(error_envelope(
                        "forbidden", "The authenticated role cannot read workspaces",
                        request_id=uuid.uuid4().hex), status=403)
                    return
                self._handle_v1_workspace_list(organization_id)
                return
            if parsed.path == "/api/v1/workspace/timeline":
                if not self._require_v1_auth():
                    return
                scope = self._professional_workspace_scope("read")
                if scope is None:
                    return
                self._handle_v1_timeline_list(*scope)
                return
            if self.path == "/status":
                mgr = self.daemon.manager
                from . import pack as _pack
                _ap = _pack.active()
                body = json.dumps({
                    "running": self.daemon.running,
                    "port": self.daemon.status_port,
                    "state": self.daemon.state.to_dict(),
                    "pack": ({"name": _ap.name, "vertical": _ap.vertical,
                              "theme": _ap.theme, "model": _ap.model} if _ap else None),
                    "pending_tasks": len(mgr.list(status="pending")) if mgr else 0,
                    "running_tasks": len(mgr.list(status="running")) if mgr else 0,
                    "waiting_approval_tasks": (
                        len(mgr.list(status="waiting_approval")) if mgr else 0
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
            elif self.path == "/api/auth/status":
                from . import auth_gate
                body = json.dumps(auth_gate.status_dict(
                    self.daemon.status_host)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/persona":
                body = json.dumps(self.daemon.persona_get()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/pulse":
                body = json.dumps(self.daemon.pulse_preview(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/growth/model":
                body = json.dumps(self.daemon.growth_model()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/growth/skills":
                body = json.dumps({"skills": self.daemon.growth_skills()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/growth/proposals":
                body = json.dumps({"proposals": self.daemon.growth_proposals()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/growth/rooms":
                body = json.dumps({"rooms": self.daemon.growth_rooms()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/growth/ttft":
                body = json.dumps(self.daemon.ttft_stats()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/channels/status":
                body = json.dumps(self.daemon.channels_status()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/channels/telegram":
                body = json.dumps(self.daemon.telegram_status()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/browser/snapshot":
                body = json.dumps(self.daemon.browser_snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/approvals":
                body = json.dumps(self.daemon.list_approvals(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/model":
                body = json.dumps(self.daemon.model_info(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/providers":
                body = json.dumps(self.daemon.providers_catalog(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/voice":
                body = json.dumps(self.daemon.voice_status(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/voice/realtime":
                self._handle_realtime_ws()
                return
            elif self.path == "/api/agent/card":
                body = json.dumps(self.daemon.agent_card(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/traces":
                body = json.dumps(self.daemon.list_runs_trace(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path.startswith("/api/traces/"):
                rid = self.path[len("/api/traces/"):].split("?", 1)[0]
                body = json.dumps(self.daemon.get_run_trace(rid), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/board":
                body = json.dumps(
                    self.daemon.board_list("", ""), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Deprecation", "true")
                self.send_header("Sunset", "Tue, 12 Jan 2027 00:00:00 GMT")
                self.send_header(
                    "Link", '</api/v1/board/cards>; rel="successor-version"')
            elif self.path == "/api/readiness":
                body = json.dumps(self.daemon.readiness(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/sources":
                body = json.dumps(self.daemon.sources_list(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/cron":
                body = json.dumps(self.daemon.cron_list(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/audit":
                body = json.dumps(self.daemon.audit_log(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/killswitch":
                body = json.dumps(self.daemon.killswitch_status(),
                                  default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/compliance":
                body = json.dumps(self.daemon.compliance_status(),
                                  default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/secrets":
                body = json.dumps(self.daemon.secrets_status(),
                                  default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/metrics":
                body = json.dumps(self.daemon.metrics(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/inference":
                body = json.dumps(self.daemon.inference_info(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/budget":
                body = json.dumps(self.daemon.budget_status(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/memory":
                body = json.dumps(self.daemon.memory_list(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/consolidation":
                body = json.dumps(self.daemon.consolidation_status(),
                                  default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path.startswith("/api/search"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                body = json.dumps(self.daemon.search(q), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path.startswith("/web/"):
                self._serve_static(self.path[len("/web/"):])
                return
            elif self.path == "/events":
                self._serve_sse()
                return
            elif self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                body = b"not found"
                self.send_response(404)
        except Exception as exc:
            if split_url(self.path).path.startswith("/api/v1/"):
                self._error_response(exc)
                return
            body = f"error: {exc}".encode()
            self.send_response(500)
        self.end_headers()
        self.wfile.write(body)

    def _is_loopback(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return (host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                or host.startswith("127."))

    def _serve_static(self, rel: str) -> None:
        """Serve a static asset from the package ``web/`` bundle (modular shell)."""
        rel = rel.split("?", 1)[0]
        base = Path(__file__).resolve().parent / "web"
        target = (base / rel).resolve()
        if base not in target.parents or not target.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return
        ctype = {".js": "text/javascript", ".css": "text/css",
                 ".html": "text/html", ".svg": "image/svg+xml",
                 ".json": "application/json",
                 ".webmanifest": "application/manifest+json"}.get(
                     target.suffix, "application/octet-stream")
        if target.name == "manifest.webmanifest":
            ctype = "application/manifest+json"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        # An SSE stream is a terminal response; don't let the handler try to
        # parse a pipelined request afterward (which would read a dead socket).
        self.close_connection = True
        # Each connection gets its own queue so emit_event can fan out to every
        # subscriber. Only this thread writes to this socket, avoiding the
        # interleaved-frame corruption of the old shared-queue design.
        queue = self.daemon._sse_subscribe()
        try:
            self.wfile.write(b"event: connected\ndata: \"ok\"\n\n")
            self.wfile.flush()
            while self.daemon.running and not self.wfile.closed:
                try:
                    event = queue.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except Empty:
                    # Keep the connection warm and surface dead peers (a write
                    # to a closed socket raises and breaks the loop).
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:  # shutdown sentinel
                    break
                self.wfile.write(f"event: {event.get('type', 'message')}\n".encode())
                self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.daemon._sse_unsubscribe(queue)

    def _handle_chat_stream(self) -> None:
        # A streamed chat reply is a terminal response (we close on completion),
        # so don't let the handler attempt keep-alive reuse afterward.
        self.close_connection = True
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        system = payload.get("system")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # Disable proxy buffering so deltas reach the browser as they are written.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(f"data: {json.dumps(obj, default=str)}\n\n".encode())
            self.wfile.flush()

        try:
            emit({"type": "meta",
                  "model": cfg.get_default_model() or "mock (offline)"})
            for piece in self.daemon.chat_stream(messages, system=system):
                if piece:
                    emit({"type": "delta", "text": piece})
            emit({"type": "done"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:
            try:
                emit({"type": "error", "error": str(exc)})
            except OSError:
                pass

    def _handle_chat_agent(self) -> None:
        # Like the streaming chat handler, a governed turn is a terminal response.
        self.close_connection = True
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            payload = {}
        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        system = payload.get("system")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(f"data: {json.dumps(obj, default=str)}\n\n".encode())
            self.wfile.flush()

        try:
            emit({"type": "meta",
                  "model": cfg.get_default_model() or "mock (offline)"})
            for event in self.daemon.chat_agent(messages, system=system):
                emit(event)
            emit({"type": "done"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:
            try:
                emit({"type": "error", "error": str(exc)})
            except OSError:
                pass

    def _handle_realtime_ws(self) -> None:
        # Hand-rolled WebSocket upgrade (the daemon's http.server has no WS), then
        # run the governed realtime bridge over the hijacked socket.
        from .wsutil import WebSocketConn, accept_key, is_ws_upgrade
        if not is_ws_upgrade(self.headers):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"expected websocket upgrade")
            return
        self.close_connection = True
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept",
                         accept_key(self.headers.get("Sec-WebSocket-Key", "")))
        self.end_headers()
        self.wfile.flush()
        conn = WebSocketConn(self.rfile, self.wfile, sock=self.connection)
        try:
            self.daemon.run_realtime_session(conn)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _handle_transcribe(self) -> None:
        # The browser POSTs the recorded audio blob as the raw request body with
        # its own Content-Type (e.g. audio/webm). Speech-to-text, return JSON.
        self.close_connection = True
        length = int(self.headers.get("Content-Length", 0) or 0)
        mime = self.headers.get("Content-Type", "audio/webm")
        audio = self.rfile.read(length) if length > 0 else b""
        try:
            result = self.daemon.transcribe(audio, mime)
        except Exception as exc:
            self._json_response({"error": str(exc)}, status=500)
            return
        self._json_response(result)

    def _handle_speak(self) -> None:
        # Text-to-speech: returns audio bytes the browser can play.
        self.close_connection = True
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            payload = {}
        text = str(payload.get("text", ""))[:4000]
        try:
            res = self.daemon.synthesize(text)
        except Exception as exc:
            self._json_response({"error": str(exc)}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", res.mime or "audio/wav")
        self.send_header("Content-Length", str(len(res.audio)))
        self.send_header("X-Voice-Detail", res.detail or "")
        self.end_headers()
        self.wfile.write(res.audio)

    def _handle_upload(self) -> None:
        # Uploads terminate their connection: we may reject before draining the
        # body, and the parser stops at the closing delimiter (leaving the
        # trailing epilogue unread), so keep-alive reuse would misframe.
        self.close_connection = True
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._json_response({"error": "expected multipart/form-data"}, status=400)
            return
        boundary_match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
        if not boundary_match:
            self._json_response({"error": "missing multipart boundary"}, status=400)
            return
        boundary = (boundary_match.group(1) or boundary_match.group(2)).strip().encode()
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self._json_response({"error": "Content-Length required"}, status=411)
            return
        try:
            length = int(length_hdr)
        except ValueError:
            self._json_response({"error": "invalid Content-Length"}, status=400)
            return
        if length < 0:
            self._json_response({"error": "invalid Content-Length"}, status=400)
            return
        max_bytes = self.daemon.max_upload_bytes
        if length > max_bytes:
            self._json_response(
                {"error": f"upload exceeds the {max_bytes}-byte limit"}, status=413)
            return
        try:
            saved, errors = _parse_multipart_stream(
                self.rfile, boundary, length, self.daemon.work_dir_upload)
        except _UploadError as exc:
            self._json_response({"error": f"malformed upload: {exc}"}, status=400)
            return
        self._json_response({"uploaded": len(saved), "files": saved, "errors": errors})

    def _handle_v1_approval(self) -> None:
        from .authz import AccessContext, AuthorizationPolicy
        from .organizations import OrganizationDirectory

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        session = getattr(self, "authenticated_session", None)
        if session is None:
            self._v1_response(error_envelope(
                "professional_session_required", "A professional session is required",
                request_id=request_id), status=403)
            return
        assert self.daemon.store is not None
        membership = OrganizationDirectory(self.daemon.store).membership(
            session.organization_id, session.user_id)
        if membership is None:
            self._v1_response(error_envelope(
                "membership_required", "An active membership is required",
                request_id=request_id), status=403)
            return
        approval_id = self.path.removeprefix(
            "/api/v1/approvals/").removesuffix("/approve")
        approval = self.daemon.store.get_approval(approval_id)
        if approval is None or approval.get("organization_id") != session.organization_id:
            self._v1_response(error_envelope(
                "approval_not_found", "Approval does not exist in this organization",
                request_id=request_id), status=404)
            return
        decision = AuthorizationPolicy().authorize(
            AccessContext(session.user_id, session.organization_id,
                          frozenset(membership.roles), "service_delivery"),
            "approve_decision", resource_organization_id=session.organization_id)
        if not decision.allowed:
            self._v1_response(error_envelope(
                "approval_forbidden", "The authenticated role cannot approve decisions",
                request_id=request_id,
                details={"reason": decision.reason}), status=403)
            return
        role = next((name for name in membership.roles if name in {
            "organization_admin", "workspace_admin", "professional"}), "")
        approved = self.daemon.approve(
            approval_id, approved_by=session.user_id, approved_role=role)
        self._v1_response(success_envelope(
            {"approval_id": approval_id, "approved": approved,
             "actor": session.user_id, "role": role}, request_id=request_id),
            status=200 if approved else 409)

    def _handle_v1_session_issue(self) -> None:
        from .authn import SessionManager

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        try:
            payload = json.loads(self._read_body(MAX_JSON_BODY_BYTES).decode() or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            user_id = str(payload.get("user_id") or "").strip()
            organization_id = str(payload.get("organization_id") or "").strip()
            if not user_id or not organization_id:
                raise ValueError("user_id and organization_id are required")
            self.daemon._ensure_agent()
            assert self.daemon.store is not None
            issued = SessionManager(self.daemon.store).issue(
                user_id, organization_id,
                ttl_seconds=min(float(payload.get("ttl_seconds", 28800)), 86400),
                device_id=str(payload.get("device_id") or ""),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._v1_response(error_envelope(
                "invalid_session_request", str(exc), request_id=request_id), status=400)
            return
        cookie = (
            f"praxis_session={issued.token}; HttpOnly; SameSite=Strict; Path=/api/v1; "
            f"Max-Age={max(0, int(issued.expires_ts - time.time()))}"
        )
        if not self._is_loopback():
            cookie += "; Secure"
        self._v1_response(success_envelope(
            {"session_id": issued.session_id, "user_id": issued.user_id,
             "organization_id": issued.organization_id,
             "csrf_token": issued.csrf_token, "expires_ts": issued.expires_ts},
            request_id=request_id), status=201, headers={"Set-Cookie": cookie})

    def _handle_v1_session_logout(self) -> None:
        from .authn import SessionManager

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        session = getattr(self, "authenticated_session", None)
        if session is None:
            self._v1_response(error_envelope(
                "professional_session_required", "A professional session is required",
                request_id=request_id), status=403)
            return
        assert self.daemon.store is not None
        SessionManager(self.daemon.store).revoke(session.session_id)
        self._v1_response(success_envelope(
            {"revoked": True}, request_id=request_id),
            headers={"Set-Cookie": (
                "praxis_session=; HttpOnly; SameSite=Strict; Path=/api/v1; Max-Age=0")})

    def _handle_v1_workspace_list(self, organization_id: str) -> None:
        from .workspaces import WorkspaceDirectory

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        assert self.daemon.store is not None
        items = [asdict(workspace) for workspace in
                 WorkspaceDirectory(self.daemon.store).list_for(organization_id)]
        version = resource_version(items)
        self._v1_response(success_envelope(
            {"items": items}, request_id=request_id,
            meta={"resource_version": version}), headers={"ETag": etag(version)})

    def _handle_v1_workspace_create(self, organization_id: str) -> None:
        from .workspaces import WorkspaceDirectory, WorkspaceError

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        session = getattr(self, "authenticated_session", None)
        if session is None:
            self._v1_response(error_envelope(
                "professional_session_required", "A professional session is required",
                request_id=request_id), status=403)
            return
        try:
            payload = self._read_v1_json_object()
            if payload is None:
                return
            assert self.daemon.store is not None
            workspace = WorkspaceDirectory(self.daemon.store).create(
                organization_id,
                str(payload.get("human_identifier") or ""),
                str(payload.get("kind") or ""),
                str(payload.get("title") or ""),
                owner_user_id=str(payload.get("owner_user_id") or session.user_id),
                team_id=str(payload.get("team_id") or ""),
                client_or_subject=str(payload.get("client_or_subject") or ""),
                confidentiality=str(payload.get("confidentiality") or "internal"),
                jurisdiction=str(payload.get("jurisdiction") or ""),
                location=str(payload.get("location") or ""),
                custom_fields=dict(payload.get("custom_fields") or {}),
                external_links=tuple(payload.get("external_links") or []),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
                WorkspaceError) as exc:
            self._v1_response(error_envelope(
                "invalid_workspace", str(exc), request_id=request_id), status=400)
            return
        self._v1_response(success_envelope(
            {"workspace": asdict(workspace)}, request_id=request_id,
            meta={"resource_version": resource_version(asdict(workspace))}), status=201)

    def _handle_v1_timeline_list(
        self, organization_id: str, workspace_id: str,
    ) -> None:
        from .workspace_timeline import WorkspaceTimeline

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        assert self.daemon.store is not None
        items = [asdict(event) for event in WorkspaceTimeline(
            self.daemon.store).events(organization_id, workspace_id)]
        self._v1_response(success_envelope({"items": items}, request_id=request_id))

    def _handle_v1_timeline_append(
        self, organization_id: str, workspace_id: str,
    ) -> None:
        from .workspace_timeline import TimelineError, WorkspaceTimeline

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        session = getattr(self, "authenticated_session", None)
        if session is None:
            self._v1_response(error_envelope(
                "professional_session_required", "A professional session is required",
                request_id=request_id), status=403)
            return
        try:
            payload = self._read_v1_json_object()
            if payload is None:
                return
            assert self.daemon.store is not None
            event = WorkspaceTimeline(self.daemon.store).append_event(
                organization_id, workspace_id,
                str(payload.get("event_type") or ""),
                str(payload.get("summary") or ""),
                actor_user_id=session.user_id,
                links=tuple(payload.get("links") or []))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
                TimelineError) as exc:
            self._v1_response(error_envelope(
                "invalid_timeline_event", str(exc), request_id=request_id), status=400)
            return
        self._v1_response(success_envelope(
            {"event": asdict(event)}, request_id=request_id), status=201)

    def _handle_v1_room_create(
        self, organization_id: str, workspace_id: str,
    ) -> None:
        from .external_rooms import ExternalRoomDirectory, ExternalRoomError

        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        session = getattr(self, "authenticated_session", None)
        if session is None:
            self._v1_response(error_envelope(
                "professional_session_required", "A professional session is required",
                request_id=request_id), status=403)
            return
        try:
            payload = self._read_v1_json_object()
            if payload is None:
                return
            assert self.daemon.store is not None
            room = ExternalRoomDirectory(self.daemon.store).create(
                organization_id, workspace_id, str(payload.get("name") or ""),
                created_by=session.user_id,
                permissions=tuple(payload.get("permissions") or
                                  ("read_shared", "comment")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
                ExternalRoomError) as exc:
            self._v1_response(error_envelope(
                "invalid_external_room", str(exc), request_id=request_id), status=400)
            return
        self._v1_response(success_envelope(
            {"room": asdict(room)}, request_id=request_id), status=201)

    def _handle_v1_board_list(
        self, query: dict[str, list[str]], organization_id: str = "",
        workspace_id: str = "",
    ) -> None:
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        try:
            limit_values = query.get("limit")
            cursor_values = query.get("cursor")
            limit_value: str | None = limit_values[0] if limit_values else None
            cursor: str | None = cursor_values[0] if cursor_values else None
            limit = normalize_limit(limit_value)
            cards = self.daemon.board_list(
                organization_id if organization_id else None,
                workspace_id if workspace_id else None)["cards"]
            version = resource_version(cards)
            items, next_cursor = page_items(
                cards, limit=limit, cursor=cursor,
                secret=self.daemon.api_cursor_secret, snapshot=version,
            )
        except ValueError as exc:
            self._v1_response(
                error_envelope("invalid_request", str(exc), request_id=request_id),
                status=400,
            )
            return
        if self.headers.get("If-None-Match") == etag(version):
            self.send_response(304)
            self.send_header("X-API-Version", API_VERSION)
            self.send_header("ETag", etag(version))
            self.end_headers()
            return
        payload = success_envelope(
            {"items": items}, request_id=request_id,
            meta={"next_cursor": next_cursor, "resource_version": version},
        )
        self._v1_response(payload, headers={"ETag": etag(version)})

    def _handle_v1_board_create(
        self, organization_id: str = "", workspace_id: str = "",
    ) -> None:
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        try:
            declared_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            declared_length = -1
        if declared_length < 0 or declared_length > MAX_JSON_BODY_BYTES:
            self._v1_response(
                error_envelope(
                    "payload_too_large",
                    f"JSON body must be at most {MAX_JSON_BODY_BYTES} bytes",
                    request_id=request_id,
                ), status=413)
            return
        try:
            payload = json.loads(
                self._read_body(max_bytes=MAX_JSON_BODY_BYTES).decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._v1_response(
                error_envelope("invalid_json", "Request body must be valid JSON",
                               request_id=request_id), status=400)
            return
        if not isinstance(payload, dict):
            self._v1_response(
                error_envelope("invalid_request", "Request body must be a JSON object",
                               request_id=request_id), status=400)
            return
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or title).strip()
        if not goal:
            self._v1_response(
                error_envelope("invalid_request", "title or goal is required",
                               request_id=request_id,
                               details={"field": "title"}), status=400)
            return

        key = str(self.headers.get("Idempotency-Key") or "").strip()
        if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            self._v1_response(
                error_envelope(
                    "invalid_request",
                    f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters",
                    request_id=request_id,
                ), status=400)
            return
        fingerprint = resource_version({"title": title, "goal": goal})
        result, replayed, conflict = self.daemon.api_idempotent_board_create(
            key, fingerprint, title, goal, organization_id, workspace_id)
        if conflict:
            self._v1_response(
                error_envelope(
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request",
                    request_id=request_id,
                ), status=409)
            return
        card = result.get("card")
        if not isinstance(card, dict):
            self._v1_response(
                error_envelope("invalid_request", str(result.get("error") or "invalid card"),
                               request_id=request_id), status=400)
            return
        version = resource_version(card)
        response_headers = {"ETag": etag(version)}
        if replayed:
            response_headers["Idempotency-Replayed"] = "true"
        self._v1_response(
            success_envelope({"card": card}, request_id=request_id,
                             meta={"resource_version": version}),
            status=200 if replayed else 201, headers=response_headers,
        )

    def _v1_response(self, payload: dict, status: int = 200,
                     headers: dict[str, str] | None = None) -> None:
        self._json_response(
            payload, status=status,
            headers={"X-API-Version": API_VERSION, **(headers or {})},
        )

    def _json_response(self, payload: dict, status: int = 200,
                       headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error_response(self, exc: Exception) -> None:
        if split_url(self.path).path.startswith("/api/v1/"):
            request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
            self._v1_response(
                error_envelope(
                    "internal_error", "The request could not be completed",
                    request_id=request_id,
                ), status=500)
            return
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
        heartbeat_interval: float = 600.0,
        max_consecutive_errors: int = 10,
        status_host: str = _DEFAULT_HOST,
        status_port: int | None = None,
        work_dir: str | None = None,
        max_upload_bytes: int | None = None,
    ) -> None:
        self.store = store
        self.agent = agent
        self._mcp_clients: list = []
        self.manager = manager or (TaskManager(store) if store else None)
        self.llm = llm or LLMClient()
        self.tick_interval = tick_interval
        self.idle_interval = idle_interval
        # How often to run a full proactive heartbeat when the queue is empty.
        # Default 10 minutes — was previously every idle tick (30s), which
        # hammered cloud providers with "scan for urgent follow-ups" LLM cycles.
        self.heartbeat_interval = max(0.0, float(heartbeat_interval))
        self.max_consecutive_errors = max_consecutive_errors
        self.status_host = status_host
        self.status_port = status_port or _find_port(status_host)
        self.work_dir = work_dir
        self.max_upload_bytes = (
            max_upload_bytes if max_upload_bytes is not None
            else _env_int("PRAXIS_MAX_UPLOAD_BYTES", _MAX_UPLOAD_BYTES)
        )
        self.log = get_logger("praxis.daemon")
        self.running = False
        self._stop_event = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._consecutive_errors = 0
        self._last_heartbeat_ts = 0.0
        self._heartbeat_backoff_until = 0.0
        self._log_buffer: list[str] = []
        # Serializes handlers sharing this daemon; SQLite BEGIN IMMEDIATE below
        # provides the cross-process transaction boundary.
        self._api_idempotency_lock = threading.Lock()
        self._approval_lock = threading.Lock()
        self._task_approvals_initialized = False
        self.api_cursor_secret = uuid.uuid4().bytes
        # Open SSE subscriber queues, one per /events connection. Guarded by
        # _sse_lock because request handlers run on independent threads.
        self._sse_clients: list[Queue[dict[str, _T] | None]] = []
        self._sse_lock = threading.Lock()
        self.state = _read_state()
        self.state.running = False
        self._setup_signal_handlers()
        if self.store is not None and self.agent is not None and self.manager is not None:
            self._initialize_task_approval_state()

    def api_idempotent_board_create(
        self, key: str, fingerprint: str, title: str, goal: str,
        organization_id: str = "", workspace_id: str = "",
    ) -> tuple[dict[str, Any], bool, bool]:
        """Atomically replay or create a board card for an idempotency key.

        Returns ``(result, replayed, conflict)``. Holding the lock across the
        store write prevents concurrent HTTP handlers from duplicating effects.
        """
        if not key:
            return self.board_create(
                title, goal, organization_id, workspace_id), False, False
        with self._api_idempotency_lock:
            self._ensure_agent()
            assert self.store is not None
            card, replayed, conflict = self.store.idempotent_add_card(
                f"{organization_id}:{workspace_id}:{key}", fingerprint,
                f"card-{uuid.uuid4().hex[:10]}", title, goal,
                max_receipts=MAX_IDEMPOTENCY_RECEIPTS,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            return ({"card": card} if card else {}), replayed, conflict

    def work_dir_upload(self, filename: str) -> Path:
        base = self.work_dir or os.environ.get("PRAXIS_WORK_DIR") or os.getcwd()
        root = Path(base).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root / Path(filename).name

    def _sse_subscribe(self) -> "Queue[dict[str, _T] | None]":
        q: "Queue[dict[str, _T] | None]" = Queue(maxsize=_SSE_QUEUE_MAXSIZE)
        with self._sse_lock:
            self._sse_clients.append(q)
        return q

    def _sse_unsubscribe(self, q: "Queue[dict[str, _T] | None]") -> None:
        with self._sse_lock:
            try:
                self._sse_clients.remove(q)
            except ValueError:
                pass

    def _sse_client_count(self) -> int:
        with self._sse_lock:
            return len(self._sse_clients)

    def _close_sse_clients(self) -> None:
        """Unblock and drop every open SSE connection (used on shutdown)."""
        with self._sse_lock:
            clients = list(self._sse_clients)
            self._sse_clients.clear()
        for q in clients:
            _offer(q, None)

    def emit_event(self, event_type: str, payload: dict[str, _T]) -> None:
        event = {"type": event_type, "ts": time.time(), "payload": payload}
        with self._sse_lock:
            clients = list(self._sse_clients)
        for q in clients:
            _offer(q, event)

    @classmethod
    def from_env(cls, work_dir: str | None = None,
                 autonomous_risks: set[RiskClass] | None = None,
                 status_port: int | None = None,
                 status_host: str | None = None) -> "Daemon":
        store = Store.open()
        agent = PraxisAgent.persistent(llm=LLMClient(), work_dir=work_dir)
        if autonomous_risks is not None:
            agent.broker.policy.autonomous_risks = set(autonomous_risks)
        # First-run bootstrap (idempotent): recall defaults + starter knowledge.
        try:
            from . import bootstrap
            bootstrap.run(store)
        except Exception:
            pass
        host = status_host or os.environ.get("PRAXIS_HOST", _DEFAULT_HOST)
        return cls(store=store, agent=agent, status_port=status_port,
                   status_host=host)

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except Exception:
                pass

    def _on_signal(self, _signum, _frame) -> None:
        self.log.info("shutdown signal received")
        self.stop()

    def _bind_durable_surfaces(self) -> None:
        """Wire Store into channel threads + evolution proposals (survive restart)."""
        if self.store is None:
            return
        try:
            from . import channels_inbound, growth
            channels_inbound.set_thread_store(self.store)
            growth.set_proposal_store(self.store)
        except Exception:
            pass

    def _ensure_agent(self) -> None:
        if self.agent is not None:
            self._bind_durable_surfaces()
            self._initialize_task_approval_state()
            return
        if self.store is not None:
            self.agent = PraxisAgent(llm=self.llm, store=self.store)
        else:
            self.agent = PraxisAgent.persistent(llm=self.llm, work_dir=self.work_dir)
            self.store = self.agent.store
        if self.manager is None:
            self.manager = TaskManager(self.store)
        self._bind_durable_surfaces()
        # First-run bootstrap: enable recall defaults and seed the starter
        # knowledge namespace so even an offline-mock daemon is usable (and the
        # Knowledge panel + grounded ask have content) on first boot. Idempotent.
        try:
            from . import bootstrap
            bootstrap.run(self.store)
        except Exception:
            pass
        # Ensure broker allowlist covers whatever registry the agent built.
        self.agent.broker.policy.allowed_tools.update(self.agent.registry.names())
        # Opt-in: expose configured external MCP servers' tools to the governed
        # loop. No servers configured -> no-op; any failure is swallowed so chat
        # always works even if a server is misconfigured or down.
        try:
            from .mcp_client import augment_registry_with_mcp
            _tools, self._mcp_clients = augment_registry_with_mcp(
                self.agent.registry,
                allowlist=self.agent.broker.policy.allowed_tools)
        except Exception:
            self._mcp_clients = []
        self._initialize_task_approval_state()

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
        self._close_sse_clients()
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
        # Non-loopback binds: ensure a shared token exists so the dashboard is
        # not accidentally open on the LAN (token is in agents.auth or env).
        try:
            from . import auth_gate
            if auth_gate.auth_required(self.status_host):
                tok = auth_gate.ensure_token()
                self._log(
                    "warning",
                    "non-loopback bind: session auth required "
                    f"(token length {len(tok)}; set PRAXIS_AUTH_TOKEN to override)")
        except Exception as exc:
            self._log("warning", f"auth setup: {exc}")
        self._start_status_server()
        self._log("info", f"daemon started on {self.status_host}:{self.status_port}")
        try:
            while self.running and not self._stop_event.is_set():
                self.tick()
                self._cron_tick()
                self._consolidation_tick()
                self._compliance_autorevert()
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

    def _compliance_autorevert(self) -> None:
        """Promptly flip an expired timed relaxation back to enforced even when the
        daemon is otherwise idle. authorize() is fail-safe regardless, but this
        keeps the persisted state and dashboard honest."""
        if self.agent is None:
            return
        try:
            self.agent.broker.effective_mode()
        except Exception:
            pass

    def _cron_tick(self) -> None:
        """Run any cron jobs that are due, then reschedule them. Each job runs
        through the same governed loop as a manual run; failures are isolated so
        one bad job can't break the scheduler or the daemon."""
        if self.store is None:
            return
        try:
            from .cron import CronScheduler
            sched = CronScheduler(self.store)
            due = sched.claim() or []
        except Exception as exc:
            self._log("error", f"cron tick error: {exc}")
            return
        for job in due:
            jid = job["job_id"]
            try:
                out = self._run_cron_job(job)
                sched.reschedule(jid, "ok", out)
                self.emit_event("cron", {"job_id": jid, "status": "ok",
                                         "name": job.get("name", "")})
            except Exception as exc:
                sched.reschedule(jid, "error", str(exc))
                self._log("error", f"cron job {jid} failed: {exc}")
                self.emit_event("cron", {"job_id": jid, "status": "error",
                                         "error": str(exc)})
        # Inbound Telegram (when enabled) — poll between cron passes.
        try:
            self._telegram_poll_tick()
        except Exception as exc:
            self._log("warning", f"telegram poll: {exc}")

    def _consolidation_tick(self) -> None:
        """Active memory consolidation — the "sleep" pass. Gated by
        ``agents.consolidation.enabled`` (default off in v0.28.0). When off,
        this method is a cheap no-op (one config read + one timestamp check).

        When on: on a configurable interval, read recent episodic + durable
        memory, extract entities/topics, find connections, synthesize one
        cross-cutting insight, re-rate salience. READ-risk only — no external
        effect. Defers when the task queue has pending work so it never
        starves the user-facing loop. See hybridagent/consolidation.py and
        praxis-consolidation-phase-plan.md."""
        try:
            from .config import get_consolidation_config
            cc = get_consolidation_config()
        except Exception:
            return
        if not cc.get("enabled", False):
            return
        now = time.time()
        if now < getattr(self, "_next_consolidation_ts", 0.0):
            return
        # Don't starve the task queue — defer if work is pending/running.
        if self.manager is not None and (
            self.manager.list(status="pending") or self.manager.list(status="retry")
        ):
            self._next_consolidation_ts = now + 60.0
            return
        try:
            from .consolidation import MemoryConsolidator
            assert self.agent is not None and self.store is not None
            consolidator = MemoryConsolidator(
                self.agent.memory, self.agent.llm, self.store,
                window_size=int(cc.get("windowSize", 20)),
                min_items=int(cc.get("minItemsToConsolidate", 3)),
                max_connections=int(cc.get("maxConnections", 5)),
                rerate_salience=bool(cc.get("rerateSalience", True)),
                extract_metadata=bool(cc.get("extractMetadata", True)),
            )
            report = consolidator.run()
            interval = float(cc.get("intervalMinutes", 30)) * 60.0
            self._next_consolidation_ts = now + interval
            self.emit_event("consolidation", report.as_dict())
            self._log("info", f"consolidation: {report.as_dict()}")
        except Exception as exc:
            self._log("error", f"consolidation tick error: {exc}")
            # back off on error — cap at 5 min so a broken pass doesn't
            # immediately retry and burn tokens
            self._next_consolidation_ts = now + min(
                float(cc.get("intervalMinutes", 30)) * 60.0, 300.0)

    def _run_cron_job(self, job: dict) -> str:
        """Execute one cron job's goal in its configured mode, deliver the result,
        and return a short text summary (also stored as the job's last_output)."""
        mode = job.get("mode", "do")
        goal = job["goal"]
        self._log("info", f"cron firing {job['job_id']} ({mode}): {goal[:60]}")
        if mode == "research":
            res = self.research(goal)
            text = res.get("text", "")
        elif mode == "ask":
            ans = self.ask(goal)
            text = getattr(ans, "text", str(ans))
        elif mode == "agent":
            res = self.agent_run(goal)
            text = res.get("summary", "") or str(res.get("status", ""))
        elif mode == "pulse":
            dig = self.pulse(target=job.get("deliver") if job.get("deliver") != "local" else None)
            text = dig.get("text", "")
            return text  # pulse already delivers when target set
        else:  # "do" — queue a durable task (the existing autonomy path)
            task_id = self.submit(goal)
            text = f"queued task {task_id}"
        self._deliver_cron(job, text)
        return text

    def _deliver_cron(self, job: dict, text: str) -> None:
        """Deliver a cron result to its target. 'local' just records it (visible
        in the dashboard/CLI); a gateway target routes through the messaging
        layer when configured. Unknown/again-local targets are a silent no-op."""
        target = (job.get("deliver") or "local").strip()
        if not target or target == "local":
            return
        try:
            from .gateways import deliver as gw_deliver
            gw_deliver(target, text, store=self.store)
        except Exception as exc:
            self._log("warning", f"cron delivery to {target!r} failed: {exc}")

    # --------------------------------------------------------------- cron API
    def cron_list(self) -> dict:
        if self.store is None:
            return {"jobs": []}
        return {"jobs": self.store.list_cron_jobs()}

    def cron_create(self, goal: str, schedule: str, name: str = "",
                    mode: str = "do", deliver: str = "local") -> dict:
        self._ensure_agent()
        assert self.store is not None
        from .cron import CronScheduler
        goal = (goal or "").strip()
        schedule = (schedule or "").strip()
        if not goal or not schedule:
            return {"error": "goal and schedule are required"}
        job = CronScheduler(self.store).create(
            goal, schedule, name=name, mode=mode, deliver=deliver)
        if job and "error" not in job:
            self._log("info", f"cron job created: {job['job_id']} ({schedule})")
        return job if job else {"error": "could not create cron job"}

    def cron_delete(self, job_id: str) -> dict:
        if self.store is None:
            return {"error": "no store"}
        return {"deleted": bool(self.store.delete_cron_job((job_id or "").strip()))}

    def cron_set_enabled(self, job_id: str, enabled: bool) -> dict:
        if self.store is None:
            return {"error": "no store"}
        from .cron import CronScheduler
        ok = CronScheduler(self.store).set_enabled((job_id or "").strip(), enabled)
        return {"updated": ok, "enabled": enabled}

    def _shutdown(self) -> None:
        self.running = False
        self.state.running = False
        self.state.stopped_ts = time.time()
        _write_state(self.state)
        _remove_pid()
        self._stop_status_server()
        self._close_mcp_clients()
        self._log("info", "daemon stopped")

    def _close_mcp_clients(self) -> None:
        """Terminate any external MCP server subprocesses spawned for the agent."""
        for client in self._mcp_clients:
            try:
                client.close()
            except Exception:
                pass
        self._mcp_clients = []

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self._stop_event.set()

    # ---------------------------------------------------------------------- tick
    def tick(self) -> None:
        """Process one ready task. If none are ready, optionally run a throttled heartbeat."""
        if self.agent is None:
            self._ensure_agent()
        assert self.agent is not None
        self.state.last_tick_ts = time.time()
        self.state.cycles += 1
        try:
            task = self._next_task()
            if task is None:
                # Idle: do NOT burn cloud tokens every idle_interval. Heartbeats
                # are throttled (default 10 min) and skipped while the provider is
                # in error backoff after timeouts/overloads.
                now = time.time()
                if (now >= getattr(self, "_heartbeat_backoff_until", 0)
                        and (now - getattr(self, "_last_heartbeat_ts", 0))
                        >= getattr(self, "heartbeat_interval", 600.0)):
                    self._log("debug", "no ready tasks; running throttled heartbeat")
                    self.agent.heartbeat(refresh_wiki=False)
                    self._last_heartbeat_ts = now
                    self._heartbeat_backoff_until = 0.0
                else:
                    self._log("debug", "no ready tasks; idle")
                self._consecutive_errors = 0
                # Bound the error ring so status payloads stay small.
                if len(self.state.errors) > 20:
                    self.state.errors = self.state.errors[-20:]
                _write_state(self.state)
                return
            self._run_task(task)
            self._consecutive_errors = 0
            self._heartbeat_backoff_until = 0.0
        except Exception as exc:
            self._consecutive_errors += 1
            msg = f"tick error: {exc}"
            self.state.errors.append(msg)
            if len(self.state.errors) > 20:
                self.state.errors = self.state.errors[-20:]
            self._log("error", msg)
            # Back off heartbeats on provider flake so a dead cloud model does
            # not keep hammering every idle cycle until max_consecutive_errors.
            err_l = str(exc).lower()
            if any(tok in err_l for tok in (
                "timed out", "timeout", "overloaded", "503", "429",
                "remote end closed", "connection",
            )):
                # Exponential-ish backoff: 1m, 2m, 4m … capped at 15m.
                delay = min(900.0, 60.0 * (2 ** min(self._consecutive_errors - 1, 4)))
                self._heartbeat_backoff_until = time.time() + delay
                self._log("warning",
                          f"provider flake — heartbeat backoff {int(delay)}s")
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
        assert self.manager is not None
        block = self._budget_block()
        if block:
            self.manager.store.update_task(
                task.task_id, status="failed",
                error=block["error"], output=block["error"])
            self.state.tasks_failed += 1
            self.emit_event("task", {
                "task_id": task.task_id, "status": "failed",
                "goal": task.goal, "output": block["error"],
                "error": block["error"],
            })
            self._log("warning", f"task {task.task_id} blocked: budget cap")
            return
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
        """Enqueue a new task. Safe to call from the CLI while the daemon runs.

        Raises ``RuntimeError`` when the spend budget cap is reached so callers
        (HTTP / CLI) can surface a clear blocked response without creating a
        doomed task.
        """
        block = self._budget_block()
        if block:
            raise RuntimeError(block["error"])
        if self.agent is None:
            self._ensure_agent()
        assert self.manager is not None
        task = self.manager.create(goal, max_attempts=max_attempts)
        self._log("info", f"submitted task {task.task_id}")
        return task.task_id

    def agent_run(self, goal: str, max_replans: int = 1) -> dict:
        """A2A: plan + execute a goal under governance, returning a JSON result.

        Records a durable, replayable run trace and pushes live ``run`` events to
        the dashboard SSE bus so the Run Graph can render the governed loop live.
        Enforces the spend budget: if the cap is reached the run is blocked (and an
        ``alert`` event is pushed) rather than executed — cost *control*, not just
        cost visibility. An engaged kill-switch blocks the run outright (before any
        planning or inference), not just its consequential tools.
        """
        self._ensure_agent()
        assert self.agent is not None
        from .agent_service import AgentService
        if self.agent.broker.kill.tripped:
            self.emit_event("alert", {"kind": "kill_switch_engaged"})
            self._log("warning", "agent run blocked: kill-switch engaged")
            return {"goal": goal, "status": "blocked",
                    "summary": "kill-switch engaged; release it to run",
                    "replans": 0, "steps": [], "held_approvals": [],
                    "run_id": "", "blocked": True}
        store = self.store
        if store is not None:
            b = store.get_budget()
            if b["limit_usd"] > 0 and b["spent_usd"] >= b["limit_usd"]:
                self.emit_event("alert", {"kind": "budget_exceeded",
                                          "spent_usd": b["spent_usd"],
                                          "limit_usd": b["limit_usd"]})
                self._log("warning", "agent run blocked: budget cap reached")
                return {"goal": goal, "status": "blocked",
                        "summary": "budget cap reached; raise or reset the budget",
                        "replans": 0, "steps": [], "held_approvals": [],
                        "run_id": "", "blocked": True}
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        if store is not None:
            try:
                store.start_run(run_id, goal, kind="plan")
            except Exception:
                pass

        def on_event(kind: str, data: dict) -> None:
            if store is not None:
                try:
                    store.add_run_event(run_id, kind, data,
                                        node_id=str(data.get("id", "")))
                except Exception:
                    pass
            self.emit_event("run", {"run_id": run_id, "kind": kind, "data": data})

        if hasattr(self.agent.llm, "reset_usage"):
            self.agent.llm.reset_usage()
        result = AgentService(self.agent).run(
            goal, max_replans=max_replans, on_event=on_event)
        usage: dict = {"prompt_tokens": 0, "completion_tokens": 0,
                       "cost_usd": 0.0, "calls": 0, "model": ""}
        if hasattr(self.agent.llm, "usage_snapshot"):
            usage = self.agent.llm.usage_snapshot()
        if store is not None:
            try:
                store.finish_run(run_id, str(result.get("status", "")))
                # Accrue real provider cost (tokens x per-model price). Local and
                # mock models are free, so an offline run adds a run but $0 spend.
                store.add_spend(round(float(usage.get("cost_usd", 0.0)), 6))
                # Persist the run's routing decision so the dashboard can show
                # which model handled it, local-vs-cloud, tokens, cost, fallbacks.
                from .router import ModelRouter
                primary = str(usage.get("model") or "mock")
                store.record_run_routing(
                    run_id, primary,
                    int(usage.get("prompt_tokens", 0) or 0),
                    int(usage.get("completion_tokens", 0) or 0),
                    float(usage.get("cost_usd", 0.0) or 0.0),
                    int(usage.get("calls", 0) or 0),
                    ModelRouter.is_local_ref(primary),
                    int(usage.get("fallbacks", 0) or 0),
                    int(usage.get("escalations", 0) or 0),
                    str(usage.get("escalation_reason", "") or ""))
            except Exception:
                pass
        result["run_id"] = run_id
        result["usage"] = usage
        return result

    def list_runs_trace(self, limit: int = 50) -> dict:
        """Recent run traces for the dashboard Run Graph (durable + replayable)."""
        if self.store is None:
            return {"runs": []}
        return {"runs": self.store.list_runs(limit=limit)}

    def get_run_trace(self, run_id: str) -> dict:
        """Full event timeline for one run, for DAG rendering and replay."""
        if self.store is None:
            return {"run": None, "events": []}
        return {"run": self.store.get_run(run_id),
                "events": self.store.list_run_events(run_id)}

    # ------------------------------------------------------------- work board
    _BOARD_LANES = ("backlog", "planned", "running", "held", "done", "failed")

    def board_list(self, organization_id: str | None = "",
                   workspace_id: str | None = "") -> dict:
        """Cards + lane vocabulary for the governed Work Board."""
        if self.store is None:
            return {"cards": [], "lanes": list(self._BOARD_LANES)}
        return {"cards": self.store.list_cards(
                    organization_id=organization_id, workspace_id=workspace_id),
                "lanes": list(self._BOARD_LANES)}

    def board_create(self, title: str, goal: str = "",
                     organization_id: str = "", workspace_id: str = "") -> dict:
        self._ensure_agent()
        assert self.store is not None
        title = (title or goal or "").strip()
        goal = (goal or title).strip()
        if not goal:
            return {"error": "title/goal required"}
        card_id = f"card-{uuid.uuid4().hex[:10]}"
        self.store.add_card(card_id, title, goal, lane="backlog",
                            organization_id=organization_id,
                            workspace_id=workspace_id)
        return {"card": self.store.get_card(card_id, workspace_id=workspace_id or None)}

    def board_move(self, card_id: str, lane: str,
                   organization_id: str = "", workspace_id: str = "") -> dict:
        if self.store is None:
            return {"error": "no store"}
        if lane not in self._BOARD_LANES:
            return {"error": f"invalid lane '{lane}'"}
        if self.store.get_card(card_id, workspace_id, organization_id) is None:
            return {"error": "card not found"}
        self.store.move_card(card_id, lane, organization_id=organization_id,
                             workspace_id=workspace_id)
        return {"card": self.store.get_card(card_id, workspace_id, organization_id)}

    def board_run(self, card_id: str, organization_id: str = "",
                  workspace_id: str = "") -> dict:
        """Execute a card's goal under governance and reflect the verdict back onto
        the card's lane (done / held / failed) — the kanban *is* the workflow."""
        self._ensure_agent()
        assert self.store is not None
        card = self.store.get_card(card_id, workspace_id, organization_id)
        if card is None:
            return {"error": "card not found"}
        self.store.move_card(card_id, "running", organization_id=organization_id,
                             workspace_id=workspace_id)
        result = self.agent_run(card["goal"])
        lane = {"completed": "done", "partial": "done", "needs_approval": "held",
                "failed": "failed"}.get(str(result.get("status", "")), "done")
        self.store.set_card_run(
            card_id, str(result.get("run_id", "")),
            str(result.get("status", "")), lane,
            organization_id=organization_id, workspace_id=workspace_id)
        return {"card": self.store.get_card(
            card_id, workspace_id, organization_id), "result": result}

    def board_delete(self, card_id: str, organization_id: str = "",
                     workspace_id: str = "") -> dict:
        if self.store is None:
            return {"error": "no store"}
        deleted = self.store.delete_card(
            card_id, organization_id=organization_id, workspace_id=workspace_id)
        return {"deleted": card_id if deleted else ""}

    # ----------------------------------------------------------- safety center
    def deny_approval(self, approval_id: str) -> bool:
        """Reject a held consequential action (it is never executed).

        Returns True only when the approval id was actually pending.
        """
        self._ensure_agent()
        assert self.agent is not None
        if self.store is not None and self.store.has_task_approval_action(approval_id):
            if approval_id not in self.agent.broker.pending:
                return False
            outcome = self.store.reject_task_approval_action(approval_id)
            self.agent.broker.pending.pop(approval_id, None)
            self._apply_task_action_reconciliation(outcome)
            return bool(outcome.get("transitions"))
        return bool(self.agent.broker.reject(approval_id))

    def killswitch_status(self) -> dict:
        self._ensure_agent()
        assert self.agent is not None
        return {"engaged": bool(self.agent.broker.kill.tripped)}

    def killswitch_set(self, engaged: bool) -> dict:
        """Engage/release the broker kill-switch; engaged denies all consequential
        actions until released."""
        self._ensure_agent()
        assert self.agent is not None
        if engaged:
            self.agent.broker.kill.trip()
        else:
            self.agent.broker.kill.reset()
        self._log("warning" if engaged else "info",
                  f"kill-switch {'engaged' if engaged else 'released'} via dashboard")
        return {"engaged": bool(self.agent.broker.kill.tripped)}

    def compliance_status(self) -> dict:
        """Current governance posture + the selectable modes for the dashboard."""
        from .broker import COMPLIANCE_MODES
        self._ensure_agent()
        assert self.agent is not None
        broker = self.agent.broker
        current = broker.effective_mode().value   # also auto-reverts on expiry
        expires_ts = broker.mode_expires_ts
        expires_in = (max(0, int(expires_ts - time.time()))
                      if expires_ts is not None else None)
        labels = {
            "enforced": ("Enforced",
                         "Send & destructive actions are held for your approval. "
                         "All guards on. (default)"),
            "autonomous": ("Autonomous",
                           "Send & destructive run without approval. Egress "
                           "firewall, injection detection & kill-switch stay on."),
            "permissive": ("Permissive",
                           "All guards off except the kill-switch. For trusted or "
                           "sandboxed environments (e.g. isolated coding)."),
        }
        return {
            "mode": current,
            "expires_ts": expires_ts,
            "expires_in_seconds": expires_in,
            "modes": [
                {"id": m.value, "label": labels[m.value][0],
                 "description": labels[m.value][1], "active": m.value == current}
                for m in COMPLIANCE_MODES
            ],
        }

    def compliance_set(self, mode: str, ttl_seconds: float | None = None) -> dict:
        """Set the governance compliance mode (persisted). 'enforced' is the
        locked-down default; 'autonomous'/'permissive' relax the approval gate.
        Optional ttl_seconds schedules an automatic revert to enforced. The
        kill-switch is independent and always overrides regardless of mode."""
        from .broker import COMPLIANCE_MODES
        self._ensure_agent()
        assert self.agent is not None
        if mode not in {m.value for m in COMPLIANCE_MODES}:
            return {"error": f"unknown compliance mode '{mode}'"}
        ttl: float | None = None
        if ttl_seconds is not None:
            try:
                ttl = float(ttl_seconds)
            except (TypeError, ValueError):
                return {"error": "ttl_seconds must be a number"}
            if ttl <= 0:
                ttl = None
            elif ttl > 604800:        # cap at 7 days to avoid a runaway relaxation
                ttl = 604800.0
        applied = self.agent.broker.set_mode(mode, ttl_seconds=ttl)
        window = f" for {int(ttl)}s" if ttl else ""
        self._log("warning" if applied.value != "enforced" else "info",
                  f"compliance mode set to '{applied.value}'{window} via dashboard")
        return self.compliance_status()

    def audit_log(self, limit: int = 100) -> dict:
        """Recent governed decisions for the audit viewer (secrets pre-redacted)."""
        if self.store is None:
            return {"entries": []}
        return {"entries": self.store.list_audit(limit=limit)}

    # ----------------------------------------------------------- observability
    def metrics(self) -> dict:
        """Aggregate eval + governance + run metrics for the observability panel."""
        if self.store is None:
            return {"evals": [],
                    "decisions": {"by_verdict": {}, "by_rule": {}, "total": 0},
                    "runs": {"by_status": {}, "total": 0},
                    "routing": {"total_cost_usd": 0.0, "total_tokens": 0,
                                "total_runs": 0, "local_runs": 0,
                                "by_model": [], "trend": []}}
        return {
            "evals": list(reversed(self.store.list_eval_runs(limit=20))),
            "decisions": self.store.audit_stats(),
            "runs": self.store.run_stats(),
            "routing": self.store.routing_cost_stats(),
        }

    # ------------------------------------------------------- inference control
    def _budget_block(self) -> dict | None:
        """Return a blocked payload when the spend cap is reached; else None.

        Used by chat, ask, research, submit, and agent_run so the cap is a hard
        stop everywhere — not only on A2A agent_run.
        """
        if self.store is None:
            return None
        try:
            b = self.store.get_budget()
        except Exception:
            return None
        if not (b.get("limit_usd", 0) > 0 and b.get("spent_usd", 0) >= b["limit_usd"]):
            return None
        self.emit_event("alert", {"kind": "budget_exceeded",
                                  "spent_usd": b["spent_usd"],
                                  "limit_usd": b["limit_usd"]})
        self._log("warning", "blocked: budget cap reached")
        return {
            "blocked": True,
            "error": (
                f"Budget cap reached (${b['spent_usd']:.4f} / "
                f"${b['limit_usd']:.2f}). Raise or reset the budget in "
                "Inference Control before continuing."
            ),
            "spent_usd": b["spent_usd"],
            "limit_usd": b["limit_usd"],
        }

    def budget_status(self) -> dict:

        if self.store is None:
            return {"limit_usd": 0.0, "spent_usd": 0.0, "runs": 0, "over": False}
        b = self.store.get_budget()
        b["over"] = bool(b["limit_usd"] > 0 and b["spent_usd"] >= b["limit_usd"])
        return b

    def budget_set(self, limit_usd: float) -> dict:
        self._ensure_agent()
        assert self.store is not None
        self.store.set_budget_limit(limit_usd)
        return self.budget_status()

    def budget_reset(self) -> dict:
        self._ensure_agent()
        assert self.store is not None
        self.store.reset_budget()
        return self.budget_status()

    def inference_info(self) -> dict:
        """Model + provider, role-routing vocabulary + learned-router state, and
        the live spend budget — the Inference Control Center payload."""
        from .orchestrator import ROLE_TO_TOOLS
        router = {"roles": sorted(ROLE_TO_TOOLS), "trained": False, "n_samples": 0}
        if self.store is not None:
            rm = self.store.load_router_model()
            if rm:
                router["trained"] = True
                router["n_samples"] = int(rm.get("n_samples", 0))
        routes = self.store.list_run_routing(limit=12) if self.store is not None else []
        return {"model": self.model_info(), "router": router,
                "budget": self.budget_status(), "routes": routes}

    # ----------------------------------------------------------- memory studio
    _MEM_TIERS = ("working", "episodic", "durable")

    def memory_list(self) -> dict:
        if self.store is None:
            return {"items": [], "by_tier": {}, "tiers": list(self._MEM_TIERS)}
        items = self.store.list_memory()
        by_tier: dict[str, int] = {}
        for it in items:
            by_tier[it["tier"]] = by_tier.get(it["tier"], 0) + 1
        return {"items": items, "by_tier": by_tier, "tiers": list(self._MEM_TIERS)}

    def memory_add(self, tier: str, text: str, provenance: str = "dashboard") -> dict:
        self._ensure_agent()
        assert self.store is not None
        tier = tier if tier in self._MEM_TIERS else "durable"
        text = (text or "").strip()
        if not text:
            return {"error": "text required"}
        mid = self.store.add_memory(tier, text, provenance or "dashboard",
                                    kind="note")
        return {"id": mid, "tier": tier}

    def memory_delete(self, memory_id: int) -> dict:
        if self.store is None:
            return {"error": "no store"}
        return {"deleted": bool(self.store.delete_memory(int(memory_id)))}

    # ------------------------------------------- active memory consolidation
    def consolidation_status(self) -> dict:
        """Report consolidation config + last-run state. Used by
        ``praxis consolidation status`` and the dashboard Mind pane."""
        from .config import get_consolidation_config
        cc = get_consolidation_config()
        status: dict = {
            "enabled": bool(cc.get("enabled", False)),
            "intervalMinutes": int(cc.get("intervalMinutes", 30)),
            "windowSize": int(cc.get("windowSize", 20)),
            "minItemsToConsolidate": int(cc.get("minItemsToConsolidate", 3)),
            "maxConnections": int(cc.get("maxConnections", 5)),
            "rerateSalience": bool(cc.get("rerateSalience", True)),
            "extractMetadata": bool(cc.get("extractMetadata", True)),
            "next_run_ts": getattr(self, "_next_consolidation_ts", 0.0),
            "pending": 0,
        }
        if self.store is not None:
            try:
                status["pending"] = len(self.store.list_unconsolidated(limit=1000))
            except Exception:
                status["pending"] = 0
        return status

    def consolidation_run(self) -> dict:
        """Manually trigger one consolidation pass. Respects the ``enabled``
        gate — if consolidation is off, returns a notice instead of running
        (mirrors the CLI behavior; don't silently run an off feature)."""
        from .config import get_consolidation_config
        cc = get_consolidation_config()
        if not cc.get("enabled", False):
            return {"error": "consolidation is disabled (agents.consolidation.enabled=false)"}
        try:
            from .consolidation import MemoryConsolidator
            self._ensure_agent()
            assert self.agent is not None and self.store is not None
            consolidator = MemoryConsolidator(
                self.agent.memory, self.agent.llm, self.store,
                window_size=int(cc.get("windowSize", 20)),
                min_items=int(cc.get("minItemsToConsolidate", 3)),
                max_connections=int(cc.get("maxConnections", 5)),
                rerate_salience=bool(cc.get("rerateSalience", True)),
                extract_metadata=bool(cc.get("extractMetadata", True)),
            )
            report = consolidator.run()
            self.emit_event("consolidation", report.as_dict())
            return {"report": report.as_dict()}
        except Exception as exc:
            return {"error": str(exc)}

    # ----------------------------------------------------------- global search
    def search(self, q: str, limit: int = 8) -> dict:
        """Cross-surface governed search over memory, runs, board cards, audit."""
        q = (q or "").strip().lower()
        if self.store is None or not q:
            return {"query": q, "memory": [], "runs": [], "cards": [], "audit": []}

        def hit(s: str) -> bool:
            return q in (s or "").lower()

        memory = [m for m in self.store.list_memory(limit=500)
                  if hit(m["text"]) or hit(m["provenance"])][:limit]
        runs = [r for r in self.store.list_runs(limit=200)
                if hit(r["goal"]) or hit(r["run_id"])][:limit]
        cards = [c for c in self.store.list_cards()
                 if hit(c["title"]) or hit(c["goal"])][:limit]
        audit = [a for a in self.store.list_audit(limit=500)
                 if hit(a["tool"]) or hit(a["verdict"]) or hit(a["policy_rule"])][:limit]
        return {"query": q, "memory": memory, "runs": runs, "cards": cards,
                "audit": audit}

    def agent_card(self) -> dict:
        """A2A: advertise this agent's capabilities and tools for discovery."""
        self._ensure_agent()
        assert self.agent is not None
        from .agent_service import AgentService
        return AgentService(self.agent).card()

    # --------------------------------------------------------------- readiness
    def readiness(self) -> dict:
        """First-run readiness checklist for the dashboard banner (model,
        memory, web research, knowledge base, embedder, skills)."""
        from . import readiness as _readiness
        return _readiness.readiness(self.store)

    # ---------------------------------------------------- knowledge / LLM wiki
    def sources_list(self) -> dict:
        """List registered knowledge sources (the RAG repositories / LLM wiki),
        each annotated with its indexed doc/chunk counts per namespace."""
        if self.store is None:
            return {"sources": [], "stats": {"chunks": 0, "docs": 0}}
        from .rag import Rag
        from .wiki import KBSourceManager
        rag = Rag(self.store)
        # Per-namespace index sizes so the panel can show how much each
        # repository actually contributes to retrieval.
        ns_stats: dict[str, dict] = {}
        out = []
        for src in KBSourceManager(self.store).list(enabled=None):
            if src.ns not in ns_stats:
                try:
                    ns_stats[src.ns] = rag.stats(ns=src.ns)
                except Exception:
                    ns_stats[src.ns] = {"chunks": 0, "docs": 0, "ns": src.ns}
            out.append({
                "source_id": src.source_id, "uri": src.uri,
                "source_type": src.source_type, "ns": src.ns,
                "title": src.title, "status": src.status,
                "enabled": src.enabled,
                "last_ingested_ts": src.last_ingested_ts,
                "refresh_interval_seconds": src.refresh_interval_seconds,
                "error": src.error,
            })
        # Aggregate index size across every namespace that holds chunks, so the
        # panel reports true totals rather than only the default 'kb' namespace.
        total_chunks = 0
        total_docs = 0
        try:
            for ns in self.store.list_namespaces():
                st = rag.stats(ns=ns)
                total_chunks += int(st.get("chunks", 0))
                total_docs += int(st.get("docs", 0))
        except Exception:
            agg = rag.stats()
            total_chunks, total_docs = agg.get("chunks", 0), agg.get("docs", 0)
        return {"sources": out,
                "stats": {"chunks": total_chunks, "docs": total_docs},
                "by_ns": ns_stats}

    def sources_add(self, uri: str, ns: str = "kb", title: str = "",
                    refresh_hours: float | None = None) -> dict:
        """Register a knowledge source (folder/file path or http(s) URL) and
        immediately index it so it is queryable right away."""
        self._ensure_agent()
        assert self.store is not None
        from .rag import Rag
        from .wiki import KBSourceManager
        from .wiki_safe import UnsafeSourceError
        uri = (uri or "").strip()
        if not uri:
            return {"error": "uri required"}
        mgr = KBSourceManager(self.store)
        interval = KBSourceManager.seconds_from_hours(refresh_hours)
        try:
            src = mgr.add(uri, ns=(ns or "kb").strip() or "kb",
                          title=(title or "").strip(),
                          refresh_interval_seconds=interval)
        except UnsafeSourceError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"could not add source: {exc}"}
        # Index now so the source is immediately useful (don't wait for the
        # heartbeat refresh cycle).
        try:
            src = mgr.refresh(src.source_id, rag=Rag(self.store))
        except Exception as exc:
            self._log("warning", f"source {src.source_id} added but refresh failed: {exc}")
        self._log("info", f"knowledge source added: {src.source_id} ({src.uri})")
        return {"source_id": src.source_id, "status": src.status,
                "uri": src.uri, "ns": src.ns, "error": src.error}

    def sources_delete(self, source_id: str) -> dict:
        if self.store is None:
            return {"error": "no store"}
        source_id = (source_id or "").strip()
        if not source_id:
            return {"error": "source_id required"}
        ok = bool(self.store.delete_kb_source(source_id))
        if ok:
            self._log("info", f"knowledge source removed: {source_id}")
        return {"deleted": ok, "source_id": source_id}

    def sources_refresh(self, source_id: str = "") -> dict:
        """Re-index one source (by id) or every source that is due."""
        if self.store is None:
            return {"error": "no store"}
        from .rag import Rag
        from .wiki import KBSourceManager
        mgr = KBSourceManager(self.store)
        rag = Rag(self.store)
        source_id = (source_id or "").strip()
        try:
            if source_id:
                refreshed = [mgr.refresh(source_id, rag=rag)]
            else:
                refreshed = mgr.refresh_due(rag=rag)
        except KeyError:
            return {"error": f"unknown source '{source_id}'"}
        except Exception as exc:
            return {"error": f"refresh failed: {exc}"}
        return {"refreshed": [
            {"source_id": s.source_id, "status": s.status, "error": s.error}
            for s in refreshed]}

    def ask(self, question: str, k: int = 5) -> Any:
        """Answer a question grounded in the agent's KB and memory."""
        from types import SimpleNamespace
        block = self._budget_block()
        if block:
            return SimpleNamespace(
                text=block["error"], abstained=True, citations=[],
                sources_used=0, contradictions=[], blocked=True)
        self._ensure_agent()
        assert self.agent is not None
        return self.agent.ask(question, k=k, refresh_wiki=False)

    def research(self, query: str, max_results: int = 5) -> dict:
        """Live web research: search the web, fetch the top results, and
        synthesize a grounded, cited answer (cite-or-abstain).

        Works out of the box via the keyless DuckDuckGo default; a configured
        provider (Tavily/Brave/SerpAPI) upgrades result quality. Returns the
        synthesized answer plus the underlying result list so the UI can show
        both the prose and its sources.
        """
        block = self._budget_block()
        if block:
            return {"text": block["error"], "error": block["error"],
                    "blocked": True, "abstained": True, "citations": [], "results": []}
        self._ensure_agent()
        assert self.agent is not None
        query = (query or "").strip()
        if not query:
            return {"error": "query required"}
        from .grounding import GroundedResponder
        from .rag import RetrievedChunk
        from .real_tools import fetch_url
        from .search import web_search
        results = web_search(query, max_results=max_results)
        if results is None:
            return {"text": "Web research is disabled (PRAXIS_SEARCH_DISABLE_DEFAULT). "
                            "Enable a search provider to research online.",
                    "abstained": True, "citations": [], "results": []}
        if not results:
            return {"text": f"No web results found for {query!r}.",
                    "abstained": True, "citations": [], "results": []}
        # Build grounding chunks: use the snippet, enriched with a short fetched
        # excerpt of the top results so synthesis has real content to cite.
        chunks: list = []
        meta: list[dict] = []
        for i, r in enumerate(results):
            text = r.snippet or ""
            if i < 3:  # fetch only the top few to bound latency
                try:
                    fetched = fetch_url(r.url)
                    # fetch_url returns a header line + body; keep a bounded slice.
                    body = fetched.split("\n", 1)[1] if "\n" in fetched else fetched
                    if body:
                        text = (text + "\n" + body)[:1500]
                except Exception:
                    pass
            chunks.append(RetrievedChunk(
                text=text or r.title, source=r.url, score=1.0,
                kind="web", provenance=f"web:{r.url}"))
            meta.append({"title": r.title, "url": r.url,
                         "snippet": r.snippet})
        answer = GroundedResponder(
            self.agent.llm, can_escalate=self.agent._under_budget).answer(
                query, chunks)
        self._bill_chat()
        return {
            "text": answer.text,
            "abstained": answer.abstained,
            "citations": answer.citations,
            "results": meta,
        }

    def _bill_chat(self) -> None:
        """Accrue an interactive chat's real token cost to the spend budget.

        Accrues spend for interactive turns. Blocking is handled by
        :meth:`_budget_block` before chat/ask/research/submit start; this only
        records usage after a turn completes (without incrementing run count).
        """
        if self.store is None or self.agent is None:
            return
        if not hasattr(self.agent.llm, "usage_snapshot"):
            return
        try:
            cost = float(self.agent.llm.usage_snapshot().get("cost_usd", 0.0))
            if cost > 0:
                self.store.add_spend(round(cost, 6), count_run=False)
        except Exception:
            pass

    def chat(self, messages: list[dict], system: str | None = None) -> dict:
        """Hold a multi-turn conversation with the configured model.

        ``messages`` is the full client-side transcript (``[{role, content}]``);
        the daemon is stateless here so the browser owns history. Returns the
        assistant reply plus the model that produced it.
        """
        block = self._budget_block()
        if block:
            return {"text": block["error"], "model": "budget", "blocked": True}
        self._ensure_agent()
        assert self.agent is not None
        if hasattr(self.agent.llm, "reset_usage"):
            self.agent.llm.reset_usage()
        text = self.agent.llm.chat(messages, system=self._chat_system(system),
                                   role="general")
        self._bill_chat()
        return {"text": text, "model": cfg.get_default_model() or "mock (offline)"}

    def _chat_system(self, system: str | None) -> str:
        """The chat persona, with pack + durable user model prepended."""
        from . import pack
        from .persona import persona_system_prefix
        base = system or pack.compose_system(_CHAT_SYSTEM)
        prefix = persona_system_prefix()
        if prefix:
            return prefix + "\n\n" + base
        return base

    def chat_stream(self, messages: list[dict],
                    system: str | None = None) -> Iterator[str]:
        """Streaming multi-turn chat — yields assistant text deltas.

        Stateless like :meth:`chat` (the browser owns history). The model label
        is read from config by the HTTP layer and sent as a leading meta event
        ahead of the first delta. Honors the same budget hard-stop as
        :meth:`chat` so the streaming UI cannot spend past the cap.
        """
        block = self._budget_block()
        if block:
            yield block["error"]
            return
        self._ensure_agent()
        assert self.agent is not None
        if hasattr(self.agent.llm, "reset_usage"):
            self.agent.llm.reset_usage()
        yield from self.agent.llm.chat_stream(
            messages, system=self._chat_system(system), role="general")
        self._bill_chat()

    def chat_agent(self, messages: list[dict],
                   system: str | None = None) -> Iterator[dict]:
        """Run the governed tool-calling loop and yield UI event dicts.

        Each model-proposed tool call is schema-validated and routed through the
        broker: read/draft execute, send/destructive are held for approval, and
        disallowed/kill-switched tools are denied. Event ``type`` is one of
        ``tool_call`` / ``tool_result`` / ``approval`` / ``denied`` / ``final`` /
        ``error``.
        """
        block = self._budget_block()
        if block:
            yield {"type": "error", "error": block["error"], "blocked": True}
            return
        self._ensure_agent()
        assert self.agent is not None
        from .chat_agent import ChatEngine, GovernedChatAgent
        from .reflexion import ReflexionConfig, ReflexiveChatAgent
        from .verifier import AnswerVerifier, VerificationConfig, VerifiedChatAgent
        base = GovernedChatAgent(
            self.agent.llm, self.agent.registry, self.agent.broker,
            memory=self.agent.memory,
            max_context_chars=int(cfg.load_config().get("agents", {})
                                  .get("maxContextChars", 24000) or 0))
        engine: ChatEngine = base
        rc = ReflexionConfig.load()
        if rc.enabled and rc.max_reflections > 0:
            # Bounded self-correction: a dead-ended, side-effect-free turn is
            # retried once with an injected reflection. Governance is unchanged.
            engine = ReflexiveChatAgent(base, max_reflections=rc.max_reflections)
        vc = VerificationConfig.load()
        if vc.enabled and vc.max_revisions > 0:
            # Critic gate (outermost): a confident answer that misreports a held/
            # denied action is rejected and revised once. When the operator has
            # configured an optional LLM-verifier critic backend (H05), it
            # upgrades the discrete APPROVE/REVISE slot to a continuous-reward
            # verifier; the deterministic honesty checks still run first.
            verifier = AnswerVerifier(critic=vc.critic)
            engine = VerifiedChatAgent(
                engine, verifier=verifier, max_revisions=vc.max_revisions)
        grounded = self._ground_with_memory(system or _AGENT_SYSTEM, messages)
        grounded = self._ground_with_skills(grounded, messages)
        # Surface what persistent memory / skills were recalled INTO this turn so
        # the dashboard can show that memory is alive and working (Phase 3).
        recall = self._recall_preview(messages)
        if recall["memory"] or recall["skills"]:
            yield {"type": "recall", **recall}
        for event in engine.run(messages, system=grounded):
            yield {"type": event.type, **event.data}

    def _recall_preview(self, messages: list[dict]) -> dict:
        """Collect a compact preview of the memory + skills recalled for the
        latest user turn, for the dashboard's 'recalled into this turn' view."""
        out: dict = {"memory": [], "skills": []}
        last_user = next((str(m.get("content", "")) for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if not last_user.strip() or self.agent is None:
            return out
        try:
            if self.agent.memory is not None:
                for it in self.agent.memory.recall(last_user, k=4):
                    out["memory"].append({
                        "kind": getattr(it, "kind", "note"),
                        "text": (it.text or "")[:200],
                        "provenance": getattr(it, "provenance", ""),
                    })
        except Exception:
            pass
        try:
            skills = getattr(self.agent, "skills", None)
            if skills is not None:
                for sk in skills.retrieve(last_user, k=3):
                    out["skills"].append({"name": getattr(sk, "name", "")})
        except Exception:
            pass
        return out

    def _ground_with_memory(self, system: str, messages: list[dict]) -> str:
        """Prepend BM25-recalled memory relevant to the latest user turn.

        Surfaces the agent's own durable/episodic memory (facts, decisions,
        notes) into the turn's context so prior work informs the answer. Empty
        memory, no user turn, or ``agents.memoryRecall=false`` (or
        ``PRAXIS_MEMORY_RECALL=0``) all leave the system prompt unchanged.
        """
        if self.agent is None or self.agent.memory is None:
            return system
        if os.environ.get("PRAXIS_MEMORY_RECALL", "").lower() in ("0", "false", "off"):
            return system
        if not cfg.load_config().get("agents", {}).get("memoryRecall", True):
            return system
        last_user = next((str(m.get("content", "")) for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if not last_user.strip():
            return system
        try:
            ctx = self.agent.memory.recall_context(last_user)
        except Exception:
            ctx = ""
        return f"{ctx}\n\n{system}" if ctx else system

    def _ground_with_skills(self, system: str, messages: list[dict]) -> str:
        """Prepend relevant learned procedures (skills) to the turn's context.

        Surfaces the agent's own distilled, non-quarantined skills for the latest
        user goal so recurring tasks benefit from prior learning. A missing skill
        library, no user turn, or ``agents.skillRecall=false`` (or
        ``PRAXIS_SKILL_RECALL=0``) leave the system prompt unchanged.
        """
        if self.agent is None:
            return system
        skills = getattr(self.agent, "skills", None)
        if skills is None:
            return system
        if os.environ.get("PRAXIS_SKILL_RECALL", "").lower() in ("0", "false", "off"):
            return system
        if not cfg.load_config().get("agents", {}).get("skillRecall", True):
            return system
        last_user = next((str(m.get("content", "")) for m in reversed(messages)
                          if m.get("role") == "user"), "")
        if not last_user.strip():
            return system
        try:
            ctx = skills.recall_context(last_user)
        except Exception:
            ctx = ""
        return f"{ctx}\n\n{system}" if ctx else system

    def model_info(self) -> dict:
        """Current default model + whether a real provider is configured."""
        model = cfg.get_default_model()
        provider = cfg.get_default_provider()
        return {
            "model": model or "mock (offline)",
            "configured": model is not None,
            "provider": provider,
            "embed_model": cfg.get_embed_model(),
        }

    def voice_status(self) -> dict:
        from . import voice
        return voice.voice_status()

    def set_voice_mode(self, mode: str) -> dict:
        from . import voice
        if mode not in voice.MODES:
            raise ValueError(f"unknown voice mode '{mode}'")
        cfg.set_voice_mode(mode)
        return voice.voice_status()

    def transcribe(self, audio: bytes, mime: str = "audio/webm") -> dict:
        from . import voice
        res = voice.transcribe_audio(audio, mime)
        return {"text": res.text, "detail": res.detail}

    def synthesize(self, text: str) -> Any:
        from . import voice
        return voice.synthesize_text(text)

    def run_realtime_session(self, conn: Any) -> None:
        from . import voice
        self._ensure_agent()
        assert self.agent is not None
        voice.run_realtime(self.agent, conn)

    def providers_catalog(self) -> list[dict]:
        """Provider picker payload for the dashboard (no secrets)."""
        from .providers import CATALOG, ORDER, discover_ollama_models
        out: list[dict] = []
        for pid in ORDER:
            p = CATALOG[pid]
            models = list(p.suggested_models)
            # For Ollama providers, live-discover models so the cloud catalog is
            # actually available in the dashboard picker.
            if pid in ("ollama", "ollama-cloud"):
                entry = cfg.provider_entry(pid) or {}
                base_url = entry.get("baseUrl") or p.base_url
                key = cfg.resolve_api_key(pid)
                discovered = discover_ollama_models(
                    base_url=base_url, timeout=5.0, api_key=key)
                seen = set(models)
                for m in discovered:
                    if m not in seen:
                        models.append(m)
                        seen.add(m)
            out.append({
                "id": p.id, "label": p.label, "needs_key": p.needs_key,
                "key_env": p.key_env, "base_url": p.base_url,
                "models": models, "notes": p.notes,
            })
        return out

    def secrets_status(self) -> dict:
        """Where each configured provider's API key resolves (never the values)."""
        from . import __version__
        catalog = {p["id"]: p for p in self.providers_catalog()}
        configured = cfg.load_config().get("providers", {})
        providers = [
            {"id": pid, "label": catalog.get(pid, {}).get("label", pid),
             "location": cfg.key_location(pid)}
            for pid in sorted(configured)
        ]
        key_providers = [{"id": p["id"], "label": p["label"]}
                         for p in self.providers_catalog() if p.get("needs_key")]
        return {
            "version": __version__,
            "config_path": str(cfg.config_path()),
            "keychain_available": cfg.keychain_available(),
            "providers": providers,
            "key_providers": key_providers,
        }

    def secrets_set(self, provider: str, key: str) -> dict:
        provider = (provider or "").strip()
        key = (key or "").strip()
        if not provider or not key:
            return {"error": "provider and key are required"}
        backend = cfg.save_api_key(provider, key)
        self._log("info", f"stored API key for '{provider}' in {backend} via dashboard")
        out = self.secrets_status()
        out["backend"] = backend
        return out

    def secrets_delete(self, provider: str) -> dict:
        provider = (provider or "").strip()
        if not provider:
            return {"error": "provider is required"}
        cfg.delete_api_key(provider)
        self._log("info", f"removed API key for '{provider}' via dashboard")
        return self.secrets_status()

    def secrets_migrate(self) -> dict:
        if not cfg.keychain_available():
            return {"error": "no keychain backend available (install the keyring extra)"}
        moved = cfg.migrate_secrets_to_keychain()
        out = self.secrets_status()
        out["migrated"] = moved
        return out

    def onboard_apply(self, provider: str, model: str, base_url: str | None = None,
                      api_key: str | None = None, use_env_ref: bool = True) -> dict:
        """Apply dashboard setup: write the provider + default model (and store a
        pasted key). The router reads config live, so the next turn uses it."""
        from . import onboard as onboard_mod
        from .providers import CATALOG
        provider = (provider or "").strip()
        model = (model or "").strip()
        if provider not in CATALOG:
            return {"error": f"unknown provider '{provider}'"}
        if not model:
            return {"error": "a model is required"}
        summary = onboard_mod.run_noninteractive(
            provider, model, base_url=(base_url or None),
            api_key=(api_key or None), use_env_ref=use_env_ref)
        self._log("info", f"onboarded '{provider}/{model}' via dashboard")
        return {"ok": True, "model": summary.get("model"),
                "key_backend": summary.get("key_backend")}
    def switch_model(self, provider_id: str, model: str,
                     base_url: str | None = None) -> dict:
        """Set the default model from the dashboard picker.

        Uses an environment-variable key reference only — secrets are never
        accepted over HTTP. The router reads config live, so the next chat turn
        uses the new model immediately.
        """
        from . import onboard
        from .providers import CATALOG
        if provider_id not in CATALOG:
            raise ValueError(f"unknown provider '{provider_id}'")
        if not model:
            raise ValueError("model id is required")
        summary = onboard.run_noninteractive(provider_id, model,
                                             base_url=base_url, use_env_ref=True)
        self._log("info", f"default model switched to {summary['model']}")
        return {"model": summary["model"], "provider": provider_id}

    def _task_pending_from_action(self, action: dict) -> PendingApproval:
        assert self.store is not None
        approval = self.store.get_approval(action["approval_id"]) or {}
        return PendingApproval(
            approval_id=action["approval_id"],
            tool=action["tool"],
            args=dict(action["args"]),
            preview=str(approval.get("preview") or ""),
            provenance=str(approval.get("provenance") or "task"),
            cycle_id=str(approval.get("cycle_id") or ""),
            decision_id=str(approval.get("decision_id") or ""),
            rationale=str(approval.get("rationale") or ""),
            evidence=list(approval.get("evidence") or []),
            organization_id=str(approval.get("organization_id") or ""),
        )

    def _apply_task_action_reconciliation(self, outcome: dict) -> None:
        assert self.agent is not None
        assert self.manager is not None
        for approval_id in outcome.get("cancelled_approvals", []):
            self.agent.broker.pending.pop(str(approval_id), None)
        for transition in outcome.get("transitions", []):
            old_status = str(transition.get("old_status") or "")
            new_status = str(transition.get("new_status") or "")
            task_id = str(transition.get("task_id") or "")
            if old_status == new_status:
                continue
            if old_status == "waiting_approval" and new_status != "waiting_approval":
                self.state.tasks_waiting_approval = max(
                    0, self.state.tasks_waiting_approval - 1
                )
            if new_status == "completed":
                self.state.tasks_completed += 1
            elif new_status == "failed":
                self.state.tasks_failed += 1
            task = self.manager.get(task_id)
            self.emit_event("task", {
                "task_id": task_id,
                "status": new_status,
                "goal": task.goal if task is not None else "",
                "output": (task.output if task is not None else "")[:500],
                "error": (task.error if task is not None else "")[:500],
            })

    def _finish_task_approval(self, approval_id: str, execution: Any) -> bool:
        assert self.store is not None
        if execution.status == "completed":
            outcome = self.store.finish_task_approval_action(
                approval_id, status="completed", output=execution.output
            )
            success = True
        else:
            reason = execution.error or "approved action did not complete"
            action_rows = self.store.list_task_approval_actions(
                approval_id=approval_id, status="pending_execution"
            )
            uncertain = bool(
                execution.provider_attempted
                and any(not row["provider_idempotent"] for row in action_rows)
            )
            terminal_status = "manual_reconciliation" if uncertain else "failed"
            if uncertain:
                reason = f"manual reconciliation required: provider outcome uncertain: {reason}"
            outcome = self.store.finish_task_approval_action(
                approval_id, status=terminal_status, error=reason
            )
            success = False
        self._apply_task_action_reconciliation(outcome)
        return success

    def _validate_claimed_task_action(
        self, pending: PendingApproval, actions: list[dict]
    ) -> ApprovalExecution | None:
        """Bind a claimed durable intent to the current exact provider contract."""
        assert self.store is not None
        assert self.agent is not None
        tool = self.agent.registry.get(pending.tool)
        if tool is None:
            return None  # execute_approved_action emits the established missing-tool failure.
        pending_args_json = self.store._task_action_json(
            pending.args, "approved task action args"
        )
        current_effect_type = tool.effect_type or tool.name
        key_arg = tool.idempotency_key_arg or ""
        current_key = str(pending.args.get(key_arg) or "") if key_arg else ""
        current_provider_idempotent = bool(key_arg and current_key)
        for action in actions:
            action_args_json = self.store._task_action_json(
                action["args"], "task action args"
            )
            expected_fingerprint = self.store._task_action_fingerprint(
                action["effect_type"], action_args_json
            )
            if (
                action["tool"] != pending.tool
                or action_args_json != pending_args_json
                or action["fingerprint"] != expected_fingerprint
                or action["effect_type"] != current_effect_type
                or action["risk"] != tool.risk.value
                or tool.risk not in {RiskClass.SEND, RiskClass.DESTRUCTIVE}
                or action["provider_idempotent"] != current_provider_idempotent
                or action["idempotency_key"] != current_key
            ):
                return ApprovalExecution(
                    "failed",
                    error="claimed task action no longer matches its registered tool contract",
                )
        return None

    def _initialize_task_approval_state(self) -> None:
        if self._task_approvals_initialized:
            return
        if self.store is None or self.agent is None or self.manager is None:
            return
        self._backfill_legacy_task_approval_actions()
        self._reconcile_task_approval_actions()
        self._task_approvals_initialized = True

    def _backfill_legacy_task_approval_actions(self) -> None:
        """Upgrade waiting tasks created before durable approval actions existed."""
        assert self.store is not None
        assert self.agent is not None
        for task in self.store.list_tasks(status="waiting_approval", limit=1_000_000):
            result = task.get("result")
            pending = result.get("pending_approvals") if type(result) is dict else None
            if type(pending) is not list:
                continue
            for reference in pending:
                if type(reference) is not dict:
                    continue
                approval_id = reference.get("approval_id")
                tool_name = reference.get("tool")
                if type(approval_id) is not str:
                    continue
                if type(tool_name) is not str:
                    reason = (
                        "manual reconciliation required: legacy held action is missing "
                        "its exact tool context"
                    )
                    if self.store.fail_legacy_waiting_task_approval(
                        str(task["task_id"]), approval_id, reason=reason
                    ):
                        self.agent.broker.pending.pop(approval_id, None)
                    continue
                approval = self.store.get_approval(approval_id)
                approval_status = str((approval or {}).get("status") or "missing")
                if approval is None or approval_status != "pending":
                    reason = (
                        "manual reconciliation required: legacy task approval was "
                        "approved before durable execution receipts; provider outcome unknown"
                        if approval_status == "approved"
                        else f"legacy task approval is no longer pending ({approval_status})"
                    )
                    if self.store.fail_legacy_waiting_task_approval(
                        str(task["task_id"]), approval_id, reason=reason
                    ):
                        self.agent.broker.pending.pop(approval_id, None)
                    continue
                if approval.get("tool") != tool_name:
                    reason = (
                        "manual reconciliation required: legacy held action tool no longer "
                        "matches its approval"
                    )
                    if self.store.fail_legacy_waiting_task_approval(
                        str(task["task_id"]), approval_id, reason=reason
                    ):
                        self.agent.broker.pending.pop(approval_id, None)
                    continue
                tool = self.agent.registry.get(tool_name)
                held_risk = reference.get("risk")
                if (
                    tool is None
                    or type(held_risk) is not str
                    or held_risk not in {
                        RiskClass.SEND.value, RiskClass.DESTRUCTIVE.value
                    }
                    or tool.risk.value != held_risk
                ):
                    reason = (
                        "manual reconciliation required: legacy held action no longer "
                        "has its exact consequential registered risk contract"
                    )
                    if self.store.fail_legacy_waiting_task_approval(
                        str(task["task_id"]), approval_id, reason=reason
                    ):
                        self.agent.broker.pending.pop(approval_id, None)
                    continue
                effect_type = tool.effect_type or tool.name
                key_arg = tool.idempotency_key_arg or ""
                args = approval.get("args")
                key_value = args.get(key_arg) if type(args) is dict and key_arg else ""
                idempotency_key = key_value if type(key_value) is str else ""
                backfilled = self.store.backfill_legacy_task_approval_action(
                    str(task["task_id"]),
                    approval_id,
                    effect_type=effect_type,
                    risk=held_risk,
                    idempotency_key=idempotency_key,
                    provider_idempotent=bool(key_arg and idempotency_key),
                )
                if not backfilled:
                    current = self.store.get_approval(approval_id)
                    if current is None or current.get("status") != "pending":
                        self.agent.broker.pending.pop(approval_id, None)
                if (
                    not backfilled
                    and not self.store.has_task_approval_action(approval_id)
                ):
                    reason = (
                        "manual reconciliation required: legacy held action metadata "
                        "does not exactly match its pending approval"
                    )
                    if self.store.fail_legacy_waiting_task_approval(
                        str(task["task_id"]), approval_id, reason=reason
                    ):
                        self.agent.broker.pending.pop(approval_id, None)

    def _reconcile_task_approval_actions(self) -> None:
        """Recover durably claimed task effects after process interruption."""
        assert self.store is not None
        assert self.agent is not None
        pending_actions = self.store.list_task_approval_actions(status="pending_approval")
        checked_pending: set[str] = set()
        now = time.time()
        for action in pending_actions:
            pending_id = str(action["approval_id"])
            if pending_id in checked_pending:
                continue
            checked_pending.add(pending_id)
            approval = self.store.get_approval(pending_id)
            approval_state = str((approval or {}).get("status") or "missing")
            expires_at = (approval or {}).get("expires_at")
            expired = bool(
                approval_state == "pending"
                and expires_at is not None
                and float(expires_at) < now
            )
            if approval_state == "pending" and not expired:
                continue
            terminal = "expired" if expired or approval_state == "expired" else "rejected"
            reason = (
                "approval expired before execution"
                if terminal == "expired"
                else f"approval is no longer pending ({approval_state})"
            )
            outcome = self.store.reject_task_approval_action(
                pending_id, reason=reason, approval_status=terminal
            )
            self.agent.broker.pending.pop(pending_id, None)
            self._apply_task_action_reconciliation(outcome)

        actions = self.store.list_task_approval_actions(status="pending_execution")
        seen: set[str] = set()
        for action in actions:
            approval_id = str(action["approval_id"])
            if approval_id in seen:
                continue
            seen.add(approval_id)
            tool = self.agent.registry.get(action["tool"])
            args_json = self.store._task_action_json(action["args"], "task action args")
            expected = self.store._task_action_fingerprint(action["effect_type"], args_json)
            idempotency_arg = getattr(tool, "idempotency_key_arg", "") if tool else ""
            current_effect_type = (tool.effect_type or tool.name) if tool else ""
            safely_retryable = bool(
                tool is not None
                and action["effect_type"] == current_effect_type
                and action["risk"] == tool.risk.value
                and tool.risk in {RiskClass.SEND, RiskClass.DESTRUCTIVE}
                and action["fingerprint"] == expected
                and action["provider_idempotent"]
                and idempotency_arg
                and action["idempotency_key"]
                and action["args"].get(idempotency_arg) == action["idempotency_key"]
            )
            if not safely_retryable:
                outcome = self.store.finish_task_approval_action(
                    approval_id,
                    status="manual_reconciliation",
                    error=(
                        "manual reconciliation required: process stopped after durable "
                        "approval claim without a verifiable provider idempotency key"
                    ),
                )
                self._apply_task_action_reconciliation(outcome)
                continue
            pending = self._task_pending_from_action(action)
            execution = self.agent.execute_approved_action(
                pending, approved_by="restart-recovery"
            )
            self._finish_task_approval(approval_id, execution)

    def approve(self, approval_id: str, mode: str = "once",
                approved_by: str = "", approved_role: str = "",
                approval_notes: str = "") -> bool:
        """Approve a pending consequential action and resume waiting work.

        Two paths share this endpoint:

        * **Task queue** — atomically claim the exact held action, execute its
          stored args, persist an immutable receipt, and reconcile every linked
          task action. No one-shot/session allow is granted.
        * **Chat** — a held tool call mid-conversation. Do *not* execute here;
          only after a *full* approval claim, grant a one-shot or session allow
          and emit a ``resume`` SSE so the dashboard re-submits the conversation.

        ``approved_by`` is required for dual-approval tools (four-eyes); defaults
        to ``web-ui`` for single-signature tools. One-shot/session allows are
        never granted on a partial dual-approval signature or on the task path.
        """
        self._ensure_agent()
        assert self.agent is not None
        pending = self.agent.broker.pending.get(approval_id)
        if pending is None:
            return False
        tool_name = pending.tool
        mode = (mode or "once").strip().lower()
        signer = (approved_by or "").strip() or "web-ui"

        assert self.store is not None
        task_actions = self.store.list_task_approval_actions(approval_id=approval_id)
        if task_actions:
            # Pre-claim safety checks leave the approval and task action pending.
            tool = self.agent.registry.get(pending.tool)
            if tool is not None and tool.risk in {RiskClass.SEND, RiskClass.DESTRUCTIVE}:
                if self.agent.broker.kill.tripped:
                    return False
                if self.agent.broker.egress_blocked_for(pending.args):
                    return False
            with self._approval_lock:
                claimed = self.agent.broker.approve(
                    approval_id,
                    approved_by=signer,
                    approval_notes=approval_notes,
                    approved_role=approved_role,
                )
                if claimed is None:
                    row = self.store.get_approval(approval_id)
                    state = str((row or {}).get("status") or "")
                    if state in {"expired", "rejected"}:
                        outcome = self.store.reject_task_approval_action(
                            approval_id,
                            reason=(
                                "approval expired before execution"
                                if state == "expired"
                                else "approval rejected before execution"
                            ),
                            approval_status=state,
                        )
                        self._apply_task_action_reconciliation(outcome)
                    return False
                execution = self._validate_claimed_task_action(claimed, task_actions)
                if execution is None:
                    execution = self.agent.execute_approved_action(
                        claimed, approved_by=signer
                    )
                success = self._finish_task_approval(approval_id, execution)
            self._log(
                "info" if success else "error",
                f"approved task action {tool_name}: "
                f"{(execution.output or execution.error)[:200]}",
            )
            return success

        if str(pending.provenance).startswith("task:"):
            # A task approval without its durable action row is an interrupted
            # setup, never a chat approval. Fail closed rather than execute it.
            return False

        # Chat path: claim the approval first. Only after a *full* release
        # (not a partial dual-approval signature) grant one-shot/session allow
        # so the dashboard can re-submit and the model re-requests the tool.
        approved = self.agent.broker.approve(
            approval_id,
            approved_by=signer,
            approval_notes=approval_notes,
            approved_role=approved_role,
        )
        if approved is None:
            return False
        if mode in ("chat", "always"):
            self.agent.broker.allow_tool_for_session(tool_name)
        elif mode == "once":
            self.agent.broker.allow_tool_once(tool_name, approved.args)
        self.emit_event("resume", {
            "approval_id": approval_id,
            "tool": tool_name,
            "mode": mode,
        })
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

    # ------------------------------------------------ preeminence surfaces
    def persona_get(self) -> dict:
        from .growth import user_model_card
        return user_model_card()

    def persona_set(self, updates: dict) -> dict:
        from .persona import mirror_to_memory, save_persona
        p = save_persona(updates or {})
        self._ensure_agent()
        if self.agent is not None:
            mirror_to_memory(getattr(self.agent, "memory", None))
        return {"persona": p, "ok": True}

    def pulse_preview(self) -> dict:
        from .pulse import build_digest
        return build_digest(self)

    def pulse(self, target: str | None = None) -> dict:
        from .pulse import deliver_digest
        dig = deliver_digest(self, target=target)
        self.emit_event("pulse", {"approvals": dig.get("approvals"),
                                  "delivered": dig.get("delivered")})
        return dig

    def growth_model(self) -> dict:
        from .growth import user_model_card
        return user_model_card()

    def growth_skills(self) -> list:
        from .growth import list_skills
        self._ensure_agent()
        return list_skills(self.agent)

    def growth_proposals(self) -> list:
        from .growth import list_proposals
        self._bind_durable_surfaces()
        return list_proposals(store=self.store)

    def growth_evolve(self, limit: int = 3) -> dict:
        from .growth import run_evolve
        self._ensure_agent()
        llm = getattr(self.agent, "llm", None) if self.agent else None
        props = run_evolve(self.agent, llm=llm, limit=limit, store=self.store)
        return {"proposals": props}

    def growth_apply(self, proposal_id: str) -> dict:
        from .growth import apply_proposal
        self._ensure_agent()
        return apply_proposal(self.agent, proposal_id, store=self.store)

    def growth_reject(self, proposal_id: str) -> dict:
        from .growth import reject_proposal
        self._bind_durable_surfaces()
        return reject_proposal(proposal_id, store=self.store)

    def growth_rooms(self) -> list:
        from .growth import list_rooms
        return list_rooms()

    def record_ttft(self, seconds: float) -> dict:
        from .growth import record_ttft
        return record_ttft(seconds)

    def ttft_stats(self) -> dict:
        from .growth import ttft_stats
        return ttft_stats()

    def channels_status(self) -> dict:
        from . import channels_inbound as ch
        from .pulse import channel_status
        st = channel_status()
        st["telegram_detail"] = ch.telegram_status()
        return st

    def telegram_configure(self, bot_token: str = "", chat_id: str = "",
                           enabled: bool = True,
                           use_env_ref: bool = False) -> dict:
        """One-click Telegram enable from Settings."""
        from . import channels_inbound as ch
        status = ch.configure_telegram(
            bot_token=bot_token, chat_id=chat_id, enabled=enabled,
            use_env_ref=use_env_ref)
        # Probe when token present
        probe = ch.telegram_get_me() if status.get("has_token") else {}
        status["probe"] = probe
        self._log("info", f"telegram configured enabled={status.get('enabled')} "
                          f"has_token={status.get('has_token')}")
        return status

    def telegram_disable(self) -> dict:
        from . import channels_inbound as ch
        return ch.disable_telegram()

    def telegram_status(self) -> dict:
        from . import channels_inbound as ch
        st = ch.telegram_status()
        if st.get("has_token"):
            st["probe"] = ch.telegram_get_me()
        return st

    def browser_snapshot(self) -> dict:
        """Last browser session view for the computer-use pane."""
        try:
            from .browser import _SESSION
            return {
                "url": getattr(_SESSION, "url", "") or "",
                "title": getattr(_SESSION, "title", "") or "",
                "text_preview": (getattr(_SESSION, "text", "") or "")[:800],
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _channel_chat(self, messages: list[dict]) -> str:
        """Governed reply for inbound messaging (uses chat_agent when possible)."""
        self._ensure_agent()
        assert self.agent is not None
        # Prefer tool-calling agent loop for capability parity with Deck.
        try:
            events = list(self.chat_agent(messages))
            finals = [e for e in events if e.get("type") == "final"]
            if finals:
                return str(finals[-1].get("text") or "")
            texts = [e.get("text") for e in events if e.get("text")]
            if texts:
                return str(texts[-1])
        except Exception:
            pass
        res = self.chat(messages)
        return str(res.get("text") or "")

    def telegram_webhook(self, update: dict) -> dict:
        from . import channels_inbound as ch
        msg = ch.parse_telegram_update(update)
        if msg is None:
            return {"ok": True, "ignored": True}
        return self._dispatch_inbound(msg)

    def slack_events(self, payload: dict, raw: bytes = b"",
                     headers=None) -> dict:
        from . import channels_inbound as ch
        from . import config as cfg_mod
        secret = ((cfg_mod.load_config().get("agents") or {})
                  .get("gateways") or {}).get("slack", {}).get("signing_secret", "")
        if secret and headers is not None:
            ts = headers.get("X-Slack-Request-Timestamp") or headers.get(
                "x-slack-request-timestamp") or ""
            sig = headers.get("X-Slack-Signature") or headers.get(
                "x-slack-signature") or ""
            if not ch.verify_slack_signature(
                    secret, str(ts), raw or b"", str(sig)):
                return {"ok": False, "error": "bad signature"}
        parsed = ch.parse_slack_event(payload)
        if isinstance(parsed, dict) and "challenge" in parsed:
            return parsed
        if parsed is None:
            return {"ok": True, "ignored": True}
        return self._dispatch_inbound(parsed)

    def _dispatch_inbound(self, msg) -> dict:
        from . import channels_inbound as ch
        text = msg.text.strip()
        # Channel-native approve / deny
        if text.upper().startswith("APPROVE_CMD:") or text.lower().startswith("approve "):
            # handle_inbound may return APPROVE_CMD — or raw command
            pass
        base = f"http://127.0.0.1:{self.status_port}"
        if self.status_host not in ("127.0.0.1", "0.0.0.0", "::"):
            base = f"http://{self.status_host}:{self.status_port}"

        def _chat(hist):
            return self._channel_chat(hist)

        # Direct approve/deny commands
        import re as _re
        m = _re.match(r"(?i)^(?:/)?approve(?:\s+|:)(\S+)", text)
        if m:
            ok = self.approve(m.group(1), mode="once", approved_by=msg.sender)
            reply = f"{'Approved' if ok else 'Could not approve'} {m.group(1)}"
            if msg.channel == "telegram":
                ch.telegram_send(reply, msg.chat_id)
            elif msg.channel == "slack":
                ch.slack_reply(reply, msg.chat_id)
            return {"ok": ok, "reply": reply}
        m = _re.match(r"(?i)^(?:/)?deny(?:\s+|:)(\S+)", text)
        if m:
            ok = self.deny_approval(m.group(1))
            reply = f"{'Denied' if ok else 'Could not deny'} {m.group(1)}"
            if msg.channel == "telegram":
                ch.telegram_send(reply, msg.chat_id)
            elif msg.channel == "slack":
                ch.slack_reply(reply, msg.chat_id)
            return {"ok": ok, "reply": reply}

        self._ensure_agent()
        approvals_before = set(self.agent.broker.pending.keys()) if self.agent else set()
        reply = ch.handle_inbound(
            msg, _chat, base_url=base,
            approvals=None, store=self.store)
        # Attach any newly held approvals
        new_appr = []
        if self.agent:
            for aid, pa in self.agent.broker.pending.items():
                if aid not in approvals_before:
                    new_appr.append({
                        "approval_id": aid, "tool": pa.tool, "preview": pa.preview})
        if new_appr:
            links = []
            for a in new_appr:
                link = ch.approval_deep_link(base, a["approval_id"])
                links.append(f"• {a['tool']}: approve {a['approval_id']}\n  {link}")
            reply += "\n\n⏸ Held for your approval:\n" + "\n".join(links)
            reply += "\n\nReply `approve <id>` or `deny <id>`."
        if msg.channel == "telegram":
            ch.telegram_send(reply, msg.chat_id)
        elif msg.channel == "slack":
            ch.slack_reply(reply, msg.chat_id)
        self.emit_event("channel", {
            "channel": msg.channel, "chat_id": msg.chat_id,
            "sender": msg.sender, "preview": text[:120]})
        return {"ok": True, "reply": reply[:500]}

    def _telegram_poll_tick(self) -> None:
        """Long-poll free: pull Telegram updates each idle tick when enabled."""
        from . import channels_inbound as ch
        if not ch.telegram_enabled():
            return
        offset = int(getattr(self, "_tg_offset", 0) or 0)
        updates = ch.telegram_poll_updates(offset=offset, timeout=0)
        for u in updates:
            uid = int(u.get("update_id") or 0)
            if uid >= offset:
                self._tg_offset = uid + 1
            try:
                self.telegram_webhook(u)
            except Exception as exc:
                self._log("warning", f"telegram update failed: {exc}")

    def resume(self, task_id: str) -> Any:
        """Mark a waiting-approval task completed after held actions were approved.

        Never executes still-pending tools. Execution must go through
        :meth:`approve` / :meth:`PraxisAgent.approve`, which enforce dual-approval,
        kill-switch, and egress. If any approvals remain pending for this cycle,
        the task stays ``waiting_approval``.
        """
        if self.agent is None:
            self._ensure_agent()
        assert self.agent is not None
        assert self.manager is not None
        task = self.manager.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != "waiting_approval":
            return task
        pending_for_cycle = [
            pa for pa in self.agent.broker.pending.values()
            if pa.cycle_id == task.cycle_id
        ]
        if pending_for_cycle:
            # Still held — do not run unapproved tools.
            return task
        assert self.store is not None
        self.store.update_task(
            task_id, status="completed", error="",
            result_json=json.dumps({
                "cycle_id": task.cycle_id,
                "actions": ["[approved] held action(s) already executed"],
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
