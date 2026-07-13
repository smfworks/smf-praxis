# PROGRESS.md

> Single source of truth for Praxis project state. Every new session reads this first.
> Updated at the end of each session. Format follows the Learn Harness Engineering course.

## Current Verified State

- **Repository root:** `/home/mikesai1/smf-praxis` (GitHub: `smfworks/smf-praxis`)
- **Version:** `0.26.6` (`hybridagent/__init__.py`; `pyproject.toml` reads it dynamically)
- **Active branch:** `feat/professional-platform-phase-4`
- **Standard startup path:** `./install.sh` → `source .venv/bin/activate` → `praxis demo`
- **Standard verification path:** see `AGENTS.md` → "Verification commands (Definition of Done)"
- **Current WIP:** PP40 — durable professional workflows
- **Current blocker:** independent exact-head maker-checker review; all `0.26.6` local Definition of Done gates pass, but PP40 remains `in_progress` until exact-commit review passes.
- **Phase 4 implementation:** `0.26.6` expands the checkpoint substrate from GoalRunner into planner-driven professional workflows — durable `PlanExecutor` state mapping; expected-head checkpoint CAS; scoped checkpoints on step transitions; terminal-run and pre-step cancellation enforcement; typed approval interrupts; exact durable approval reconstruction; resumable held, running, and skipped-dependent `PlanStep` state; durable replan generation/budget state; durable consequential outbox intents; exact action/receipt fingerprint binding; immutable effect receipts; explicit provider-idempotent at-least-once crash-gap semantics; truthful daemon task failure after approved tool errors; typed workspace-scoped professional reviews with maker-checker, role, active-user, database immutability, and concurrent-decision enforcement; and a structured research supervisor with active-actor reads/writes, bound compatible review snapshots, atomic review interruption/application, and decision-specific outcomes.
- **Phase 4 local verification:** all prior exact-head regressions, 90 PP40 contracts, 1,188 non-fuzz tests, 11 parser fuzz tests, 40/40 capability evals, deterministic hold/approval/injection/denial demo assertions, Ruff, mypy across 121 modules, architecture/WIP/version checks, wheel/sdist build, `twine check`, clean-wheel install/import, 33 dashboard assets, package data, jobs catalog, and additive populated-database migration pass at `0.26.6`.

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
- [x] **PP10** — Professional platform Phase 1: tenant isolation, revocable sessions, RBAC/ABAC/purpose controls, authenticated approvals, and classified-data lifecycle/egress — independently verified 2026-07-12
- [x] **PP20** — Professional platform Phase 2: tenant-owned workspaces, immutable timelines, workspace-isolated context and board/runtime records, and controlled external collaboration rooms — independently verified 2026-07-12

## In Progress

- [ ] **PP30** — Professional platform Phase 3: canonical sources and versions,
  extraction lineage, append-only custody, claim support states, and authority policy.
  - Canonical evidence, extraction lineage, custody, claims, authority, and runtime
    wiring are implemented. Tenant/workspace-owned source versions and exact spans
    are append-only; custody is transactionally sequenced and hash-chained; material
    claims fail closed at professional release; authority applicability filters run
    before similarity; ingestion/media/verifier paths use the substrate.
  - Verified: 66/66 focused Phase 3/professional/API contracts, complete non-fuzz
    suite, evals 40/40, governed demo, whole-repository Ruff, mypy across 117
    modules, architecture checks, package build, clean-wheel `0.25.6` import, and
    clean diff validation. First independent review failed on runtime claim-scope
    propagation, inactive-organization release readiness, and weak document locator
    values. Subsequent reviews exposed boolean/range, textual-field, and nested
    non-finite JSON gaps across locators; `0.25.11` now enforces strict scalar types,
    valid ranges, finite numbers, nonempty identifiers, and RFC-compatible JSON for
    locator, parser, derived-artifact, and custody metadata. A later critical review
    proved rejected material-claim text was still emitted after its verification
    event; `0.25.12` suppressed rejected final payloads, and a subsequent critical
    review found intermediate critique/error channels could run before verification.
    `0.25.13` added preflight enforcement; a readiness-flip review then proved
    already-streamed intermediate events could not be recalled. `0.25.14` buffers
    every scoped inner event until terminal verification and a final readiness check.
    `0.25.15` extends that atomic boundary across all revision attempts and converts
    scoped generator failures to a generic release block, so critic details and
    exception text cannot escape before readiness succeeds. `0.25.16` closes the
    remaining terminal boundaries: scoped custom-verifier exceptions and error/no-
    terminal outcomes discard all buffered content and emit generic verification
    failures. `0.25.17` also makes built-in critic failures and malformed verifier
    returns fail closed, and treats every non-approved scoped verdict as a hard block;
    advisory rejection-plus-final behavior is legacy-only. `0.25.18` closes runtime
    type confusion: verdict fields have a strict schema and scoped critics must return
    exactly `APPROVE` or `REVISE: <reason>`; malformed values block release.
    `0.25.19` requires exact built-in types at this boundary and full-matches the
    critic grammar, preventing string-subclass method dispatch and malformed REVISE
    prefixes. `0.25.20` rejects verdict subclasses and snapshots each exact built-in
    field once before any retry or release decision, closing stateful descriptor and
    repeated-read races. Independent maker-checker PASS on exact commit
    `ea090056c3515c7650024a0ce9e480dfef42801e`; Phase 3 release gate approved.

