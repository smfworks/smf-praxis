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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterator

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
    "and continue with whatever you can safely do. Format answers with Markdown."
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
</head>
<body>
<div id="toasts" class="toasts" aria-live="polite"></div>
<header>
  <div class="brand"><span class="logo"></span> Praxis</div>
  <span class="pill modelpill"><span class="dot"></span><span id="modelBadge">—</span></span>
  <span class="spacer"></span>
  <button id="cmdk" class="badge" type="button" title="Command palette (Ctrl/Cmd+K)">⌘K</button>
  <button id="settingsBtn" class="badge" type="button" title="Settings">⚙</button>
  <span id="connPill" class="pill conn conn-connecting" title="Live update stream"><span class="dot"></span><span id="connText">connecting…</span></span>
  <span id="status" class="badge">checking…</span>
</header>

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
      <div class="segmented">
        <button id="seg-chat" class="active" onclick="setMode('chat')">Chat</button>
        <button id="seg-ask" onclick="setMode('ask')">Ask</button>
        <button id="seg-research" onclick="setMode('research')">Research</button>
        <button id="seg-do" onclick="setMode('do')">Do</button>
        <button id="seg-agent" onclick="setMode('agent')">Agent</button>
      </div>
      <span class="hint" id="modeHint">Conversational chat with your model.</span>
      <button class="ghost" onclick="newChat()" title="Start a new chat">New chat</button>
    </div>
    <div id="messages" class="messages"></div>
    <form class="composer" onsubmit="sendMessage(event)">
      <textarea id="message" rows="1" placeholder="Message Praxis…  (Enter to send, Shift+Enter for newline)" autocomplete="off"></textarea>
      <button class="mic-btn" id="mic" type="button" title="Push to talk" onclick="toggleMic()" hidden>🎙</button>
      <button class="send-btn" id="send" type="submit" title="Send">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
      </button>
    </form>
  </section>

  <aside>
    <div class="panel pad">
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
    <div class="panel pad">
      <h2>Voice</h2>
      <div class="vmodes" id="vmodes"></div>
      <div class="hint" id="voiceHint">Voice is off.</div>
    </div>
    <div class="panel pad">
      <h2>Files</h2>
      <div id="drop" class="dropzone" tabindex="0" role="button" aria-label="Upload files">
        <input id="fileInput" type="file" multiple hidden />
        <div class="dz-icon">⬆</div>
        <div class="dz-text">Drop files here or <span class="dz-link">browse</span></div>
        <div class="dz-sub">Saved to the agent's work directory</div>
      </div>
      <div id="uploads"></div>
    </div>
    <div class="panel pad">
      <h2>Queue</h2>
      <div id="tasks"><div class="empty">No tasks yet.</div></div>
    </div>
    <div class="panel pad">
      <h2>Run Graph</h2>
      <div id="runlist"><div class="empty">No runs yet.</div></div>
    </div>
    <div class="panel pad">
      <h2>Work Board</h2>
      <div id="board-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Safety Center</h2>
      <div id="safety-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Inference</h2>
      <div id="inference-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Metrics</h2>
      <div id="metrics-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Memory</h2>
      <div id="memory-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Knowledge</h2>
      <div id="knowledge-mount"><div class="skel" aria-hidden="true"><span></span><span></span><span></span></div></div>
    </div>
    <div class="panel pad">
      <h2>Approvals</h2>
      <div id="approvals"><div class="empty">Nothing waiting approval.</div></div>
    </div>
    <div class="panel pad">
      <h2>Activity log</h2>
      <pre id="logs" class="logs">—</pre>
    </div>
  </aside>
</main>
<div id="toast" role="status" aria-live="polite"></div>

