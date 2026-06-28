# Praxis — Capabilities

Praxis is a **governed, self‑improving autonomous AI colleague**. It fuses a
proactive, local‑first action ecosystem with persistent memory, editorial
judgment, and self‑improvement — all behind a **governance broker** so the result
is proactive *and* safe.

**Design principles**

- **Governed by construction** — every action (native or external) is
  risk‑classified and routed through one broker: read/draft run autonomously,
  send/destructive are held for human approval.
- **Dependency‑free core** — the runtime needs no third‑party packages (offline
  deterministic mock LLM); richer features are opt‑in extras. Runs on **Linux,
  macOS, and Windows** (Python 3.10+), verified in CI on all three.
- **Quality‑gated** — every capability is covered by an offline eval and the full
  suite is a CI gate (currently **30/30**), plus a regression gate against a
  baseline.

The loop: **perceive → plan → govern → act/draft → reflect → consolidate**.

---

## 1. Governance & safety spine

The control plane every other capability flows through (`broker.py`,
`validation.py`, `content_guard.py`, `compliance.py`).

- **Risk‑classified tools** — `READ` / `DRAFT` are autonomous; `SEND` /
  `DESTRUCTIVE` are **held for human approval** (draft‑before‑send).
- **Persistent approval queue** — held actions survive restarts; **dual‑approval
  (four‑eyes)** for destructive actions, with single‑signer enforcement.
- **Approval idempotency** — an identical re‑proposed consequential action reuses
  its pending approval instead of queuing a duplicate (no double‑execution),
  scoped to the live broker session.
- **Allowlist + least privilege** and a **kill‑switch** that disables all
  consequential tools instantly — **persisted across restarts** and gating new
  runs outright, so an engaged brake survives a crash instead of silently releasing.
- **Schema validation** — malformed tool arguments are rejected before the broker
  ever sees them.
- **Prompt‑injection boundary** — retrieved/tool content is treated as *data,
  never instruction*; injection is detected and secrets are redacted.
- **External‑content quarantine** — a tool result that trips the injection
  detector is wrapped in an explicit data boundary before it re‑enters the model.
- **Egress firewall** — a consequential action whose arguments would relay
  injection‑flagged content back out is **denied** (anti‑exfiltration).
- **Compliance attestation** + a full, attributable **audit trail**.
- **Sensitivity‑aware routing** — content classified as sensitive never leaves
  the machine (pinned to local models).

## 2. Agentic inference layer

The capability layer on top of the spine.

- **Streaming responses** — live token SSE end‑to‑end (`llm.py`, `daemon.py`).
- **Governed tool‑calling loop (ReAct)** — the model proposes tools →
  schema‑validated → broker‑authorized → executed/held/denied; tool results feed
  back until a final answer (`chat_agent.py`).
- **Reflexion (bounded self‑correction)** — a turn that dead‑ends (step‑budget
  exhausted, empty answer, provider error) is retried once with a self‑critique,
  never re‑running held or side‑effecting turns (`reflexion.py`).
- **Verification (critic gate)** — a confident answer that misreports a held or
  denied action as done is caught and (where safe) revised (`verifier.py`).
- **Long‑context compaction** — conversational and **tool‑loop‑pairing‑aware**
  compaction keep long multi‑tool turns within budget without orphaning a
  tool_call from its result (`context.py`).
- **Learned model routing** — a transparent, stdlib goal→role classifier trained
  from governed outcome history, with a heuristic fallback and an injection‑pin
  safety invariant (`router_model.py`, `router.py`); difficulty/sensitivity
  routing across local + cloud providers.
- **Real inference cost accounting** — the spend budget bills actual provider
  token usage (per-model pricing; local/mock models are free), so the cap controls
  real cost rather than a placeholder estimate (`pricing.py`, `llm.py`).
- **Routing observability** — every run records which model handled it,
  local-vs-cloud, tokens, cost, fallbacks, and adaptive-cascade escalations,
  surfaced in the Inference Control Center's *Recent routing* view (`run_routing`
  in `persistence.py`, `daemon.py`).

## 3. Reasoning & deliberation