### Phase 2 release-candidate evidence

- 66 focused workspace/auth/API/security contracts pass.
- Complete non-fuzz repository suite, 40/40 capability evaluations, and governed
  offline demo pass.
- Ruff, mypy across 99 modules, architecture invariants, and diff checks pass.
- Authenticated two-workspace HTTP verification confirms isolated board/timeline
  data, workspace-scoped idempotency, missing-selector denial, cross-tenant
  workspace concealment, and closed external-room permissions.
- Independent maker-checker `deleg_8c417aea` returned PASS after 10 synchronized
  two-process ownership races, rollback checks, and same-workspace idempotence.
- `0.24.1` transport hotfix drains oversized request bodies with a strict bounded
  timeout so macOS clients reliably receive the structured `413` envelope rather
  than a TCP reset; the two affected HTTP scenarios passed five repeated runs.
- `0.24.2` adds explicit JSON `Content-Length` framing and deterministic
  creation-order workspace listing after remote CI exposed macOS EOF resets and
  Windows timestamp ties; both platform regressions passed five repeated runs.

## Known Issues / Risks

- **Evaluator rubric is un-tuned.** Out of the box, agents identify issues then talk themselves into approving. Needs 3–5 tuning rounds against real agent-contributed PRs before it's reliable (course L11). Do not gate merges on it yet.
- **Harness simplification is documented but unpracticed.** No monthly component-removal benchmark has been run. H09 tracks this.
- **Maker-checker at the dev-review layer is aspirational.** The runtime broker enforces separation at execution; the *code-review* layer still allows an agent to grade its own PR. H05.
- **Model-specific compaction (H08) is open.** `context.py` compacts; it is not yet model-aware (course L5: Sonnet needs resets, Opus tolerates compaction).
- **Architectural invariants may lack dedicated executable checks.** The dependency-free-core and injection-boundary rules are enforced by tests in aggregate, but H06 asks whether there are *targeted* checks (course L10: "architectural rules must be executable, not paper docs").

## Next Steps (priority order)

1. Implement canonical source and immutable source-version records test-first.
2. Preserve Hermes and repository WIP=1 throughout the professional-platform sequence.
3. Require independent maker-checker PASS before every phase promotion.

## Session Record

### 2026-07-11 — Harness engineering implementation
- **Goal:** Implement the 10 key takeaways from the Learn Harness Engineering course as Praxis harness artifacts.
- **Completed:** Audited Praxis against the five-subsystem model and 10 takeaways. Found a mature governed agent (v0.21.2, 926 tests, 40/40 evals, 80% cov, full CI) but missing the *agent-facing* harness pack the course prescribes. Shipped: `AGENTS.md` (≤100-line router), `feature_list.json` (10 features, H01–H04 passing, H05–H10 not_started), `PROGRESS.md` (this file), `docs/harness/` (evaluator-rubric, clean-state-checklist, session-handoff, quality-document).
- **Verification run:** pytest 926 pass / 16 skip; `praxis eval` 40/40; ruff clean; mypy clean; `praxis demo` runs.
- **Evidence recorded:** this file + `feature_list.json` evidence fields + `docs/harness/`.
- **Commits:** (this session) `harness: implement Learn Harness Engineering course artifacts`
- **Known risks:** evaluator rubric un-tuned; maker-checker at review layer is docs-only; model-specific compaction not implemented.
- **Next best action:** H05 (maker-checker separation behavior change in the review loop).
