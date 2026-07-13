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
  suite is a CI gate (currently **40/40**), plus a regression gate against a
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
- **Operator‑selectable compliance modes** — the approval gate is a runtime,
  persisted posture: **enforced** (default; hold send/destructive for approval),
  **autonomous** (run without approval — egress firewall + injection detection +
  kill‑switch stay on), or **permissive** (guards off, kill‑switch only; for
  trusted/sandboxed use). Relaxed modes support a **timed auto‑revert** that fails
  safe back to enforced. Set from the Safety Center or `praxis governance`.
- **Schema validation** — malformed tool arguments are rejected before the broker
  ever sees them.
- **Prompt‑injection boundary** — retrieved/tool content is treated as *data,
  never instruction*; injection is detected and secrets are redacted.
- **External‑content quarantine** — a tool result that trips the injection
  detector is wrapped in an explicit data boundary before it re‑enters the model.
- **Egress firewall** — a consequential action whose arguments would relay
  injection‑flagged content back out is **denied** (anti‑exfiltration).
- **External policy hook (OPA/Rego/Cedar‑ready)** — an operator can plug a custom
  policy callable into the broker. A hook **deny** is an absolute veto; a hook
  **allow** can waive *human approval* but never the allowlist, kill‑switch, or
  egress firewall — and a broken hook **fails safe** (deny). Policy‑as‑code without
  weakening the safety spine (`broker.py`).
- **Skill & MCP‑tool security scanning** — installed skills and external MCP tool
  definitions are statically scanned (shell injection, secret exfiltration,
  prompt‑injection directives, suspicious URLs, obfuscation) and graded A–F;
  critical content is refused at `SkillLibrary.add` and poisoned MCP tools are
  skipped. Includes an offline‑tolerant **OSV dependency check** (`security_scan.py`).
- **Signed agent identity & attestations** — each agent has a stable cryptographic
  identity; actions/messages can be attributed and tamper‑checked. HMAC‑SHA256 by
  default (stdlib), auto‑upgrading to **Ed25519** when `cryptography` is present;
  identity file is restricted to the current user cross‑platform (`identity.py`).
- **Sandboxed execution** — shell/code execution runs through a pluggable isolation
  backend: `local` (host shell, cross‑platform), `docker` (throwaway container,
  cap‑drop ALL, no‑new‑privileges, `--network none`, read‑only rootfs, host‑uid
  mapping), or remote `ssh`/`modal`/`daytona`. The `run_shell` tool is
  `DESTRUCTIVE` (held) and sandboxed by construction (`sandbox.py`).
- **OWASP Agentic Top‑10 coverage matrix** — an auditable map of the AAI001–010
  agentic threats to the concrete Praxis controls (`docs/OWASP_AGENTIC_COVERAGE.md`).
- **Compliance attestation** + a full, attributable **audit trail**.
- **Sensitivity‑aware routing** — content classified as sensitive never leaves
  the machine (pinned to local models).
- **Cross‑platform secret protection** — credential/identity files are restricted
  to the current user on every OS: `chmod 0600` on POSIX, `icacls` ACLs on Windows
  (no false 0600 assurance) (`config.secure_file`).

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

## 4.5. Professional Artifact Studio

- **Canonical professional-document IR** — strict frozen models for metadata,
  confidentiality, sections, paragraphs, lists, tables, figures, page breaks,
  citations, immutable sources, revisions, reviews, and signatures. Canonical
  UTF-8 JSON is the content-identity surface; unknown fields, coercions, floats,
  malformed unions, dangling references, and invalid accessibility semantics fail
  closed (`artifacts/models.py`, `artifacts/validation.py`).
- **Deterministic multi-format rendering** — exact JSON and Markdown in the
  dependency-free core; lazy optional DOCX/PDF/PPTX/XLSX renderers with fixed
  metadata and normalized OpenXML packages. Renderers accept caller-bound asset
  bytes and never fetch external resources (`artifacts/render_*.py`).
- **Tenant-scoped append-only versioning** — organization/workspace ownership,
  expected-head CAS, sequential parent-linked revisions, immutable assets,
  restart durability, one-winner concurrent writes, and semantic comparison of
  metadata, sections, blocks, citations, sources, reviews, and signatures
  (`artifacts/service.py`, `artifacts/versions.py`, `persistence.py`).
- **Governed professional release** — current-head validation, supported material
  claims, exact approved `professional_release` review, signature binding, active
  signer/role revalidation, successful requested renderers, and exact durable
  run/checkpoint provenance are required in one atomic release transaction.
  Scoped idempotency returns the original immutable result for retries and rejects
  conflicting requests.
- **Self-verifying release packages** — deterministic content-addressed ZIP bundles
  contain canonical IR, selected renders, assets, claims/evidence, reviews,
  signatures, run/checkpoint provenance, and validation. Verification rejects hash
  or size mismatches, duplicate/case-colliding/absolute/traversing paths, symlinks,
  unbounded members, malformed canonical manifests, unexpected renders, and
  tampering (`artifacts/bundles.py`).