- **Multi‑agent debate** — best‑of‑N stance‑diverse solvers + a majority‑vote
  (self‑consistency) judge, verifier‑filtered (`debate.py`).
- **Deep‑think mode** — difficulty‑gated, **multi‑round** deliberation: if the
  solvers disagree, they debate again seeing each other's attempts, then the
  result is verified (`deepthink.py`). Composes routing + debate + verification.
- **Adaptive cascade inference** — the runtime counterpart to a-priori difficulty
  routing: run the cheaper routed tier first and **escalate to the strongest tier
  only when the answer is low-confidence *and* the budget allows** — modern hybrid
  inference kept under the governance budget (`escalation.py`); wired into both
  grounded Q&A and agent_run planning, and recorded per run for the dashboard.

## 4. Retrieval & memory

- **Hybrid retrieval** — fuses **BM25 lexical** ranking with **embedding** vector
  ranking via **Reciprocal Rank Fusion**, so exact‑term and semantic matches both
  surface and retrieval stays strong even offline (`rag.py`, `bm25.py`,
  `vecsim.py`).
- **Multi‑tier memory** — working / episodic / durable, with provenance,
  salience, decay, expiry purge, and right‑to‑be‑forgotten; recall ranks with
  BM25 (`memory.py`).
- **RAG knowledge base** — ingest documents (text + optional PDF/Office/media),
  chunk, embed, retrieve; **cite‑or‑abstain** grounded Q&A with contradiction
  detection (`rag.py`, `grounding.py`, `contradiction.py`, `ingest.py`).
- **Auto‑grounded chat** — Agent‑mode turns are automatically grounded in
  recalled **memory** and **skills**.

## 5. Procedural skill memory (self‑improvement)

- **Skill distillation** — distill a reusable, triggerable procedure from a goal
  and its action trace; stored as `SKILL.md` with provenance (`skills.py`).
- **Procedural recall in the loop** — relevant learned skills are retrieved
  (BM25 + embeddings) and injected into the governed turn, so recurring tasks
  benefit from prior learning.
- **Skill quality control** — outcomes are recorded and low‑quality skills are
  auto‑quarantined (`skill_evaluator.py`).

## 6. Multi‑agent orchestration

- **Scoped subagents** — narrowed tool registries per role, all under the shared
  governance spine (`orchestrator.py`).
- **Concurrent fan‑out** — run several goals concurrently over a thread‑safe
  store (`praxis fanout`).
- **Hierarchical plan‑and‑execute** — decompose a goal into a **dependency DAG**
  of governed steps, execute with per‑step monitoring, **skip dependents** of a
  failed/held step, and **replan** a failed step's remainder (bounded)
  (`plan_execute.py`). Status: completed / needs_approval / partial / failed.
- **Inter‑agent scratchpad** and **persistent, resumable tasks**
  (`scratchpad.py`, `task_manager.py`).

## 7. Tools & extensibility

- **Dependency‑free MCP client** — consume tools from **any external MCP server**
  with no extra dependencies (stdlib JSON‑RPC over stdio). External tools are
  **untrusted**: risk‑classified (annotations → name → config override; unknown
  defaults to *held*) and broker‑gated. Wired into the live agent (`mcp_client.py`).
- **MCP server** — expose Praxis tools to Claude/Copilot/any MCP host
  (`mcp_adapter.py`, optional `mcp` extra).
- **A2A — callable agent** — other agents/systems invoke Praxis over HTTP
  (`POST /api/agent/run`, `GET /api/agent/card`): hand it a goal, get back a
  **governed** result (status, steps, held approvals); discover its capabilities
  via an agent card (`agent_service.py`).
- **Governed browser / computer‑use** — navigate/read (autonomous) vs click/type
  (consequential) (`browser.py`, optional `browser` extra).
- **Web, files, and Microsoft 365** — fetch_url, search_web, read/write file,
  list_dir, calendar/mail (`real_tools.py`, `m365_tools.py`, `wiki_safe.py`).

## 8. Interfaces & multimodal

