# PROGRESS.md

> Single source of truth for Praxis project state. Every new session reads this first.
> Updated at the end of each session. Format follows the Learn Harness Engineering course.

## Current Verified State

- **Repository root:** `/home/mikesai1/smf-praxis` (GitHub: `smfworks/smf-praxis`)
- **Version:** `0.21.2` (`hybridagent/__init__.py` + `pyproject.toml`)
- **Standard startup path:** `./install.sh` → `source .venv/bin/activate` → `praxis demo`
- **Standard verification path:** see `AGENTS.md` → "Verification commands (Definition of Done)"
- **Highest priority unfinished feature:** H05 — Maker-checker separation documented for the Praxis dev review loop (`feature_list.json`)
- **Current blocker:** none

## Baseline verification (captured 2026-07-11)

| Check | Command | Result |
|---|---|---|
| Test suite | `python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q` | **926 passed, 16 skipped** in 35s |
| Capability evals | `python3 -m hybridagent.cli eval` | **40/40 passed** (a2a, approval, browser, context, debate, mcp, orchestration, planning, reasoning, reflexion, retrieval, routing, safety×7, schema, skills, tool_use, verification, vertical×10, voice) |
| Coverage | `pytest --cov=hybridagent` | **80%** (12403 stmts, 2463 missing) — CI gate met |
| Lint | `python3 -m ruff check hybridagent/` | **All checks passed** |
| Types | `python3 -m mypy hybridagent --ignore-missing-imports` | **Success: no issues in 86 source files** |
| Build | `python3 -m build` (wheel exists) | `dist/praxis_agent-0.21.2-py3-none-any.whl` |
| CI | `.github/workflows/ci.yml` | Linux 3.10/3.11/3.12 + macOS + Windows, 80% coverage gate |

Baseline is **green**. New work must not regress any of these.

## Completed

- [x] Phase 1–16: foundations, RAG, model router, multimodal, grounding, skills, memory, persistent tasks, LLM wiki, subagents, compliance spine, security hardening, regulated controls, quality gates, RAG performance, test hardening (see FRAMEWORK.md build phases)
- [x] 0.21.x sprint — preeminence durable threads, Telegram settings, governance fixes; Command Deck viewport with tabbed right rail; friendliness sprint; release dry-run + dogfood harness
- [x] **H01** — Minimal harness pack: `AGENTS.md` (router), `feature_list.json` (scope surface), `PROGRESS.md` (this file), `docs/harness/` (templates) — 2026-07-11
- [x] **H02** — Evaluator rubric template (`docs/harness/evaluator-rubric.md`)
- [x] **H03** — Clean-state checklist + session handoff templates
- [x] **H04** — Quality document with initial module snapshot

## In Progress

(none — WIP=1 enforced; next pick is H05)

## Known Issues / Risks

- **Evaluator rubric is un-tuned.** Out of the box, agents identify issues then talk themselves into approving. Needs 3–5 tuning rounds against real agent-contributed PRs before it's reliable (course L11). Do not gate merges on it yet.
- **Harness simplification is documented but unpracticed.** No monthly component-removal benchmark has been run. H09 tracks this.
- **Maker-checker at the dev-review layer is aspirational.** The runtime broker enforces separation at execution; the *code-review* layer still allows an agent to grade its own PR. H05.
- **Model-specific compaction (H08) is open.** `context.py` compacts; it is not yet model-aware (course L5: Sonnet needs resets, Opus tolerates compaction).
- **Architectural invariants may lack dedicated executable checks.** The dependency-free-core and injection-boundary rules are enforced by tests in aggregate, but H06 asks whether there are *targeted* checks (course L10: "architectural rules must be executable, not paper docs").

## Next Steps (priority order)

1. **H05** — Maker-checker separation for the dev-review loop. Encode in `AGENTS.md` and `evaluator-rubric.md` that the verifier must be an independent session (ideally a different model). Behavior change, not just docs.
2. **H06** — Audit whether dependency-free-core and injection-boundary invariants have dedicated executable checks (grep/lint/custom test), not just aggregate test coverage. Add if missing.
3. **H07** — Improve agent-oriented error messages in `broker.py`/`validation.py` denials: what + why + how-to-fix. Enables self-correction loops.
4. **H08** — Make `context.py` compaction model-aware (per-model harness profiles).
5. **H10** — Document Praxis's loop capabilities as the six loop primitives + maturity ladder.
6. **H09** — First harness simplification pass: disable one component, benchmark with `praxis eval --set-baseline`, decide keep/remove.

## Session Record

### 2026-07-11 — Harness engineering implementation
- **Goal:** Implement the 10 key takeaways from the Learn Harness Engineering course as Praxis harness artifacts.
- **Completed:** Audited Praxis against the five-subsystem model and 10 takeaways. Found a mature governed agent (v0.21.2, 926 tests, 40/40 evals, 80% cov, full CI) but missing the *agent-facing* harness pack the course prescribes. Shipped: `AGENTS.md` (≤100-line router), `feature_list.json` (10 features, H01–H04 passing, H05–H10 not_started), `PROGRESS.md` (this file), `docs/harness/` (evaluator-rubric, clean-state-checklist, session-handoff, quality-document).
- **Verification run:** pytest 926 pass / 16 skip; `praxis eval` 40/40; ruff clean; mypy clean; `praxis demo` runs.
- **Evidence recorded:** this file + `feature_list.json` evidence fields + `docs/harness/`.
- **Commits:** (this session) `harness: implement Learn Harness Engineering course artifacts`
- **Known risks:** evaluator rubric un-tuned; maker-checker at review layer is docs-only; model-specific compaction not implemented.
- **Next best action:** H05 (maker-checker separation behavior change in the review loop).