# Praxis Command Deck — Dashboard Scope

A scoped extension of the Praxis chat/dashboard designed to **exceed OpenClaw and
Hermes** in usefulness, design, and functionality. Grounded in a source-level
teardown of both (see `files/openclaw-teardown.md`, `files/hermes-teardown.md` in
the session, and the summary below).

The wedge: both rivals are excellent *action* consoles but **governance is bolted
on**. Praxis is governance-native (broker, audit, egress/taint, eval flywheel,
plan-execute DAG, deep-think/debate). The Command Deck makes that the product.

---

## 1. Competitive teardown (what we must beat)

### OpenClaw — Control UI (`ui/`, Vite + Lit web components, one Gateway process)
- **Surfaces:** browser Control UI (~19 tabs), Live Canvas / A2UI (agent-driven
  UI protocol), macOS menu-bar (SwiftUI), Windows Hub (WinUI 3), iOS/Android nodes.
- **Strengths:** model **failover** (`auto` vs `user` source, 5-min reprobe);
  rich **Usage** analytics (daily cost, time-of-day heatmap, session drill-down);
  **Live Canvas/A2UI** bidirectional agent↔UI; ed25519 **device pairing**;
  schema-driven config editor; 19 locales; clean dark design-token system
  (accent `#ff5c5c`, `color-mix`, 3 themes + tweakcn).
- **Gaps:** **no true kanban** ("Workboard" is a flat card/list with health
  badges); **Activity feed is ephemeral** (resets on reload, no durable trace);
  **no real-time per-session cost**; **no run/trace DAG**; **no command palette**;
  exec-approval management is CLI/native-prompt heavy (no web approval queue);
  Dreams/memory is a black box.

### Hermes — multi-surface (Python backend + FastAPI dashboard + Electron desktop)
- **Surfaces:** Rich CLI, **Node.js TUI** (subprocess), **Electron Desktop**
  (React/Vite/Tailwind, nanostores, Codicon), **FastAPI Web Dashboard** (13+
  pages), Tauri installer, "Hermes Mod" skin editor, 20+ messaging bots.
- **Strengths:** **multi-surface session coherence** (`/handoff`); status-bar
  **token-economics HUD** (`model │ 12.4K/200K │ 6% │ $0.06 │ 15m`); background
  **learning-loop curator**; **cron delivers to any platform** with model
  snapshot + fail-closed billing guard; **`/agents` live subagent tree** with
  per-branch cost/token rollups; **smart (LLM-assessed) approvals**; **FTS5
  session search**; sticky per-device model pill; remote-backend desktop.
- **Gaps:** **no kanban**; **no budget caps / cost control** (visibility ≠
  control); **no trajectory/trace viewer UI**; memory is two tiny flat files
  (2,200 / 1,375 chars) with no visual editor; **web chat is a PTY passthrough**
  (xterm.js over ANSI, not a native UI; broken on native Windows); cron is
  single-prompt, **not composable DAGs**; **no multi-user/team**; dashboard is
  **5s polling, not push**; Electron is 200–400 MB.

### The white space neither fills
1. A **true kanban that actually executes** governed workflows.
2. A **durable, replayable run/trace DAG** (Hermes is live-only; OpenClaw is
   ephemeral).
3. **Cost control** — budgets/ceilings that *halt or alert*, not just display.
4. A **web approval queue** with inline approve/deny + audit viewer.
5. **Push-based** live updates everywhere (Hermes polls).
6. A **tiered-memory studio** with provenance + visual editor.
7. A **⌘K command palette** + global governed search.

---

## 2. Current Praxis baseline

The daemon already serves a single-page console at `/` (`_DASHBOARD_HTML` in
`daemon.py`) over SSE (`/events`) and JSON endpoints: chat/stream, model +
provider pickers, durable task queue, approvals list, realtime voice, agent
run/card. **Solid plumbing, thin surface** — it is a *monolithic inline HTML*
string with **no board, no metrics charts, no trace graph, no audit viewer**.
That is the launch pad.

---

## 3. Design goals

- **Governance-native:** every surface foregrounds the broker, audit, egress
  firewall, eval gate, and approval flow — our durable advantage.