## 5. Procedural skill memory (self‑improvement)

- **Skill distillation** — distill a reusable, triggerable procedure from a goal
  and its action trace; stored as `SKILL.md` with provenance (`skills.py`).
- **Procedural recall in the loop** — relevant learned skills are retrieved
  (BM25 + embeddings) and injected into the governed turn, so recurring tasks
  benefit from prior learning.
- **Skill quality control** — outcomes are recorded and low‑quality skills are
  auto‑quarantined (`skill_evaluator.py`).
- **Evolutionary self‑improvement (PR‑gated)** — Praxis optimizes the skills it
  keeps: a fitness function scored from real governed usage history drives a
  reflective LLM rewrite (or an offline heuristic), guarded by security scan +
  size caps + a ≥50% semantic‑preservation check + strict fitness improvement.
  It **proposes** a diff; applying it is a separate, reviewed step — never a silent
  self‑edit (`evolution.py`, `praxis evolve`).

## 6. Multi‑agent orchestration

- **Scoped subagents** — narrowed tool registries per role, all under the shared
  governance spine (`orchestrator.py`).
- **Model‑callable delegation** — the agent can spawn a scoped subagent mid‑run via
  the `delegate` tool (`DRAFT`: autonomous, but the subagent's own `SEND`/
  `DESTRUCTIVE` calls are still held); recursion is prevented structurally because
  subagent role allowlists never include `delegate` (`real_tools.py`).
- **Concurrent fan‑out** — run several goals concurrently over a thread‑safe
  store (`praxis fanout`).
- **Scheduled autonomy (cron)** — recurring unattended jobs with interval
  (`30m`/`2h`), keyword (`daily`/`hourly`/`weekly`), `daily@HH:MM`, and 5‑field
  cron schedules; due jobs are **atomically claimed** (no double‑fire) and run
  through the governed loop, results recorded and rescheduled (`cron.py`,
  `praxis cron`, `/api/cron`).
- **Hierarchical plan‑and‑execute** — decompose a goal into a **dependency DAG**
  of governed steps, execute with per‑step monitoring, **skip dependents** of a
  failed/held step, and **replan** a failed step's remainder (bounded)
  (`plan_execute.py`). Status: completed / needs_approval / partial / failed.
- **Inter‑agent scratchpad** and **persistent, resumable tasks**
  (`scratchpad.py`, `task_manager.py`).

## 7. Tools & extensibility

- **Dependency‑free MCP client** — consume tools from **any external MCP server**
  with no extra dependencies. Supports both **stdio** (JSON‑RPC) and **remote
  Streamable‑HTTP** (JSON + SSE, session IDs, `${ENV}` auth‑header substitution).
  External tools are **untrusted**: risk‑classified (annotations → name → config
  override; unknown defaults to *held*), security‑scanned for poisoning, and
  broker‑gated. Wired into the live agent (`mcp_client.py`).
- **Prebuilt MCP presets** — one‑command enablement of curated servers:
  **xAI Docs** (keyless, READ) and **Peekaboo** (macOS screen/GUI computer‑use;
  see/capture = READ, click/type = SEND‑held). `praxis mcp --list-presets/
  --enable-preset/--probe` (`mcp_presets.py`).
- **MCP server** — expose Praxis tools to Claude/Copilot/any MCP host
  (`mcp_adapter.py`, optional `mcp` extra).
- **A2A — callable agent + client** — other agents invoke Praxis over HTTP
  (`POST /api/agent/run`, `GET /api/agent/card`) for a **governed** result; and
  Praxis can call **other** A2A agents via the `call_agent` tool (`SEND`: held),
  with a bounded (8 MiB) response read so a hostile peer can't exhaust memory
  (`agent_service.py`, `a2a_client.py`).
- **Outbound messaging gateways** — deliver to Telegram / Slack / Discord /
  generic webhook / ntfy via the `send_message` tool (`SEND`: held; draft‑before‑
  send) with `${ENV}` auth substitution and per‑channel formatting
  (`gateways.py`, `praxis message`).
- **Generation tools** — `generate_image` and `text_to_speech` via OpenAI/xAI‑
  compatible providers (`DRAFT`: local artifact), honest when unconfigured
  (`real_tools.py`).
- **Plugin system + marketplace** — drop‑in `~/.praxis/plugins/*.py` plugins
  (disabled by default, **source security‑scanned before import**, tools flow
  through the same broker), plus a publish/search/install marketplace on a local/
  shared registry (scanned at both publish and install, no auto‑run)
  (`plugins.py`, `marketplace.py`, `praxis plugins`, `praxis market`).
