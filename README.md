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
pytest -q               # test suite
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
praxis m365                                      # check the M365 broker connection
praxis handle "Draft a customer follow-up and send it" --m365   # act on real M365 (via broker)
praxis --help
```

| Command | What it does |
|---|---|
| `praxis tui` | launch the **interactive terminal UI** (menu-driven, stdlib-only) |
| `praxis handle "<goal>"` | run one full `perceive→…→consolidate` cycle; prints actions, held approvals, reflection |
| `praxis handle ... --approve-all` | auto-approve consequential actions (dev convenience) |
| `praxis handle ... --m365` | run against **live Microsoft 365** through the broker |
| `praxis heartbeat [--watch "<goal>"]` | proactive always-on tick |
| `praxis remember "<fact>" --kind {preference,fact,decision,skill,note}` | store durable memory (persisted to `~/.praxis/praxis.db`) |
| `praxis approvals` | list held consequential actions (persisted across runs) |
| `praxis approve <id> --approved-by <name> --notes "<why>"` | approve + execute a held action by id, recording operator/justification |
| `praxis compliance` | render an audit attestation over cycles, approvals, and consequential actions |
| `praxis task-create "<goal>"` | create a persistent resumable task |
| `praxis tasks` | list persistent tasks and statuses |
| `praxis task-run <id>` | run one task attempt (records cycle/result) |
| `praxis task-cancel <id>` | cancel a queued/runnable task |
| `praxis wiki-add <path-or-url>` | register a KB/wiki source for periodic revalidation |
| `praxis wiki-sources` | list registered KB/wiki sources and freshness status |
| `praxis wiki-refresh [source-id]` | refresh one source or all due sources into RAG |
| `praxis ingest <paths…>` | ingest PDF/Word/PowerPoint/Excel/email/HTML/text into the RAG knowledge base |
| `praxis recall "<query>"` | semantic search over the ingested knowledge base |
| `praxis ask "<question>"` | grounded Q&A over KB + memory — cites sources or abstains |
| `praxis describe <path>` | extract text from a document or caption/transcribe a media file |
| `praxis route` | show contextual model routing per role + sensitivity |
| `praxis learn "<goal>"` | distill a reusable skill (`/learn`); saved only on approval (`--yes`) |
| `praxis skills` | list saved skills |
| `praxis skill <name>` | show a saved skill |
| `praxis m365` | check broker health + signed-in status |
| `praxis demo` | run the full bundled demo |

## Compliance spine

Every persistent run now receives a `cycle_id`; every governed decision receives a
`decision_id`. Praxis writes a durable compliance event chain to
`~/.praxis/praxis.db` so auditors can trace:

```
signal evidence -> plan step -> broker decision -> held approval -> execution
```

Held approvals carry rationale and source evidence bundles, and approvals can
record an operator and justification (`--approved-by`, `--notes`). `praxis
compliance` renders an attestation proving recorded SEND/DESTRUCTIVE actions were
approved, pending, or denied before execution.

## Persistent tasks

Long-running work can be placed into a durable task queue. Tasks track status,
attempt count, retry timing, last `cycle_id`, result metadata, and errors in the
SQLite store, so work can be resumed after process restarts or handed to a future
background scheduler.

```bash
praxis task-create "Review recent mail and save a brief"
praxis tasks
praxis task-run task-abc123def0
praxis task-cancel task-abc123def0
```

## Managed wiki / KB sources

Praxis can register durable knowledge sources (files now; URL/wiki-like sources
via the same registry) with refresh intervals, content hashes, status, and
change-detection. `wiki-refresh` re-ingests only changed sources and keeps the RAG
knowledge base fresh without re-embedding unchanged pages.

```bash
praxis wiki-add ./docs/clinical-policy.md --refresh-hours 24
praxis wiki-sources
praxis wiki-refresh
praxis recall "clinical policy evidence requirements"
```

Durable memory now carries salience, access counts, freshness/TTL metadata, and
recall updates access statistics so future ranking can favor high-value, recently
used facts.

## Skills library (`/learn`)

Praxis builds a curated, reusable skills library — Hermes-style. `praxis learn`
(or `/learn` in the TUI) distills a goal into a named, triggerable **skill**
(`SKILL.md` with frontmatter + steps) and indexes it for semantic retrieval.
Because saving a skill changes future behavior, it's a **governed** act: Praxis
drafts autonomously but only persists after you approve (`--yes`, or `y` at the
prompt). Saved skills are stored under `~/.praxis/skills/<name>/SKILL.md`, and the
relevant ones are retrieved and folded into perception on every cycle, so the
agent's capability compounds over time.

```bash
praxis learn "Prepare and send a customer follow-up after a sync" --yes
praxis skills
praxis skill prepare-and-send-a-customer-follow-up
```

## Grounded, non-hallucinating answers

`praxis ask` answers **only** from retrieved sources (knowledge base + durable
memory). Every claim is cited `[S#]`; when the sources don't support an answer it
returns **`INSUFFICIENT_EVIDENCE`** instead of guessing. Offline the answer is
purely *extractive* (it copies supporting sentences, so it cannot fabricate); the
real-model path uses a strict source-only system prompt at temperature 0 plus a
verification pass that flags any claim not backed by a source. The LLM planner
(`GroundedPlanner`) similarly drops any step that names a tool outside the
registry, so it can never invent tools.

## Model routing & multimodal

Praxis routes each model call by **role** (planner / summarizer / vision /
transcribe / general) and **data sensitivity**. Configure per-role models under
`agents.roles` in `praxis.json`; anything classified sensitive (secrets, SSNs,
card numbers, "confidential" markers) is pinned to a **local** model or the
offline mock and is **never sent to a cloud provider**. On error the client falls
back to the next candidate. Inspect the matrix with `praxis route`.

Images, audio, and video are first-class inputs (`praxis describe <file>` or
`praxis ingest <file>`). Offline, Praxis emits honest *metadata* (size, duration,
dimensions) and never fabricates a description or transcript; set `PRAXIS_MM=real`
with a vision model (`agents.roles.vision`) and speech-to-text (local Whisper or
`agents.roles.transcribe`) to caption/transcribe for real. Extracted text flows
into the same RAG + perception pipeline, injection-screened like any document.

## Knowledge base (RAG)

Praxis grounds its work in your documents. Ingested files are chunked, embedded,
and stored in a local SQLite vector table (`~/.praxis/praxis.db`); relevant
chunks are retrieved into **perception** each cycle and injection-screened like
any other read (retrieved content is *data, never instruction*).

```bash
praxis ingest report.pdf notes.docx deck.pptx data.xlsx thread.eml
praxis recall "Q3 revenue follow-up for the customer"
```

Embeddings and parsers are **offline-first**: a deterministic mock embedder needs
no model or network, so RAG works out of the box. Plain text, Markdown, CSV/JSON,
HTML, and `.eml` parse with the standard library; PDF/Word/PowerPoint/Excel/`.msg`
need the optional extra (`pip install "praxis-agent[docs]"`). Point at a real
embedding model by setting `agents.defaults.embedModel` (e.g.
`ollama/nomic-embed-text`) and `PRAXIS_EMBED=real`.

## Microsoft 365 (via the broker)

Praxis acts on your calendar/mail/files **only through the OpenClaw M365 Access
Broker** — a separate local control plane that enforces auth, least-privilege
scopes, an allowlist, approval gates, an injection firewall, and a hash-chained
audit log. It works against **any tenant you control, including your personal
M365/Entra tenant** — no work environment required.

```bash
praxis m365                                                 # verify broker connection
praxis handle "Prepare a customer follow-up and send it" --m365
```

Reads & drafts run autonomously; **send/share/delete are held** until you
approve — at which point Praxis (as host UI) mints the broker's single-use,
tool-scoped approval token and executes. The agent key alone can never send,
share, or delete. Full setup (broker start, env vars, going live against your
tenant) is in **[M365-SETUP.md](M365-SETUP.md)**.


## Tests & CI

`pytest -q` runs the test suite. GitHub Actions
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