- **Push, not poll:** one live event bus (SSE now, WebSocket upgrade) drives all
  panels — beats Hermes's 5s lag.
- **Durable + replayable:** persist the event/trace stream (`persistence.py` WAL)
  so "what did it do 3 hours ago" is answerable — beats OpenClaw's ephemeral
  Activity.
- **Dependency-light:** keep the **stdlib-only core**; ship the frontend as a
  small modular static bundle (no Electron, no 200 MB runtime) — beats Hermes on
  footprint while matching OpenClaw's single-process simplicity.
- **Keyboard-first, dark-first, themeable, WCAG 2.1 AA**, responsive.

---

## 4. The eight pillars (each beats both rivals)

### P1 — Live Run Graph (durable trace DAG)  ⭐ observability crown
Render the governed loop **perceive → plan → govern → act → reflect** as a live,
zoomable DAG: plan-execute steps, deep-think rounds, debate panels, subagents,
tool calls, broker decisions. Click any node for inputs/outputs, model, tokens,
cost, latency, citations, audit entry. **Durable + replayable** timeline scrubber.
> Beats OpenClaw (ephemeral flat Activity) and Hermes (live-only `/agents` tree,
> no post-hoc trace UI).

### P2 — Governed Work Board (true kanban → real execution)  ⭐ the wedge
Columns map to **governed loop states**: *Backlog → Planned → Awaiting Approval →
Running → Held → Done / Failed*. Cards are goals, plans, cron jobs, or A2A runs.
**Dragging a card into Running invokes `PlanExecutor`**; approval gates resolve
**inline on the card**; health badges (running/stale/blocked/failed) borrowed from
OpenClaw's Workboard; templates + quick-create. WIP limits, dependencies, fan-out.
> Neither rival has this — OpenClaw's Workboard is a flat list; Hermes cron is a
> single-prompt list with no board and no DAG.

### P3 — Approvals & Safety Center
A live **approval queue**: one-click approve/deny, risk class, arg/diff preview,
**egress-taint flags**, dual-approval, **kill-switch**, **operator-selectable
compliance modes** (enforced/autonomous/permissive, with timed auto-revert),
idempotency view. Optional
**smart triage** (LLM risk assessment) à la Hermes. Plus an **audit-trail viewer**
with secret redaction and export.
> Beats OpenClaw (exec approvals are CLI/native-prompt, no web queue, no web audit
> viewer) and Hermes (inline prompts, no centralized web queue/audit).

### P4 — Inference Control Center
Model/provider catalog + **router visualization** (per-role / per-sensitivity
routing from `router.py`), **failover chains** (adopt OpenClaw `auto` vs `user`
source), **sticky session model pill** (adopt Hermes), hybrid-inference A/B, and
**real-time cost + enforceable budget caps** that halt/alert at a ceiling, plus
**eval-score-per-model**. Live token/cost HUD per run (adopt Hermes status bar).
> Adds the **cost control** and **routing viz** neither rival has; pairs with the
> queued LLM-wiring / contextual-model work.

### P5 — Observability & Metrics
Adopt OpenClaw Usage richness (cost time-series, time-of-day heatmap, session
drill-down) + Hermes analytics, then add **governance metrics neither has**:
**eval pass-rate trend (30/30 over time)**, approval/deny rates, injection &
egress-taint blocks, abstention rate, broker-decision mix, latency P50/P99. All
**push-based**.

### P6 — Memory & Knowledge Studio
Visual browser of **tiered memory** (working / episodic / durable) with
**provenance**, a **consolidation timeline**, **contradiction flags**, and a
RAG/skills explorer (BM25 + embedding hybrid). Inline viewer/editor behind the
memory-write **approval gate**.
> Beats OpenClaw (Dreams black box) and Hermes (two tiny flat files, no visual
> editor).

### P7 — Command Palette + Global Search
⌘K palette to run any command, jump to any panel, or execute a governed action;
**FTS over sessions, memory, and audit** (adopt Hermes FTS5). Neither rival has a
command palette.