- **Credential vault** — named secret bundles scoped per‑tool, injected as env
  vars only for a call's duration (ephemeral, restored after), 0600/ACL‑restricted
  and obfuscated at rest; loudly warns if `PRAXIS_VAULT_KEY` is set without the
  `cryptography` extra rather than silently downgrading (`vault.py`,
  `praxis secrets-bundle`).
- **Governed browser / computer‑use** — navigate/read (autonomous) vs click/type
  (consequential) (`browser.py`, optional `browser` extra); desktop control also
  available via the Peekaboo MCP preset.
- **Web, files, and Microsoft 365** — fetch_url, search_web, query_knowledge,
  read/write file, list_dir, run_shell (sandboxed), calendar/mail (`real_tools.py`,
  `m365_tools.py`, `wiki_safe.py`). **Web search works out of the box** with a
  keyless DuckDuckGo default; Tavily/Brave/SerpAPI are optional upgrades.
- **Model providers** — OpenAI, Anthropic, Ollama **local + Ollama.com cloud**,
  OpenRouter, xAI, **Microsoft Azure AI Foundry** (`azure-foundry`), and more;
  offline deterministic mock by default (`providers.py`).

## Out of the box

A fresh install is usable immediately — no hidden configuration:

- **First-run bootstrap** (`bootstrap.py`) enables memory + skill recall and
  seeds a starter knowledge namespace, so grounded `ask` returns cited content
  on the very first query.
- **Readiness checklist** — `praxis doctor` and the dashboard banner
  (`/api/readiness`, `readiness.py`) report model / memory / web research /
  knowledge base / embedder / skills at a glance, replacing silent failures.
- **Knowledge panel** — register RAG repositories (folder, file, or URL) in
  named namespaces, see indexed-chunk counts, re-index, or remove them
  (`/api/sources`, `web/knowledge.js`). Retrieval spans **every** repository.
- **Research mode** — a first-class dashboard mode that searches the web, reads
  results, and answers with citations (`/api/research`).
- **Keyless web research** and a **keyless local embedder** mean research and
  hybrid retrieval work with zero API keys.

## 8. Interfaces & multimodal

- **CLI** — 40+ commands (`praxis ...`): `handle`, `ask`, `plan-run`, `think`,
  `debate`, `fanout`, `router-train`, `recall`, `ingest`, `eval`, `mcp`,
  `doctor`, `daemon`, and more (`cli.py`).
- **Web dashboard + daemon** — long‑running worker with a single‑page dashboard
  (Chat / Ask / Research / Do / Agent modes), SSE streaming, approvals, and a
  status API (`daemon.py`). The **Command Deck** surfaces the governed loop as
  panels over
  one shared SSE stream: **Live Run Graph** (durable, replayable run DAG), governed
  **Work Board** (kanban-that-executes), **Approvals & Safety Center** (queue +
  redacted audit + persistent kill-switch + **compliance-mode selector** with
  timed auto-revert), **Inference Control Center**
  (model/router, enforceable budget, per-run routing + cost), **Observability
  Metrics** (governance decision mix + spend trend & per-model cost), **Memory
  Studio**, a **Knowledge** panel (manage RAG repositories / the LLM wiki), and a
  `Ctrl/Cmd+K` **command palette** (`hybridagent/web/`). Panel
  overlays are keyboard-accessible (Escape-to-close, dialog roles).
- **Voice** — turn‑based and **realtime** with **live PCM16 microphone streaming**
  (push‑to‑talk) over a persistent, hand‑rolled, dependency‑free WebSocket; each
  turn runs the governed loop and the reply is spoken back. Upstream is the OpenAI
  Realtime API (governed function calls) or an offline loopback; operator‑selectable
  per agent config (`voice.py`, `wsutil.py`).
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
- **Reliability benchmarking** — runs the eval suite ×k and reports
  **pass@1 / pass^k / variance** plus per‑case flaky detection, so reliability
  (not just pass/fail) is measurable (`benchmark.py`, `praxis bench`).
- **Cross‑platform CI matrix** — Linux (3.10/3.11/3.12) + macOS + Windows run the
  full suite; both installers (`install.sh`, `install.ps1`) are executed on their
  platforms and the Docker image + dashboard are smoke‑tested, with an 80%
  coverage gate on Linux (`.github/workflows/ci.yml`).

### Eval categories (40/40)

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
| vertical | 10 | per‑vertical packs ship the promised persona + governance posture |

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
praxis onboard --provider ollama-cloud --model llama3.3   # configure Ollama.com
praxis model set openai/gpt-4o-mini                        # one-line model switch
praxis model list --discover                               # list available local/cloud Ollama models
praxis cron add "summarize overnight alerts" --schedule daily@08:00  # scheduled autonomy
praxis evolve                                        # propose skill improvements (PR-gated)
praxis scan skills                                   # security-scan installed skills
praxis bench -k 5                                    # reliability: pass@1 / pass^k / variance
praxis eval --check                                  # regression gate
praxis daemon                                        # dashboard + HTTP/A2A API
```
