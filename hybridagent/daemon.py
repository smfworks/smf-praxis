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
import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
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

# Persona for the conversational chat surface (/api/chat). Kept short; the rich
# governance/agent behavior lives in the task pipeline, this is a direct,
# helpful conversation with the configured model.
_CHAT_SYSTEM = (
    "You are Praxis, a hybrid autonomous AI colleague. Be helpful, accurate, and "
    "concise. Use Markdown (headings, **bold**, lists, and fenced code blocks) to "
    "format answers clearly. If you are unsure, say so rather than inventing facts."
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

main { display: grid; grid-template-columns: 1fr 22rem; gap: 1rem; padding: 1rem; max-width: 1500px; margin: 0 auto; }
@media (max-width: 980px) { main { grid-template-columns: 1fr; } aside { order: 2; } }
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

/* composer */
.composer { border-top: 1px solid var(--border); padding: .85rem 1rem; display: flex; gap: .6rem; align-items: flex-end; background: rgba(10,12,16,.4); }
#message { flex: 1; resize: none; max-height: 9rem; min-height: 2.7rem; padding: .7rem .9rem; border-radius: .8rem; border: 1px solid var(--border); background: var(--bg); color: var(--text); font: inherit; font-size: .95rem; line-height: 1.5; outline: none; transition: border-color .15s; }
#message:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(91,141,239,.15); }
.send-btn { display: grid; place-items: center; width: 2.7rem; height: 2.7rem; border: none; border-radius: .8rem; background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; cursor: pointer; flex: none; box-shadow: 0 6px 16px rgba(91,141,239,.35); transition: transform .12s; }
.send-btn:hover { transform: translateY(-1px); }
.send-btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }

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
.empty { color: var(--faint); font-size: .85rem; }

#toast { position: fixed; bottom: 1.25rem; left: 50%; transform: translateX(-50%) translateY(2rem); background: var(--panel2); border: 1px solid var(--border); color: var(--text); padding: .6rem 1rem; border-radius: .7rem; box-shadow: var(--shadow); font-size: .85rem; opacity: 0; transition: all .25s ease; pointer-events: none; z-index: 50; }
#toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<header>
  <div class="brand"><span class="logo"></span> Praxis</div>
  <span class="pill modelpill"><span class="dot"></span><span id="modelBadge">—</span></span>
  <span class="spacer"></span>
  <span id="status" class="badge">checking…</span>
</header>

<main>
  <section id="chat" class="panel">
    <div class="chat-top">
      <div class="segmented">
        <button id="seg-chat" class="active" onclick="setMode('chat')">Chat</button>
        <button id="seg-ask" onclick="setMode('ask')">Ask</button>
        <button id="seg-do" onclick="setMode('do')">Do</button>
      </div>
      <span class="hint" id="modeHint">Conversational chat with your model.</span>
      <button class="ghost" onclick="clearChat()">Clear</button>
    </div>
    <div id="messages" class="messages"></div>
    <form class="composer" onsubmit="sendMessage(event)">
      <textarea id="message" rows="1" placeholder="Message Praxis…  (Enter to send, Shift+Enter for newline)" autocomplete="off"></textarea>
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
    </div>
    <div class="panel pad">
      <h2>Queue</h2>
      <div id="tasks"><div class="empty">No tasks yet.</div></div>
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
<div id="toast"></div>