### P8 — Design system & shell
Refactor the monolithic inline HTML into a **modular static bundle** with a
dark-first "Command Deck" design-token system (match OpenClaw's token rigor +
Hermes's "flat, not boxed" elevation), themes, reduced-motion, AA contrast,
keyboard nav, responsive/mobile. Optional **A2UI-style** agent-rendered panels as
a later differentiator.

---

## 5. Architecture

Extend the existing daemon; **no new heavyweight deps**.

- **Event/trace bus:** promote `/events` SSE to a typed, **durable** event stream
  persisted in `persistence.py` (WAL); add replay (`/api/traces`,
  `/api/traces/{run_id}`). WebSocket upgrade path for bi-directional.
- **New endpoints:** `/api/board` (CRUD + `POST /api/board/{id}/run` → PlanExecutor),
  `/api/traces`, `/api/metrics`, `/api/audit`, `/api/memory`, `/api/budget`,
  `/api/router` (routing snapshot). Reuse existing `/api/model`, `/api/providers`,
  `/api/approvals`, `/api/approve`, `/api/tasks`, `/api/agent/*`.
- **Frontend:** small modular ES-module bundle (vanilla or a tiny Lit-like layer),
  built to `hybridagent/web/` and served by the daemon — preserves the
  single-process, dependency-light story (explicit contrast to Hermes Electron).
- **Security:** keep loopback-default; reuse onboarding auth; never render
  MIP-classified content to unprotected destinations; redact secrets in audit/UI.

---

## 6. Phased roadmap

**Status (2026-06-28): D1–D6 all shipped on `main`.**

| Phase | Deliverable | Why first |
|---|---|---|
| **D1** | Modular shell + durable push event bus + **Live Run Graph (P1)** | Observability crown; unlocks every other panel |
| **D2** | **Governed Work Board (P2)** + **Approvals & Safety Center (P3)** | The kanban-executes-workflow wedge + governance moat |
| **D3** | **Inference Control Center (P4)** + budgets + **Metrics (P5)** | Pairs with LLM wiring / contextual model work |
| **D4** | **Memory Studio (P6)** + **Command Palette (P7)** + design polish (P8) | Depth + delight |
| **D4.1** | **Kill-switch hardening** — persisted across restarts + halts new runs | A real emergency brake before live inference |
| **D5a** | **Real token-cost accounting** — budget bills actual provider usage | Cost *control*, not a placeholder |
| **D5b** | **Routing observability** — per-run model / tokens / cost / fallbacks | Make contextual routing legible |
| **D6** | **Adaptive cascade inference** — cheap-first, escalate on low confidence | Runtime hybrid inference under budget |

Each phase ships behind the existing green gate (ruff + mypy + pytest + `praxis
eval` 30/30) and adds eval cases + tests for new endpoints.

---

## 7. How we exceed both (scorecard)

| Capability | OpenClaw | Hermes | **Praxis Command Deck** |
|---|---|---|---|
| True kanban that executes workflows | ⚠️ flat board | ❌ | ✅ governed-loop lanes → PlanExecutor |
| Durable, replayable run/trace DAG | ❌ ephemeral | ⚠️ live-only | ✅ persisted + scrubber |
| Web approval queue + audit viewer | ⚠️ CLI-heavy | ⚠️ inline only | ✅ queue + redacted audit |
| Cost **control** (budgets/halt) | ❌ daily view | ❌ HUD only | ✅ enforceable caps + alerts |
| Routing visualization | ❌ | ❌ | ✅ role/sensitivity router viz |
| Governance metrics (eval/injection/abstain) | ❌ | ❌ | ✅ first-class |
| Push vs poll | ✅ WS | ❌ 5s poll | ✅ durable push bus |
| Tiered-memory studio + editor | ❌ black box | ⚠️ 2 flat files | ✅ tiers + provenance + editor |
| Command palette + global search | ❌ | ⚠️ FTS only | ✅ ⌘K + governed FTS |
| Footprint | ✅ 1 process | ❌ Electron | ✅ stdlib-light single process |

Borrow their best (failover sourcing, Usage heatmaps, sticky model pill, status
HUD, smart approvals, FTS, design tokens); win on the seven things **neither**
does — all anchored to Praxis's governance spine.
