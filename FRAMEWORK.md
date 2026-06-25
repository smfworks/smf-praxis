# Hybrid Autonomous AI — Framework & Analysis

This document does three things, grounded in the OpenClaw and Hermes M365
integration guides:

1. Lists the **strengths and weaknesses** of OpenClaw AI and Hermes AI.
2. Proposes a **hybrid framework** that uses each system's strengths to cancel
   the other's weaknesses.
3. Points to the **reference build** (`hybridagent/`) that implements it.

---

## 1. OpenClaw AI

### Strengths
- **Local-first, persistent agent runtime** — always-on, runs on the user's
  hardware, owns its own state.
- **Proactive action ecosystem** — heartbeat/cron ticks let it act without being
  prompted (briefs, scans, follow-ups).
- **Broad tool / skills gateway** — MCP + plugins give wide action surface
  (calendar, mail, files, web, automations).
- **Action breadth** — gathers context, drafts, and executes across many
  surfaces; great for high-volume, real-world tasks.
- **Reflection/memory loop** — captures outcomes back into memory.

### Weaknesses
- **Permissionless drift** — local autonomy without a control plane becomes "a
  raccoon with admin rights"; broad scopes are taken for convenience.
- **Prompt-injection exposure** — it reads emails/docs/web, which can smuggle
  instructions ("ignore previous instructions", "send to everyone").
- **Memory hoarding** — tends to store raw content, causing bloat and leaking
  private bodies into durable memory.
- **Weak attribution** — without a broker, M365 actions aren't reliably
  logged/attributable.
- **Breadth over judgment** — strong at *doing*, weaker at editorial *quality*
  and disciplined approval.

## 2. Hermes AI

### Strengths
- **Persistent identity + editorial judgment** — produces polished, review-ready
  work; quality-first.
- **Structured multi-tier memory** — working / episodic / durable layers with
  **provenance** and summaries instead of raw dumps.
- **Self-improvement / consolidation** — distills interactions into durable
  facts and reusable patterns.
- **Native multi-agent orchestration** — designed to coordinate specialized
  collaborators.
- **Discipline** — "autonomy for preparation, approval for consequence";
  draft-before-send by default.

### Weaknesses
- **Less proactive out of the box** — governed and cautious; lacks OpenClaw's
  always-on heartbeat breadth.
- **Broker-dependent** — assumes a control plane exists; less plug-and-play
  action surface on its own.
- **Curation overhead** — disciplined memory needs upkeep and can be slower.
- **Single-colleague framing** — oriented to one assistant, not high-volume
  parallel action.

---

## 3. Hybrid framework: a proactive **and** governed colleague

The two systems are complementary: OpenClaw is **strong where Hermes is weak**
(proactivity, action breadth) and Hermes is **strong where OpenClaw is weak**
(memory discipline, judgment, governance). Fuse them behind a single control
plane.

| Weakness | Eliminated by |
|---|---|
| OpenClaw: permissionless drift | Hermes-style **broker**: tool allowlist, least privilege, **approval gates** |
| OpenClaw: prompt-injection | **Injection boundary** — retrieved content is *data, never instruction* |
| OpenClaw: memory hoarding | Hermes **tiered memory + consolidation** (summarize-not-hoard, provenance) |
| OpenClaw: weak attribution | **Audit trail** on every governed action, with secret redaction |
| Hermes: low proactivity | OpenClaw **heartbeat** + broad **tool gateway** |
| Hermes: broker dependence | Broker shipped as a first-class component |

### The loop

```
perceive  →  plan  →  govern  →  act / draft  →  reflect  →  consolidate
(proactive, (decompose (broker:     (run reads &   (Hermes    (durable facts
 injection-  into tool- autonomy vs  drafts; hold   self-      + skills;
 screened)   bound      approval)    sends/deletes  improve)   clear working)
             steps)                  for approval)
```

**Operating principle (from both guides):**
> Let the agent perceive, prepare, draft, and remember autonomously.
> Require human approval before it sends, shares, deletes, or commits.

