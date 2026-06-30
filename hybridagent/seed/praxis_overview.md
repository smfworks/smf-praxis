# Praxis — Quick Reference

Praxis is a governed, self-improving autonomous AI colleague. It pairs a
proactive local-first action ecosystem with persistent memory, retrieval, and
self-improvement, all behind a governance broker so the result is proactive
*and* safe.

## The autonomy loop
Praxis runs a perceive -> plan -> govern -> act/draft -> reflect -> consolidate
loop. Every action is risk-classified and routed through one governance broker:
READ and DRAFT actions run autonomously, while SEND and DESTRUCTIVE actions are
held for human approval (draft-before-send).

## Governance and safety
The broker enforces a tool allowlist with least privilege, a persistent approval
queue that survives restarts, dual-approval (four-eyes) for destructive actions,
and a kill-switch that disables all consequential tools and survives a restart.
Operator-selectable compliance modes (enforced, autonomous, permissive) set the
approval posture, with a timed auto-revert that fails safe back to enforced.
Retrieved and tool content is treated as data, never instruction: prompt
injection is detected, secrets are redacted, and an egress firewall blocks a
consequential action that would relay injection-flagged content back out.

## Retrieval and the knowledge base
Praxis answers questions grounded in a knowledge base using hybrid retrieval:
BM25 lexical ranking fused with embedding vectors via Reciprocal Rank Fusion.
Grounded answers cite their sources or abstain when evidence is insufficient.
Knowledge sources (the LLM wiki / RAG repositories) can be folders, files, or
URLs, each registered in its own namespace and refreshed on a schedule. Add
them from the dashboard Knowledge panel or with `praxis wiki-add` / `praxis
ingest`.

## Memory
Multi-tier memory (working, episodic, durable) persists across sessions with
provenance, salience, and decay. Relevant memory is recalled into each governed
turn automatically.

## Research
Praxis can search the web and fetch URLs. Web search works out of the box with a
keyless DuckDuckGo default; configure Tavily, Brave, or SerpAPI with an API key
for higher-quality results.

## Interfaces
A CLI (`praxis ...`) exposes handle, ask, plan-run, think, debate, fanout,
ingest, recall, eval, daemon, doctor, and more. The web dashboard (Command Deck)
provides Chat / Ask / Do / Agent modes, SSE streaming, an approvals and safety
center, an inference control center, observability metrics, a memory studio, and
a knowledge panel.

## Readiness
Run `praxis doctor` (or open the dashboard) to see a readiness checklist:
language model, persistent memory, web research, knowledge base, embedding
model, and skill recall.
