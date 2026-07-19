# Vertical Packs — Complete Guide

A **vertical pack** is a single, shareable bundle that retargets Praxis for a domain
(legal, medical, forensic, homeschool, …). Activating a pack changes the agent's
**persona**, **governance posture**, **tool allowlist**, **knowledge**, **skills**,
**model**, **eval suite**, and **dashboard theme** — without touching code.

This guide is written for two readers:

- **A person** who wants to install, activate, and create packs.
- **An AI agent** asked to *build, extend, or repair* the pack system. The
  "AI quick reference" and "Extending to a new domain" sections are the contract —
  follow them literally and the test/eval gates will pass.

---

## 1. Mental model

```
manifest (pack.json) ──activate──▶ persona prepended to chat
                                   compliance mode set
                                   risk policy applied to broker
                                   tool allowlist enforced
                                   knowledge ingested → pack:<name> RAG ns
                                   skills installed → shared skill library
                                   model pinned (config default still wins)
                                   theme exposed on /status
```

Exactly **one** pack is active at a time. Activation is a pointer in config plus
side effects; deactivation clears the pointer (knowledge/skills already ingested
stay until overwritten). Everything degrades safely — a pack with only a persona is
valid.

| Source | Location | Wins |
|---|---|---|
| User packs | `~/.praxis/packs/<name>/pack.json` | override bundled |
| Vertical packages | `pip install praxis-<vertical>` (private, commercial) | registers via plugin registry |
| Bundled packs | `hybridagent/packs/<name>/pack.json` (ship in wheel) | fallback |

Today bundled in the open-core base: **general** (safe default).

Vertical packs (law_firm, medical_office, school_system, homeschool,
forensic_engineering) have been extracted into private commercial packages:
`praxis-legal`, `praxis-medical`, `praxis-education`, `praxis-homeschool`,
`praxis-forensic`. Install the package to activate that vertical — it
auto-registers its `VerticalSpec`, eval cases, and dashboard routes with
the base's plugin registry (`hybridagent.verticals.registry`). See the
private repos (`smfworks/smf-praxis-<vertical>`) for details.

---

## 2. The `pack.json` manifest — every field

```json
{
  "name": "homeschool",
  "version": "0.1.0",
  "vertical": "Homeschool",
  "description": "Parent-educator aide: lesson planning, multi-grade tutoring.",
  "systemPrompt": "You are Praxis configured for the Homeschool vertical: ...",
  "complianceMode": "autonomous",
  "tools": ["read_email", "draft_email", "rag_search"],
  "riskPolicy": {
    "dualApprovalRisks": ["send", "destructive"],
    "autonomousRisks": ["read", "draft"],
    "egressCheck": true,
    "injectionCheck": true,
    "approvalTtlSeconds": 1800
  },
  "knowledge": ["knowledge.md"],
  "skills": [{ "name": "lesson-plan", "trigger": "planning a lesson", "body": "1. ..." }],
  "model": "openai/gpt-4o",
  "theme": { "accent": "#0a7" }
}
```

| Field | Type | Effect |
|---|---|---|
| `name` | string | Required. `[a-z0-9][a-z0-9_-]{0,40}`. Directory + identifier. |
| `version` | string | Free-form, default `0.1.0`. |
| `vertical` | string | Human label (e.g. `Legal`). |
| `description` | string | One line shown in `pack list`. |
| `systemPrompt` | string | Persona; prepended to chat system prompt. |
| `complianceMode` | enum | `enforced` \| `autonomous` \| `permissive`. Applied on activate. |
| `tools` | string[] | Allowlist subset; absent ⇒ all. Survives runtime registration. |
| `riskPolicy` | object | Broker overrides (below). Absent keys keep defaults. |
| `knowledge` | string[] | `.md`/`.txt`/doc paths ingested to `pack:<name>` (p10). |
| `skills` | object[]/string[] | Inline `{name,trigger,body}` or SKILL.md refs (p11). |
| `model` | string | `provider/model` fallback; explicit config default wins (p12). |
| `theme` | object | Token map surfaced on dashboard `/status` (p12). |

