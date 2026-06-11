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

## Setup

Requires **Python 3.10+**. No third-party packages are needed to run (offline
mock LLM); `pytest` is the only dev dependency.

```bash
# 1. clone
git clone https://github.com/smfworks/smf-praxis.git
cd smf-praxis

# 2. (recommended) create a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

# 3. install (editable) with the `praxis` CLI + dev tools
pip install -e ".[dev]"
```

Everything runs offline with a deterministic mock LLM — no API keys required.

## Configure a model provider

Praxis mirrors **OpenClaw's onboarding model**. Run the wizard to pick a
provider and model — it's offered automatically on first use, or run it anytime:

```bash
praxis onboard
```

The wizard walks you through:
1. **Existing-config detection** — Keep / Modify / Reset (like `openclaw onboard`).
2. **Pick a provider** — Ollama · OpenRouter · GitHub Models · OpenAI · Anthropic · xAI (Grok) · Vercel AI Gateway · Custom (OpenAI-compatible).
3. **Pick a model** — suggestions per provider (Ollama models are auto-discovered from the local host), or enter one manually.
4. **Key storage** — environment-variable reference (recommended; nothing secret on disk) or paste-now (stored in `~/.praxis/auth-profiles.json`, gitignored).

Config is written OpenClaw-style to `~/.praxis/praxis.json` (override the dir
with `PRAXIS_HOME`):

```json
{
  "agents": { "defaults": { "model": "openrouter/openai/gpt-4o-mini" } },
  "providers": {
    "openrouter": {
      "baseUrl": "https://openrouter.ai/api/v1",
      "compatibility": "openai",
      "keyRef": { "source": "env", "id": "OPENROUTER_API_KEY" }
    }
  }
}
```

Non-interactive (scripts/CI):

```bash
praxis onboard --provider ollama --model llama3.1
praxis onboard --provider openrouter --model "openai/gpt-4o-mini"   # uses OPENROUTER_API_KEY
```

**Model selection (`PRAXIS_LLM`):** `auto` (default — use the configured
provider if onboarded, else offline mock) · `mock` (always offline) · `real`
(always use the provider).

## Quick start

```bash
python demo.py          # offline, mock LLM
pytest -q               # 11 tests
```

## CLI

After `pip install -e .` the `praxis` command is available (or run
`python -m hybridagent.cli ...` without installing):

```bash
praxis tui                                       # interactive full-screen menu UI
praxis demo                                      # bundled demo
praxis handle "Prepare a customer follow-up email after today's sync"
praxis handle "<goal>" --approve-all             # auto-approve held sends (dev only)
praxis heartbeat --watch "scan for urgent follow-ups"
praxis remember "Michael prefers concise briefs" --kind preference
praxis --help
```

| Command | What it does |
|---|---|
| `praxis tui` | launch the **interactive terminal UI** (menu-driven, stdlib-only) |
| `praxis handle "<goal>"` | run one full `perceive→…→consolidate` cycle; prints actions, held approvals, reflection |
| `praxis handle ... --approve-all` | auto-approve consequential actions (dev convenience) |
| `praxis heartbeat [--watch "<goal>"]` | proactive always-on tick |
| `praxis remember "<fact>" --kind {preference,fact,decision,skill,note}` | store durable memory |
| `praxis demo` | run the full bundled demo |

## Tests & CI

`pytest -q` runs the 11-test suite. GitHub Actions
(`.github/workflows/ci.yml`) runs tests on Python 3.10–3.12 plus a demo/CLI
smoke test on every push and PR to `main`.

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