<script>
let mode = 'chat';
let chatHistory = [];
let providers = [];
const HINTS = {
  chat: 'Conversational chat with your model.',
  ask: 'Grounded Q&A over the knowledge base — cites sources or abstains.',
  do: 'Queue an autonomous task for the agent to work.'
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
function timeNow(){ return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
function appendUser(text){
  clearWelcome();
  const row = document.createElement('div'); row.className = 'msg user';
  row.innerHTML = '<div class="avatar">You</div><div class="bubble-wrap"><div class="bubble"></div><div class="meta">'+timeNow()+'</div></div>';
  row.querySelector('.bubble').textContent = text;
  messagesEl.appendChild(row); scrollDown();
}
function appendAgent(text, meta){
  clearWelcome();
  const row = document.createElement('div'); row.className = 'msg agent';
  row.innerHTML = '<div class="avatar">P</div><div class="bubble-wrap"><div class="bubble"></div><div class="meta"></div></div>';
  row.querySelector('.bubble').innerHTML = renderMarkdown(text);
  row.querySelector('.meta').textContent = (meta ? meta+' · ' : '') + timeNow();
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
  ['chat','ask','do'].forEach(x => document.getElementById('seg-'+x).classList.toggle('active', x===m));
  document.getElementById('modeHint').textContent = HINTS[m];
  document.getElementById('message').placeholder = m==='do' ? 'Describe a goal to queue…' : (m==='ask' ? 'Ask a grounded question…' : 'Message Praxis…  (Enter to send, Shift+Enter for newline)');
}
function clearChat(){ chatHistory = []; messagesEl.innerHTML = ''; showWelcome(); }
function showWelcome(){
  messagesEl.innerHTML = '<div class="welcome"><h3>Talk to Praxis</h3><p>Chat with your configured model, ask grounded questions, or queue autonomous tasks. Switch models any time from the panel on the right.</p><div class="chips">'
    + ['Explain the governance broker','Draft a customer follow-up email','Summarize my open tasks'].map(c=>'<button class="chip" onclick="useChip(this)">'+c+'</button>').join('')
    + '</div></div>';
}
function useChip(el){ const ta = document.getElementById('message'); ta.value = el.textContent; ta.focus(); autoGrow(ta); }

function setBusy(b){ document.getElementById('send').disabled = b; }

async function sendMessage(ev){
  ev.preventDefault();
  const ta = document.getElementById('message');
  const text = ta.value.trim(); if(!text) return;
  ta.value=''; autoGrow(ta);
  appendUser(text);
  const typing = appendTyping(); setBusy(true);
  try {
    if(mode === 'chat'){
      chatHistory.push({role:'user', content:text});
      const res = await api('/api/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({messages: chatHistory})});
      typing.remove();
      const reply = res.text || res.error || 'No response.';
      chatHistory.push({role:'assistant', content: reply});
      appendAgent(reply, res.model || '');
    } else if(mode === 'ask'){
      const res = await api('/api/ask', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({question: text})});
      typing.remove();
      appendAgent(res.text || res.error || 'No answer.', (res.citations||[]).join(', '));
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
async function loadModel(){ const m = await api('/api/model'); document.getElementById('modelBadge').textContent = m.model || 'mock'; }

let _toastT;
function showToast(msg){ const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(_toastT); _toastT = setTimeout(()=>t.classList.remove('show'), 2600); }

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

/* ---------- boot ---------- */
showWelcome();
loadProviders();
loadModel();
refresh();
setInterval(refresh, 4000);
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
            self.send_response(404)
            self.end_headers()
        except Exception as exc:
            self._error_response(exc)

    def do_GET(self) -> None:
        try:
            if self.path == "/status":
                mgr = self.daemon.manager
                body = json.dumps({
                    "running": self.daemon.running,
                    "port": self.daemon.status_port,
                    "state": self.daemon.state.to_dict(),
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
            elif self.path == "/events":
                self._serve_sse()
                return
            elif self.path == "/upload":
                self._handle_upload()
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

    def ask(self, question: str, k: int = 5) -> Any:
        """Answer a question grounded in the agent's KB and memory."""
        self._ensure_agent()
        assert self.agent is not None
        return self.agent.ask(question, k=k, refresh_wiki=False)

    def chat(self, messages: list[dict], system: str | None = None) -> dict:
        """Hold a multi-turn conversation with the configured model.

        ``messages`` is the full client-side transcript (``[{role, content}]``);
        the daemon is stateless here so the browser owns history. Returns the
        assistant reply plus the model that produced it.
        """
        self._ensure_agent()
        assert self.agent is not None
        text = self.agent.llm.chat(messages, system=system or _CHAT_SYSTEM,
                                   role="general")
        return {"text": text, "model": cfg.get_default_model() or "mock (offline)"}

    def model_info(self) -> dict:
        """Current default model + whether a real provider is configured."""
        model = cfg.get_default_model()
        return {
            "model": model or "mock (offline)",
            "configured": model is not None,
            "embed_model": cfg.get_embed_model(),
        }

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
