# Praxis — Design Rationale & Framework

This document does three things:

1. States the **design tensions** Praxis resolves.
2. Describes the **framework** — a proactive *and* governed colleague.
3. Points to the **reference build** (`hybridagent/`) that implements it.

---

## 1. The design tension

An effective autonomous colleague pulls in two directions:

- **Proactivity & breadth** — always-on, local-first, acts without being prompted
  (briefs, scans, follow-ups) across calendar, mail, files, and web. Left
  unchecked this becomes permissionless drift, prompt-injection exposure, raw
  memory hoarding, and weak attribution.
- **Judgment & governance** — polished, review-ready work; tiered memory with
  provenance; "autonomy for preparation, approval for consequence." Left alone
  this is cautious and low-throughput.

Praxis fuses both behind one control plane: proactive where it's safe, governed
where it matters.

---

## 2. Framework: a proactive **and** governed colleague

| Risk | Eliminated by |
|---|---|
| Permissionless drift | **Broker**: tool allowlist, least privilege, **approval gates** |
| Prompt-injection | **Injection boundary** — retrieved content is *data, never instruction* |
| Memory hoarding | **Tiered memory + consolidation** (summarize-not-hoard, provenance) |
| Weak attribution | **Audit trail** on every governed action, with secret redaction |
| Low proactivity | **Heartbeat** + broad **tool gateway** |
| Broker dependence | Broker shipped as a first-class component |

### The loop

```
perceive  →  plan  →  govern  →  act / draft  →  reflect  →  consolidate
(proactive, (decompose (broker:     (run reads &   (self-     (durable facts
 injection-  into tool- autonomy vs  drafts; hold   improve)   + skills;
 screened)   bound      approval)    sends/deletes             clear working)
             steps)                  for approval)
```

**Operating principle:**
> Let the agent perceive, prepare, draft, and remember autonomously.
> Require human approval before it sends, shares, deletes, or commits.

### Components (→ module)

| Component | Module |
|---|---|
| Proactive perception (heartbeat, injection screen) | `perception.py`, `agent.heartbeat()` |
| Planner (goal → tool-bound steps) | `planner.py` |
| Governance broker (allowlist, risk class, approval queue, compliance modes, kill-switch, audit, redaction) | `broker.py` |
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

The same governance + memory spine scales out to the multi-agent case in a
companion orchestrator (swarm spawning); this project is the single-colleague
foundation.

### Production wiring
- Implement `LLMClient._complete_real` (local/cloud model); set `PRAXIS_LLM=real`. ✅ done (`providers.py`).
- Replace mock tools in `tools.py` with real M365 broker/Graph calls. ✅ via `m365_tools.py`.
- Durable memory + audit + held approvals + RAG vectors persist to
  `~/.praxis/praxis.db` (`persistence.py`); semantic recall via `rag.py`.
- Provider calls retry with backoff and log structured events; held actions carry
  a TTL. The kill-switch persists across restarts and halts new runs outright, not
  just consequential tools. The approval gate itself is an operator-selectable,
  persisted compliance mode (enforced / autonomous / permissive) with optional
  timed auto-revert back to enforced; the kill-switch overrides every mode.

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
