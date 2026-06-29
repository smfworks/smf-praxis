# Praxis Command Deck — Dashboard Scope

A scoped extension of the Praxis chat/dashboard. The wedge: most agent consoles
are excellent *action* surfaces but **governance is bolted on**. Praxis is
governance-native (broker, audit, egress/taint, eval flywheel, plan-execute DAG,
deep-think/debate) and the Command Deck makes that the product.

---

## 1. White space we fill

1. A **true kanban that actually executes** governed workflows.
2. A **durable, replayable run/trace DAG**.
3. **Cost control** — budgets/ceilings that *halt or alert*, not just display.
4. A **web approval queue** with inline approve/deny + audit viewer.
5. **Push-based** live updates everywhere.
6. A **tiered-memory studio** with provenance + visual editor.
7. A **⌘K command palette** + global governed search.

---

## 2. Current Praxis baseline

The daemon serves a single-page console at `/` (`_DASHBOARD_HTML` in `daemon.py`)
over SSE (`/events`) and JSON endpoints: chat/stream, model + provider pickers,
durable task queue, approvals list, realtime voice, agent run/card. **Solid
plumbing, thin surface** — a *monolithic inline HTML* string with **no board, no
metrics charts, no trace graph, no audit viewer**. That is the launch pad.

---

## 3. Design goals

- **Governance-native:** every surface foregrounds the broker, audit, egress
  firewall, eval gate, and approval flow — our durable advantage.
- **Push, not poll:** one live event bus (SSE now, WebSocket upgrade) drives all
  panels — no polling lag.
- **Durable + replayable:** persist the event/trace stream (`persistence.py` WAL)
  so "what did it do 3 hours ago" is answerable.
- **Dependency-light:** keep the **stdlib-only core**; ship the frontend as a
  small modular static bundle (no Electron, no 200 MB runtime) — single-process
  simplicity.
- **Keyboard-first, dark-first, themeable, WCAG 2.1 AA**, responsive.

---

## 4. The eight pillars

### P1 — Live Run Graph (durable trace DAG)  ⭐ observability crown
Render the governed loop **perceive → plan → govern → act → reflect** as a live,
zoomable DAG: plan-execute steps, deep-think rounds, debate panels, subagents,
tool calls, broker decisions. Click any node for inputs/outputs, model, tokens,
cost, latency, citations, audit entry. **Durable + replayable** timeline scrubber.

### P2 — Governed Work Board (true kanban → real execution)  ⭐ the wedge
Columns map to **governed loop states**: *Backlog → Planned → Awaiting Approval →
Running → Held → Done / Failed*. Cards are goals, plans, cron jobs, or A2A runs.
**Dragging a card into Running invokes `PlanExecutor`**; approval gates resolve
**inline on the card**; health badges (running/stale/blocked/failed); templates +
quick-create. WIP limits, dependencies, fan-out.

### P3 — Approvals & Safety Center
A live **approval queue**: one-click approve/deny, risk class, arg/diff preview,
**egress-taint flags**, dual-approval, **kill-switch**, **operator-selectable
compliance modes** (enforced/autonomous/permissive, with timed auto-revert),
idempotency view. Optional **smart triage** (LLM risk assessment). Plus an
**audit-trail viewer** with secret redaction and export.

### P4 — Inference Control Center
Model/provider catalog + **router visualization** (per-role / per-sensitivity
routing from `router.py`), **failover chains** (`auto` vs `user` source),
**sticky session model pill**, hybrid-inference A/B, and
**real-time cost + enforceable budget caps** that halt/alert at a ceiling, plus
**eval-score-per-model**. Live token/cost HUD per run.
> Cost **control** + routing viz; pairs with the contextual-model work.

### P5 — Observability & Metrics
Rich Usage views (cost time-series, time-of-day heatmap, session drill-down) plus
**governance metrics**: **eval pass-rate trend over time**, approval/deny rates,
injection & egress-taint blocks, abstention rate, broker-decision mix, latency
P50/P99. All **push-based**.

### P6 — Memory & Knowledge Studio
Visual browser of **tiered memory** (working / episodic / durable) with
**provenance**, a **consolidation timeline**, **contradiction flags**, and a
RAG/skills explorer (BM25 + embedding hybrid). Inline viewer/editor behind the
memory-write **approval gate**.

### P7 — Command Palette + Global Search
⌘K palette to run any command, jump to any panel, or execute a governed action;
**FTS over sessions, memory, and audit**.

### P8 — Design system & shell
Refactor the monolithic inline HTML into a **modular static bundle** with a
dark-first "Command Deck" design-token system (rigorous tokens, flat elevation),
themes, reduced-motion, AA contrast, keyboard nav, responsive/mobile. Optional
**agent-rendered panels** as a later differentiator.

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
  single-process, dependency-light story (no Electron runtime).
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

## 7. Differentiators

The Command Deck wins on capabilities that consoles rarely combine — all anchored
to Praxis's governance spine:

- **True kanban that executes** governed workflows (lanes → `PlanExecutor`)
- **Durable, replayable run/trace DAG** (persisted + scrubber)
- **Web approval queue + redacted audit viewer**
- **Cost control** — enforceable budget caps that halt/alert
- **Routing visualization** (role/sensitivity)
- **Governance metrics** — eval pass-rate, injection/egress blocks, abstention
- **Durable push bus** (no polling)
- **Tiered-memory studio** with provenance + editor
- **⌘K command palette + governed FTS**
- **stdlib-light single process** footprint