- **CLI** — 40+ commands (`praxis ...`): `handle`, `ask`, `plan-run`, `think`,
  `debate`, `fanout`, `router-train`, `recall`, `ingest`, `eval`, `mcp`,
  `daemon`, and more (`cli.py`).
- **Web dashboard + daemon** — long‑running worker with a single‑page dashboard
  (Chat / Ask / Do / Agent modes), SSE streaming, approvals, and a status API
  (`daemon.py`). The **Command Deck** surfaces the governed loop as panels over
  one shared SSE stream: **Live Run Graph** (durable, replayable run DAG), governed
  **Work Board** (kanban-that-executes), **Approvals & Safety Center** (queue +
  redacted audit + persistent kill-switch), **Inference Control Center**
  (model/router, enforceable budget, per-run routing + cost), **Observability
  Metrics** (governance decision mix + spend trend & per-model cost), **Memory
  Studio**, and a `Ctrl/Cmd+K` **command palette** (`hybridagent/web/`). Panel
  overlays are keyboard-accessible (Escape-to-close, dialog roles).
- **Voice** — turn‑based and **realtime** (mic → transcribe → governed turn →
  audio) over a hand‑rolled, dependency‑free WebSocket, with an OpenAI Realtime
  bridge; operator‑selectable per agent config (`voice.py`, `wsutil.py`).
- **Multimodal** — vision (image → text) and speech‑to‑text (`multimodal.py`,
  optional `multimodal` extra).
- **Model‑agnostic** — OpenAI, Anthropic, Ollama (local), OpenRouter, and more
  (`providers.py`); offline deterministic mock by default.

## 9. Quality flywheel

- **Offline eval suite** — deterministic capability + safety scenarios run against
  the *real* governance machinery and an offline mock LLM (`evals.py`).
- **Regression gate** — persist runs, set a baseline, and **fail CI on any
  regression** even if the overall suite still passes; JSON artifact + run history
  (`eval_history.py`, `praxis eval --json/--save/--set-baseline/--check/--history`).
- **Mutation‑tested governance core** — a strong oracle of broker‑guard tests.

### Eval categories (30/30)

| Category | Cases | Covers |
|---|---|---|
| tool_use | 2 | draft executes, read autonomous |
| approval | 2 | send held, destructive dual‑approval |
| safety | 7 | kill‑switch, allowlist, injection flag, redaction, tool‑result quarantine, approval idempotency, egress firewall |
| schema | 1 | malformed args rejected |
| routing | 2 | difficulty tiers, learned goal→role |
| context | 2 | conversation + tool‑loop compaction |
| retrieval | 2 | BM25 ranking, hybrid RRF fusion |
| skills | 1 | procedural recall injection |
| orchestration | 1 | concurrent scoped subagents |
| reasoning | 1 | deep‑think deliberation |
| planning | 2 | replan recovery, consequential step held |
| verification | 1 | false‑claim caught + revised |
| debate | 1 | majority‑vote consensus |
| mcp | 1 | external tool risk‑classified + held |
| a2a | 1 | governed run + capability card |
| voice | 1 | turn/realtime backends selectable |
| browser | 1 | navigate/read vs click/type risk |

---

## Install

```bash
# one command (Linux / macOS)
curl -fsSL https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.sh | bash
# Windows (PowerShell)
irm https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.ps1 | iex
# or, in a clone
pip install .            # core is dependency‑free
praxis demo              # offline demo
praxis onboard           # pick a provider + model
```

Optional extras: `pip install ".[docs,multimodal,fast,mcp,browser]"`.

## Quick tour

```bash
praxis handle "Prepare a customer follow-up email"   # one governed cycle
praxis plan-run "follow up with the customer about the report"  # plan + execute
praxis think "Analyze the trade-offs and design the cache layer" # deliberate
praxis debate "What is the best rollout strategy?"   # best-of-N + judge
praxis ask "What did we decide about Q4 pricing?"    # grounded, cite-or-abstain
praxis ingest ./notes.md && praxis recall "pricing"  # RAG knowledge base
praxis eval --check                                  # regression gate
praxis daemon                                        # dashboard + HTTP/A2A API
```