### Components (→ module)

| Component | Module |
|---|---|
| Proactive perception (heartbeat, injection screen) | `perception.py`, `agent.heartbeat()` |
| Planner (goal → tool-bound steps) | `planner.py` |
| Governance broker (allowlist, risk class, approval queue, kill-switch, audit, redaction) | `broker.py` |
| Narrow risk-classified tools (read/draft autonomous; send/destructive gated) | `tools.py` |
| Multi-tier memory with provenance | `memory.py` |
| Reflection + consolidation (self-improvement) | `reflection.py` |
| Orchestrating agent loop | `agent.py` |
| Durable state (memory/audit/approvals/vectors) | `persistence.py` |
| RAG (chunk/embed/retrieve) + document ingestion | `rag.py`, `embeddings.py`, `ingest.py` |
| Contextual model routing (role + sensitivity) | `router.py`, `llm.py` |
| Multimodal intake (image/audio/video) | `multimodal.py` |
| Grounding (cite-or-abstain, verify, tool-constrained) | `grounding.py` |
| Skills library + governed `/learn` | `skills.py` |
| Compliance attestation (cycle/decision evidence chain) | `compliance.py`, `persistence.py` |
| Persistent task queue (long-running/resumable work) | `task_manager.py`, `persistence.py` |
| Managed wiki / KB source revalidation | `wiki.py`, `rag.py`, `persistence.py` |
| Skill outcome evaluation / quarantine | `skill_evaluator.py`, `skills.py` |
| Scoped subagent orchestration / predictive routing | `orchestrator.py`, `persistence.py` |
| Safe wiki ingestion (scheme/IP allowlist, size cap) | `wiki_safe.py` |
| Dual approval, JSON-schema validation, retention/decay | `broker.py`, `validation.py`, `memory.py` |
| Contradiction detection, scratchpad, health metrics | `contradiction.py`, `scratchpad.py`, `metrics.py` |
| Cached numpy vector index + WAL store | `vecsim.py`, `rag.py`, `persistence.py` |

---

## 4. Reference build

`hybridagent/` implements the framework and runs **offline** (mock LLM).

```bash
./install.sh          # one command: venv + install + onboarding (see README)
praxis demo           # full loop incl. injection + kill-switch demos
python -m pytest -q   # test suite
```

The same governance + memory spine scales out to the multi-agent case in the
companion **Clawmes Orchestrator** (swarm spawning); this project is the
single-colleague foundation.

### Production wiring
- Implement `LLMClient._complete_real` (local/cloud model); set `PRAXIS_LLM=real`. ✅ done (`providers.py`).
- Replace mock tools in `tools.py` with real M365 broker/Graph calls. ✅ via `m365_tools.py`.
- Durable memory + audit + held approvals + RAG vectors persist to
  `~/.praxis/praxis.db` (`persistence.py`); semantic recall via `rag.py`.
- Provider calls retry with backoff and log structured events; held actions carry
  a TTL. Kill-switch still needs wiring to a real disable path.

### Build phases (post-review roadmap)
1. **Foundations** — SQLite persistence, resilience (retry/backoff/logging), TTLs. ✅
2. **RAG + ingestion** — embeddings, vector store, PDF/Office/email parsers. ✅
3. **Model router + multimodal** — role/sensitivity routing; image/audio/video intake. ✅
4. **Grounding** — cite-or-abstain, structured outputs, verification pass. ✅
5. **Skills + `/learn`** — persistent skill store, governed learn command, retrieval. ✅
6.–15. **Memory, persistent tasks, LLM wiki, subagents, compliance spine, security
   hardening, regulated controls, quality gates, RAG performance** — see the
   README "Recent additions / quality" sections. ✅
16. **Test hardening** — Hypothesis parser fuzzing (`tests/test_fuzz_parsers.py`),
   provider wire tests against a stub server, a gated real-Ollama integration
   test, and cosmic-ray mutation testing of the governance broker
   (`scripts/mutation_test.py`, oracle `tests/test_broker_mutation_guard.py`). ✅
