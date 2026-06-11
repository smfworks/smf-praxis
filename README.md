# Praxis — Hybrid Autonomous AI Colleague

A single autonomous agent that fuses **OpenClaw's** proactive, local-first
action ecosystem with **Hermes'** persistent multi-tier memory, editorial
judgment, and self-improvement — behind a **governance broker** so it is
proactive *and* safe.

> **Principle:** autonomy for preparation, approval for consequence.

See **[FRAMEWORK.md](FRAMEWORK.md)** for the full strengths/weaknesses analysis
of OpenClaw and Hermes and the design rationale.

## The loop

```
perceive → plan → govern → act/draft → reflect → consolidate
```

- **perceive** — proactively pull calendar/mail/file context; screen every
  signal so retrieved content is *data, never instruction*.
- **plan** — decompose the goal into tool-bound steps.
- **govern** — the broker classifies each step: read/draft run autonomously;
  send/destructive are **held for approval**; tools are allowlisted; a
  kill-switch disables consequential actions.
- **act/draft** — execute autonomous steps; queue consequential ones.
- **reflect / consolidate** — distill outcomes into durable facts + reusable
  skills with provenance, then clear working memory (summarize-not-hoard).

## Quick start

```bash
cd hybrid-autonomous-agent
python demo.py          # offline, mock LLM
python -m pytest -q     # 11 tests
```

## Minimal usage

```python
from hybridagent import PraxisAgent

agent = PraxisAgent()
agent.learn("Michael prefers drafts for customer follow-ups, not direct sends.",
            kind="preference", provenance="setup")

report = agent.handle("Prepare a customer follow-up email after today's sync")
print(report.summary())            # reads+drafts done; send is HELD
for appr in report.pending_approvals:
    print(agent.approve(appr["approval_id"]))   # human approves -> executes

agent.heartbeat()                  # proactive always-on tick
print(agent.memory.stats())        # working/episodic/durable/skills
```

## What it demonstrates

- ✅ Reads & drafts happen autonomously
- ✅ Sends & deletes are held for human approval (draft-before-send)
- ✅ Prompt injection in retrieved content is flagged and treated as data
- ✅ Kill-switch blocks consequential actions while reads still work
- ✅ Tiered memory consolidates into durable facts + skills with provenance
- ✅ Every governed action is audited (secrets redacted)

## Relationship to Clawmes Orchestrator

This is the **single-colleague foundation**. The same governance + memory spine
scales to many parallel specialized agents in the companion **Clawmes
Orchestrator** (sub-agent swarm spawning).