<script>
let mode = 'chat';
let conversations = [];
let activeId = null;
const HIST_KEY = 'praxis.chats.v1';
let providers = [];
const HINTS = {
  chat: 'Conversational chat with your model.',
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
  mode = m;
  ['chat','ask','research','do','agent'].forEach(x => document.getElementById('seg-'+x).classList.toggle('active', x===m));
  document.getElementById('modeHint').textContent = HINTS[m];
  const ph = {do:'Describe a goal to queue…', ask:'Ask a grounded question…', research:'Ask anything — Praxis will search the web…', agent:'Ask Praxis to do something — it can call tools…'};
  document.getElementById('message').placeholder = ph[m] || 'Message Praxis…  (Enter to send, Shift+Enter for newline)';
}
function newChat(){
  activeId = null;
  messagesEl.innerHTML = '';
  showWelcome();
  renderHistList();
  const ta = document.getElementById('message'); if(ta) ta.focus();
}

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
        else if(ev.type === 'tool_call'){ cards[ev.tool] = addStep('🔧 <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> <span class="muted">running…</span>', 'run'); }
        else if(ev.type === 'tool_result'){ setCard(ev.tool, '✅ <b>'+escapeHtml(ev.tool)+'</b> <span class="muted">'+escapeHtml(ev.preview||'')+'</span>', 'ok'); }
        else if(ev.type === 'approval'){ setCard(ev.tool, '⏸ <b>'+escapeHtml(ev.tool)+'</b><span class="rk">'+escapeHtml(ev.risk||'')+'</span> held for your approval', 'hold'); refresh(); }
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
  ta.value=''; autoGrow(ta);
  appendUser(text);
  const typing = appendTyping(); setBusy(true);
  try {
    if(voiceMode === 'realtime'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
      await rtSendText(text, typing);
    } else if(mode === 'chat'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
      const wire = conv.messages.map(m=>({role:m.role, content:m.content}));
      await streamChat(conv, wire, typing);
    } else if(mode === 'ask'){
      const res = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: text})});
      typing.remove();
      appendAgent(res.text || res.error || 'No answer.', (res.citations||[]).join(', '));
    } else if(mode === 'research'){
      const res = await api('/api/research', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query: text})});
      typing.remove();
      let body = res.text || res.error || 'No answer.';
      if(res.results && res.results.length){
        body += '\n\n**Sources**\n' + res.results.map((r,i)=>(i+1)+'. ['+(r.title||r.url)+']('+r.url+')').join('\n');
      }
      appendAgent(body, (res.citations||[]).length + ' cited');
    } else if(mode === 'agent'){
      const conv = ensureConversation();
      conv.messages.push({role:'user', content:text, ts: Date.now()});
      conv.updated = Date.now(); persistConversations(); renderHistList();
      const wire = conv.messages.map(m=>({role:m.role, content:m.content}));
      await agentChat(conv, wire, typing);
    } else {
      const res = await api('/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({goal: text, max_attempts: 3})});
      typing.remove();
      appendAgent(res.task_id ? ('Queued task **'+res.task_id+'** — watch the Queue panel.') : (res.error || 'Could not queue task.'));
      refresh();
    }
  } catch(e){ typing.remove(); appendAgent('Error: '+e); }
  setBusy(false);
}

/* ---------- composer behavior ---------- */
function autoGrow(ta){ ta.style.height='auto'; ta.style.height = Math.min(ta.scrollHeight, 144)+'px'; }
const _ta = document.getElementById('message');
_ta.addEventListener('input', ()=>autoGrow(_ta));
_ta.addEventListener('keydown', e => { if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); document.getElementById('send').click(); } });