**Risk classes:** `read`, `draft` (autonomous by default) · `send`, `destructive`
(consequential). `riskPolicy` keys: `dualApprovalRisks` (need 2 approvers),
`autonomousRisks` (run without approval), `egressCheck`/`injectionCheck` (bool),
`approvalTtlSeconds` (number, null = never expires).

---

## 3. CLI — daily use

```bash
praxis pack list                       # installed packs (* = active)
praxis pack templates                  # built-in domain templates
praxis pack create mine --vertical legal   # scaffold from a template
praxis pack install ./mine             # copy + activate an external pack dir
praxis pack activate homeschool        # apply persona + policy + knowledge + skills
praxis pack show                       # print the active manifest
praxis pack deactivate                 # back to defaults
```

`activate` prints what it wired, e.g.
`activated 'homeschool' (compliance: autonomous); ingested 1 knowledge source(s); installed 1 skill(s)`.

---

## 4. Built-in templates

`general, legal, medical, forensic, education, homeschool, business, developer`.
Regulated verticals → **enforced** + dual-approval send/destructive + egress/injection
guards. Productivity verticals → **autonomous** read+draft. Alias-aware & case-
insensitive: `lawyer`→legal, `dental`→medical, `coding`→developer,
`homeschooling`/`k12`→homeschool. Explicit `--vertical`, `system_prompt`, or manifest
edits always override the template.

---

## 5. AI quick reference (source of truth)

| Concern | File | Symbol |
|---|---|---|
| Manifest schema, lifecycle, apply | `hybridagent/pack.py` | `VerticalPack`, `activate`, `apply_to_policy` |
| Domain templates + aliases | `hybridagent/vertical_templates.py` | `VERTICAL_TEMPLATES`, `_ALIASES` |
| Per-vertical eval packs | `hybridagent/vertical_evals.py` | `VERTICAL_SPECS`, `vertical_eval_cases` |
| Knowledge ingest/retrieve | `hybridagent/pack.py` | `ingest_knowledge`, `knowledge_chunks`, `pack_ns` |
| Skills install | `hybridagent/pack.py` | `install_skills` |
| Model fallback | `hybridagent/config.py` | `get_default_model` |
| Bundled packs ship | `pyproject.toml` | `packs/*/pack.json`, `packs/*/*.md` |
| Tests | `tests/test_pack.py`, `tests/test_vertical_evals.py` | — |

Gates after any change: `pytest tests/test_pack.py tests/test_vertical_evals.py`,
`praxis eval --category vertical`, `ruff check`, `mypy hybridagent/pack.py`.

---

## 6. Extending to a new domain (5 steps)

1. **Template** — add to `VERTICAL_TEMPLATES` in `vertical_templates.py`:
   `vertical`, `description`, `systemPrompt`, `complianceMode`, `riskPolicy`
   (reuse `_REGULATED_RISK` or `_PRODUCTIVITY_RISK`). Add aliases to `_ALIASES`.
2. **Bundled pack** (optional) — `hybridagent/packs/<name>/pack.json` mirroring the
   template; add `knowledge.md` / inline `skills` to ship grounding + procedures.
3. **Tests** — extend `tests/test_pack.py` (templates list, alias, posture).
4. **Eval pack** — add one `VerticalSpec` row to `vertical_evals.py` (persona keyword,
   autonomous + held risk classes, mode); persona+posture cases generate automatically.
5. **Verify & document** — run the gates above; add the vertical to README's list.

No-code path: `praxis pack create mine --vertical <t>` then edit `pack.json`.

---

## 7. Worked example — homeschool

`hybridagent/packs/homeschool/` ships: `pack.json` (persona, autonomous, knowledge +
skill refs), `knowledge.md` (state recordkeeping, multi-grade, privacy). Template alias
`homeschooling`/`k12`. Eval pack asserts autonomous read+draft, held send/destructive.
Activate → persona prepended, attendance facts grounded, `lesson-plan` skill retrievable.

---

## 8. Troubleshooting

- **Not listed:** invalid `name`, missing `pack.json`, or bad JSON (silently skipped).
- **No knowledge hits:** needs a store + embeddings; activate ingests into `pack:<name>`.
- **Model ignored:** explicit `agents.defaults.model` beats the pack pin (by design).
- **Destructive auto-runs in tests:** isolate state — use `Store.open(tmp_path/'db')`.
