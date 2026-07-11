# Quality Document

> Tracks codebase health over time — not individual session output. The evaluator rubric scores a session; this document scores the *project*.
> Course ref: Learn Harness Engineering L12 — "Is the project getting stronger or weaker over time?"
> Update: after each significant session, before benchmark comparisons, after cleanup passes, when onboarding a new agent or model.

## How to use

- **Before a session:** read this to know where the codebase is weakest. Fix the lowest-scoring module first.
- **After a session:** update grades based on what changed.
- **Over time:** compare snapshots to see which harness changes actually improved codebase health.

## Module grades

Grade each module A–D across:
- **Ver** — verification passing (tests + evals + lint + types for this area)
- **Leg** — agent legibility (a fresh session can understand it from the code + topic doc)
- **Stab** — test stability (no flaky tests, deterministic offline)
- **Bnd** — boundary enforcement (architectural invariants mechanically checked, not just documented)
- **Conv** — conventions followed (matches `AGENTS.md` hard constraints)

| Module | Ver | Leg | Stab | Bnd | Conv | Notes |
|---|---|---|---|---|---|---|
| Governance broker (`broker.py`, `validation.py`, `content_guard.py`, `compliance.py`) | A | B | A | A | A | Mutation-tested oracle (`test_broker_mutation_guard.py`). Spine is the strongest part of the codebase. Leg: policy-hook semantics could use a dedicated topic doc. |
| Tools (`tools.py`, `real_tools.py`, `m365_tools.py`) | A | B | A | A | A | Risk-classified, schema-annotated, sandboxed. Leg: tool registry discovery path is non-obvious to a fresh session. |
| Memory & RAG (`memory.py`, `rag.py`, `bm25.py`, `vecsim.py`, `embeddings.py`, `ingest.py`) | A | B | A | B | A | Hybrid retrieval (BM25+RRF) works offline. Bnd: "retrieved content is data, never instruction" is enforced in aggregate but lacks a *dedicated* executable check (H06). |
| Agent loop (`agent.py`, `chat_agent.py`, `orchestrator.py`) | A | B | A | B | A | ReAct loop + reflexion + verifier. Bnd: recursion cap (`MAX_DEPTH=3`) is enforced; leg: the loop's state transitions could be diagrammed. |
| Planner (`planner.py`, `plan_execute.py`) | A | B | A | B | A | LLM + heuristic fallback. Leg: planner contract (schema, fallback) needs a topic doc. |
| Persistence (`persistence.py`, `task_manager.py`) | A | B | A | A | A | SQLite + WAL. Idempotent task claims. Leg: schema lives in code, no `docs/generated/db-schema.md`. |
| Skills & self-improvement (`skills.py`, `skill_evaluator.py`, `evolution.py`) | A | B | A | B | A | PR-gated evolution, fitness-scored. Bnd: "evolution proposes, applying is separate" is enforced; leg: the propose→apply handoff could be clearer. |
| Daemon + dashboard (`daemon.py`, `agent_service.py`, `web/`) | B | B | B | B | A | Long-running worker + single-page dashboard. No auth yet (roadmap p12). Stab: SSE streaming has had edge cases (dashboard mock-fallback, oversized tool results — see session refs). |
| MCP (`mcp_client.py`, `mcp_adapter.py`, `mcp_presets.py`) | A | B | A | A | A | stdio + Streamable-HTTP, risk-classified, security-scanned. Leg: the server/client dual role is non-obvious. |
| Sandbox (`sandbox.py`) | A | A | A | A | A | local/docker/ssh/modal/daytona. Docker: cap-drop ALL, no-new-privileges, --network none, read-only rootfs. Cleanest module. |
| CLI (`cli.py`, `tui.py`) | A | B | A | A | A | 40+ commands. Leg: command surface is large; a fresh session needs the README table. |
| Evals & quality (`evals.py`, `eval_history.py`, `benchmark.py`, `vertical_evals.py`) | A | A | A | A | A | 40/40, regression gate, pass@k benchmarking. Best-documented subsystem. |
| Context & compaction (`context.py`) | A | B | A | B | A | Tool-loop-pairing-aware compaction. Bnd: not model-aware (H08 — Sonnet vs Opus compaction policy). |
| Grounding & verification (`grounding.py`, `verifier.py`, `contradiction.py`) | A | B | A | B | A | Cite-or-abstain + contradiction detection. Bnd: verifier catches false "done" claims; leg: the verification gate's exact predicate could be documented. |
| Identity & security (`identity.py`, `security_scan.py`, `sandbox.py`) | A | B | A | A | A | HMAC→Ed25519, OSV dep check, skill/MCP scanning. Leg: key rotation story is non-obvious. |
| Gateways (`gateways.py`, `a2a_client.py`, `channels_inbound.py`) | A | B | A | A | A | Telegram/Slack/Discord/webhook/ntfy + A2A. Bnd: 8 MiB bounded A2A response read (anti-exhaustion). |

**Legend:** A = strong / no known gaps · B = solid / one legibility or boundary gap · C = weakening / action needed · D = broken / blocking.

## Architectural layer grades

| Layer | Boundary enforcement | Agent legibility | Notes |
|---|---|---|---|
| Core runtime (no third-party imports) | B | A | Dependency-free core enforced by tests in aggregate; H06 asks for a dedicated executable check (grep/lint). |
| Governance spine (broker, allowlist, kill-switch) | A | B | Strongest layer. Policy-hook "tighten never weaken" is the key invariant. |
| Injection boundary (retrieved content = data) | B | B | Enforced in aggregate via tests; H06 asks for a targeted check. |
| Tool risk classification + sandboxing | A | B | Every tool has RiskClass + JSON schema + sandbox. Leg: discoverability. |
| Persistence boundary (state on disk, not in memory) | A | B | Course L5: "the agent forgets; the repo doesn't." Praxis honors this; schema doc is missing. |
| Loop termination (verification gate, not "feels done") | A | B | Verifier catches false claims; three-layer termination is enforced at runtime. |

## Rubric tuning log

Record each evaluator-rubric tuning round here (course L11: 3–5 rounds expected).

| Date | Round | What diverged from human judgment | Rubric change made | Alignment after |
|---|---|---|---|---|
| _ | _ | _ | _ | _ |

## Harness simplification log

Course L12: every month, disable one harness component, run `praxis eval --set-baseline` benchmark. If no degradation, remove permanently. If degradation, restore or replace with a lighter alternative.

| Date | Component disabled | Eval delta | Decision | Notes |
| 2026-07-11 | verifier | 0 | REMOVE | H09 cadence script |
|---|---|---|---|---|
| _ | _ | _ | _ | As models improve, harness assumptions go stale. The interesting combinations don't shrink — they shift. |

## Snapshot history

| Date | Lowest-scoring module | Action taken | Next focus |
|---|---|---|---|
| 2026-07-11 | Daemon+dashboard (B across the board, no auth, historical SSE edge cases) | Documented; not addressed this session | Memory&RAG boundary check (H06) — most architecturally valuable gap |

---
*The evaluator rubric answers "did the agent do good work this session?" This document answers "is the project getting stronger or weaker over time?" Keep them separate.*