/* ---------- model picker ---------- */
async function loadProviders(){
  providers = await api('/api/providers');
  const sel = document.getElementById('prov');
  sel.innerHTML = providers.map(p=>'<option value="'+p.id+'">'+escapeHtml(p.label)+'</option>').join('');
  sel.onchange = onProvChange; onProvChange();
}
function onProvChange(){
  const p = providers.find(x=>x.id===document.getElementById('prov').value); if(!p) return;
  document.getElementById('modelList').innerHTML = (p.models||[]).map(m=>'<option value="'+escapeHtml(m)+'"></option>').join('');
  document.getElementById('modelInput').value = (p.models && p.models[0]) || '';
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
    div.innerHTML = '<div class="task-id">'+t.task_id+'</div><div class="task-goal"></div><div class="task-status">'+t.status+'</div>';
    div.querySelector('.task-goal').textContent = t.goal + (t.error ? ' — '+t.error : '');
    taskEl.appendChild(div);
  });
  const appr = await api('/api/approvals');
  const apprEl = document.getElementById('approvals');
  apprEl.innerHTML = (appr && appr.length) ? '' : '<div class="empty">Nothing waiting approval.</div>';
  (appr||[]).forEach(a => {
    const div = document.createElement('div'); div.className = 'approval';
    div.innerHTML = '<div class="task-id">'+a.approval_id+'</div><div class="task-goal"></div><div class="task-status"></div><button class="primary">Approve</button>';
    div.querySelector('.task-goal').textContent = a.tool;
    div.querySelector('.task-status').textContent = a.preview || '';
    div.querySelector('button').onclick = () => approve(a.approval_id);
    apprEl.appendChild(div);
  });
  const logs = await fetch('/log').then(r => r.text()).catch(()=> '');
  document.getElementById('logs').textContent = logs || '—';
}
async function approve(id){
  const res = await api('/api/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({approval_id: id})});
  showToast(res.approved ? ('Approved '+id) : ('Could not approve'+(res.error?': '+res.error:''))); refresh();
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
                approved = self.daemon.approve(approval_id)
                self._json_response({"approved": bool(approved), "approval_id": approval_id})
                return
            if self.path == "/api/deny":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                aid = payload.get("approval_id", "")
                self._json_response({"denied": self.daemon.deny_approval(aid),
                                     "approval_id": aid})
                return
            if self.path == "/api/killswitch":
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode() or "{}")
                self._json_response(self.daemon.killswitch_set(
                    bool(payload.get("engaged", False))))
                return
            if self.path == "/api/compliance":
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
                if action == "set":
                    # Secrets are only accepted from localhost — defense in depth
                    # for a 0.0.0.0-bound container (the dashboard has no auth yet).
                    if not self._is_loopback():
                        self._json_response(
                            {"error": "setting keys is only allowed from localhost"},
                            status=403)
                        return
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
            self.send_response(404)
            self.end_headers()
        except Exception as exc:
            self._error_response(exc)

    def do_GET(self) -> None:
        try:
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
                body = json.dumps(self.daemon.board_list(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/readiness":
                body = json.dumps(self.daemon.readiness(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/api/sources":
                body = json.dumps(self.daemon.sources_list(), default=str).encode()
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
                 ".json": "application/json"}.get(
                     target.suffix, "application/octet-stream")
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
        max_upload_bytes: int | None = None,
    ) -> None:
        self.store = store
        self.agent = agent
        self._mcp_clients: list = []
        self.manager = manager or (TaskManager(store) if store else None)
        self.llm = llm or LLMClient()
        self.tick_interval = tick_interval
        self.idle_interval = idle_interval
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
        self._log_buffer: list[str] = []
        # Open SSE subscriber queues, one per /events connection. Guarded by
        # _sse_lock because request handlers run on independent threads.
        self._sse_clients: list[Queue[dict[str, _T] | None]] = []
        self._sse_lock = threading.Lock()
        self.state = _read_state()
        self.state.running = False
        self._setup_signal_handlers()

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

    def _ensure_agent(self) -> None:
        if self.agent is not None:
            return
        self.agent = PraxisAgent.persistent(llm=self.llm, work_dir=self.work_dir)
        self.store = self.agent.store
        if self.manager is None:
            self.manager = TaskManager(self.store)
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
        self._start_status_server()
        self._log("info", f"daemon started on {self.status_host}:{self.status_port}")
        try:
            while self.running and not self._stop_event.is_set():
                self.tick()
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
        """Process one ready task. If none are ready, run a heartbeat cycle."""
        if self.agent is None:
            self._ensure_agent()
        assert self.agent is not None
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
        assert self.manager is not None
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
        """Enqueue a new task. Safe to call from the CLI while the daemon runs."""
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

    def board_list(self) -> dict:
        """Cards + lane vocabulary for the governed Work Board."""
        if self.store is None:
            return {"cards": [], "lanes": list(self._BOARD_LANES)}
        return {"cards": self.store.list_cards(), "lanes": list(self._BOARD_LANES)}

    def board_create(self, title: str, goal: str = "") -> dict:
        self._ensure_agent()
        assert self.store is not None
        title = (title or goal or "").strip()
        goal = (goal or title).strip()
        if not goal:
            return {"error": "title/goal required"}
        card_id = f"card-{uuid.uuid4().hex[:10]}"
        self.store.add_card(card_id, title, goal, lane="backlog")
        return {"card": self.store.get_card(card_id)}

    def board_move(self, card_id: str, lane: str) -> dict:
        if self.store is None:
            return {"error": "no store"}
        if lane not in self._BOARD_LANES:
            return {"error": f"invalid lane '{lane}'"}
        if self.store.get_card(card_id) is None:
            return {"error": "card not found"}
        self.store.move_card(card_id, lane)
        return {"card": self.store.get_card(card_id)}

    def board_run(self, card_id: str) -> dict:
        """Execute a card's goal under governance and reflect the verdict back onto
        the card's lane (done / held / failed) — the kanban *is* the workflow."""
        self._ensure_agent()
        assert self.store is not None
        card = self.store.get_card(card_id)
        if card is None:
            return {"error": "card not found"}
        self.store.move_card(card_id, "running")
        result = self.agent_run(card["goal"])
        lane = {"completed": "done", "partial": "done", "needs_approval": "held",
                "failed": "failed"}.get(str(result.get("status", "")), "done")
        self.store.set_card_run(card_id, str(result.get("run_id", "")),
                                str(result.get("status", "")), lane)
        return {"card": self.store.get_card(card_id), "result": result}

    def board_delete(self, card_id: str) -> dict:
        if self.store is None:
            return {"error": "no store"}
        self.store.delete_card(card_id)
        return {"deleted": card_id}

    # ----------------------------------------------------------- safety center
    def deny_approval(self, approval_id: str) -> bool:
        """Reject a held consequential action (it is never executed)."""
        self._ensure_agent()
        assert self.agent is not None
        self.agent.broker.reject(approval_id)
        return True

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

        Like agent_run, but chat is user-driven, so it only *accrues* spend — it
        does not increment the autonomous-run counter or block on the cap.
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
        self._ensure_agent()
        assert self.agent is not None
        if hasattr(self.agent.llm, "reset_usage"):
            self.agent.llm.reset_usage()
        text = self.agent.llm.chat(messages, system=self._chat_system(system),
                                   role="general")
        self._bill_chat()
        return {"text": text, "model": cfg.get_default_model() or "mock (offline)"}

    def _chat_system(self, system: str | None) -> str:
        """The chat persona, with the active vertical pack's persona prepended."""
        from . import pack
        return system or pack.compose_system(_CHAT_SYSTEM)

    def chat_stream(self, messages: list[dict],
                    system: str | None = None) -> Iterator[str]:
        """Streaming multi-turn chat — yields assistant text deltas.

        Stateless like :meth:`chat` (the browser owns history). The model label
        is read from config by the HTTP layer and sent as a leading meta event
        ahead of the first delta.
        """
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
        self._ensure_agent()
        assert self.agent is not None
        from .chat_agent import ChatEngine, GovernedChatAgent
        from .reflexion import ReflexionConfig, ReflexiveChatAgent
        from .verifier import VerificationConfig, VerifiedChatAgent
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
            # denied action is rejected and revised once.
            engine = VerifiedChatAgent(engine, max_revisions=vc.max_revisions)
        grounded = self._ground_with_memory(system or _AGENT_SYSTEM, messages)
        grounded = self._ground_with_skills(grounded, messages)
        for event in engine.run(messages, system=grounded):
            yield {"type": event.type, **event.data}

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
        return {
            "model": model or "mock (offline)",
            "configured": model is not None,
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
        from .providers import CATALOG, ORDER
        out: list[dict] = []
        for pid in ORDER:
            p = CATALOG[pid]
            out.append({
                "id": p.id, "label": p.label, "needs_key": p.needs_key,
                "key_env": p.key_env, "base_url": p.base_url,
                "models": list(p.suggested_models), "notes": p.notes,
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
