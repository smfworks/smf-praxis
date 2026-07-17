# PROGRESS.md

> Single source of truth for Praxis project state. Every new session reads this first.
> Updated at the end of each session. Format follows the Learn Harness Engineering course.

## Current Verified State

- **Repository root:** `/home/mikesai1/smf-praxis` (GitHub: `smfworks/smf-praxis`)
- **Version:** `0.28.29` (`hybridagent/__init__.py`; `pyproject.toml` reads it dynamically)
- **Active branch:** `main` (consolidation + vertical build-out + law-firm pack + medical-office pack committed locally; pending push)
- **Standard startup path:** `./install.sh` → `source .venv/bin/activate` → `praxis demo`
- **Standard verification path:** see `AGENTS.md` → "Verification commands (Definition of Done)"
- **Current WIP:** None — Active Memory Consolidation (Phase 6) + Forensic/Legal vertical build-out (Phase 7) + Law Firm pack (Phase 8) + Medical Office pack (Phase 9) all `passing` locally.
- **Phase 6 status:** `0.28.7` — Active Memory Consolidation complete. 6 slices. Released as `v0.28.7`.
- **Phase 7 status:** `0.28.14` — Forensic Engineering / Law Firm vertical build-out complete. Released as `v0.28.14`.
- **Phase 8 status:** `0.28.19` — Law Firm pack complete. Released as `v0.28.19`.
- **Phase 9 status:** `0.28.29` — Medical Office pack complete. Session 1 (Slices 1–5: MEDICAL registry, clinical attestation, HIPAA governance, CME topics, controlled-substance guardrails) + Session 2 (Slices 6–10: telemedicine gate, minor-consent gate, records retention + patient access, portal triage, ambient documentation + pack assembly + 13-state integration test). Modules: `telemedicine_gate`, `minor_consent`, `records_retention`, `portal_triage`, `ambient_documentation` (+ Session 1 modules). Pack: `hybridagent/packs/medical_office/` (manifest, persona, knowledge, 6 skills). Vertical evals: 24/24. Full suite green, ruff/mypy clean (164 source files). Pending: tag + push to origin.

- **Phase 5 research:** Praxis will use a compact stdlib-native canonical document IR; strict canonical JSON is the identity surface; Markdown/JSON remain core; DOCX/PDF/PPTX/XLSX are lazy optional backends; versions and releases are append-only and tenant-scoped; release bundles use fixed-path content-addressed manifests and fail closed on validation, claims, review, signature, renderer, or integrity failures. See `docs/artifacts/phase-5-architecture.md` and `.hermes/plans/2026-07-13_212538-phase-5-artifact-studio.md`.
- **Phase 5 implementation:** Artifact Studio now provides strict canonical professional-document models with unique JSON object members, NFC normalization, lone-surrogate rejection, and exact-class identity; exact evidence/source/span/claim references; deterministic JSON/Markdown and optional DOCX/PDF/PPTX/XLSX renderers with bounded dependency-free PNG/JPEG/SVG validation before persistence; append-only organization/workspace-keyed versions and assets guarded on both primary and alternate unique keys against `INSERT OR REPLACE` deletion; immutable revision prefixes with forward-only head advancement; occurrence-preserving semantic comparison; exact professional-release review/signature binding; active signer/role revalidation; durable scoped idempotency; atomic migration from early global artifact keys; and deterministic bundle-schema-v2 release packages containing selected renders, assets, claims/evidence, exact review/signature ID sets, validation, and run/checkpoint provenance. SQLite blocks update, delete, conflict-upsert, replacement (including alternate-key), history-rewrite, and head-rewind paths. Standalone verification recomputes JSON, Markdown, validation, declared-media structure, and governance linkage and rejects malformed or noncanonical manifests/payloads and ZIP metadata/order, traversal, drive-qualified or Windows-reserved paths, duplicate/case-colliding names, symlinks, size violations, unexpected members, unreadable payloads, and release/document/governance identity tampering.
- **Phase 5 local verification:** The `0.27.8` release candidate passed the expanded PP50 focused gate (76/76); Python 3.12 (1,306 passed, 13 skipped, 82.77%), 3.11 (1,298, 21 skipped, 80.75%), and 3.10 (1,298, 21 skipped, 80.74%) complete suites; 11 parser fuzz tests; 40/40 capability evaluations; governed semantic demo; 25/25 repeated cross-process version/release races; whole-repository Ruff; mypy across 135 modules; architecture checks; compilation; added-line secret scanning; diff hygiene; immutable-archive package verification; neutral wheel installation; standard installer/CLI; and rebuilt non-root Docker smoke. Exact regressions reject duplicate JSON members, lone Unicode surrogates, model subclasses at canonical identity, truncated/signature-only/empty-scan/scan-before-frame media, malformed/prefixed SVG, alternate-key append-only version/release replacement via `INSERT OR REPLACE`, and malformed assets before any document, version, or asset row is persisted. Three independent reviewers returned PASS on exact SHA `2eaec9703361605feffdba3103373df236fe39a7` (batch `deleg_49650214`). Candidates `8cb9a360…`, `7113c4cd…`, `9d6255ac…`, and `6381f06…` and all evidence bound to them are superseded by their review rejections.
- **Phase 4 implementation:** `0.26.16` expands the checkpoint substrate from GoalRunner into planner-driven professional workflows — durable `PlanExecutor` state mapping; expected-head checkpoint CAS; scoped checkpoints on step transitions; terminal-run and pre-step cancellation enforcement before approval restoration; typed approval interrupts; exact durable approval reconstruction; resumable held, running, and skipped-dependent `PlanStep` state; durable replan generation/budget state; durable consequential outbox intents; exact action/receipt fingerprint binding; immutable effect receipts; explicit provider-idempotent at-least-once crash-gap semantics; multi-action completion and rejection reconciliation; just-in-time exact restored grants with denial/error revocation; independent run/step approval provenance; typed workspace-scoped professional reviews; and structured research supervision. Daemon task approvals use task-bound provenance plus an append-only SQLite action outbox. Distinct signatures, persisted threshold evaluation, final approval resolution, and the `pending_execution` claim share one cross-process transaction. Task and execution-scoped plan approvals never deduplicate across independent actions, while conversational re-proposals retain compatibility deduplication; CLI task approvals use the daemon receipt path; and legacy `0.26.6` waiting approvals are either backfilled exactly or failed for manual reconciliation. SQLite enforces exact signer-object JSON, positive thresholds, non-empty distinct dual signers, finite timestamps, threshold satisfaction, resolved-decision immutability, action identity including the held consequential risk contract, and immutable receipts. Chat one-shot approval grants bind to exact canonical arguments, and store-backed kill checks refresh durable state across workers. PP40 is released and marked `passing`; Phase 5 may now begin as the next WIP.
- **Phase 4 local verification:** `0.26.16` passes 1,230 non-fuzz tests with 17 expected skips; 11 parser fuzz tests; 141 PP40 contracts; 1,241 total tests with 17 expected skips at 81.93% coverage; 40/40 capability evals; Ruff; mypy across 121 modules; architecture; compilation; semantic demo; six historical migration/recovery classes through rejected `v0.26.13`; 100 repeated concurrent schema/recovery initializations; and 25 repeated eleven-outcome lifecycle/recovery stress runs. Wheel/sdist, `twine check`, recursive manifest inspection, external clean-wheel authority imports, standard installer, rebuilt Docker status/dashboard/assets smoke, and added-line secret scanning pass. The GitHub tag workflow attaches both artifacts; operator docs contain no unpublished PyPI requirements; and `praxis update` validates the latest stable GitHub Release, installs its exact wheel, and fails closed without invoking pip when discovery fails. Three independent reviewers returned PASS on exact SHA `78541f5e09fffbc3a8928207780a511cb5c90019`; PR #5 passed the Linux/macOS/Windows matrix and merged as `a56c615bfd2fa11d20b7c3d16676c885a4ed2595`; GitHub Release `v0.26.16` published and clean-install verification passed. Published SHA-256: wheel `66a584420b9ba3ed5b110d208bc84ffc238d2f4fb9d2b6761ad25f9675cff2d0`; sdist `b766d131087ac62a4e0679dc05d21e66e271cd42be8b981a7e68ec7966842083`.

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
- [x] **PP30** — Professional platform Phase 3: immutable evidence, extraction lineage, custody, claims, and vertical authority policy — released as `v0.25.20` on 2026-07-12
- [x] **PP40** — Professional platform Phase 4: durable workflows, checkpoints, professional reviews, research supervision, and effect receipts — independently reviewed and released as `v0.26.16` on 2026-07-13
- [x] **PP50** — Artifact Studio: canonical professional-document models, append-only versions, optional renderers, release bundles — released as `v0.27.8`
- [x] **Phase 6 / Active Memory Consolidation** — 6-slice phase extending Praxis with the GCP "Always-On Memory Agent" pattern: periodic consolidation reads episodic + durable memory, extracts structured metadata, finds cross-corpus connections, synthesizes insights, and re-rates salience. Reuses the existing Store + RAG + daemon (no standalone microservice). Schema (memory_connections table, entities/topics/last_consolidated_at columns) → MemoryConsolidator core → daemon wiring (gated) → metadata extraction on ingest → CLI + dashboard visibility → bug-hunt (22 hostile probes, 1 real bug fixed) + bounded dogfood against Spark Qwen3.6-35B-NVFP4 (insight-quality issue found + fixed: reasoning-model CoT leaking into stored insights) → flipped on-by-default. 59 consolidation tests, reasoning-model provider support (`content`-null → `reasoning` fallback). Released as `v0.28.7` on 2026-07-16.
- [x] **Phase 7 / Forensic Engineering + Law Firm Vertical Build-out** — 7 gaps from the 13-state regulatory gap analysis, built per the gap analysis Part 4 order: Gap 1 (per-jurisdiction registry — 13 states, forensic + legal profiles with confidence tracking), Gap 3 (MA 201 CMR 17.00 WISP + encryption attestation surface, 3 tiers), Gap 2 (NY 22 NYCRR 1200 + FL attorney-advertising filing workflow), Gap 6 (matter-wide legal hold + custodian acknowledgment), Gap 4 (per-state CE/PDH credential tracking), Gap 5 (conflict-of-interest checking across matters — no-leak, break-glass audit), Gaps 7-8 (privilege log + expert-witness disclosure templates). 7 new modules (~1,800 lines), 7 test files (224 tests). Every module sources per-state rules from the Gap 1 registry; all standalone (no canonical-IR touch, no governance-spine changes). 2 bugs caught by tests during development (MD CLE flag, credentials session-history loss) — both fixed. Released as `v0.28.14` on 2026-07-16.
- [x] **Phase 8 / Law Firm Pack** — the bundled, public, MIT-licensed `law_firm` pack assembling the Phase 7 compliance modules into a single one-activation experience covering the 13 states. 5 slices: manifest+persona+knowledge (pack.json with UPL+IOLTA+per-jurisdiction guardrails, regulated risk policy, 13-tool allowlist, 4 skills, knowledge base, navy theme), skills (conflict-check, ad-filing-gate, matter-hold, ce-status — retrieval + body verification), vertical eval suite (VerticalSpec + 5 manual cases: UPL guardrail, NY ad-filing gate, MA WISP attestation, conflict check, CLE status; 17/17 vertical evals), dashboard surfaces (matter-hold badge, credential card, ad-filing tracker, attestation panel via /api/law_firm), 13-state integration test (83 cases proving per-jurisdiction behavior for all 13 states). Bug caught by the integration test: MD cle_required=True but cle_hours=0 — fixed with a placeholder pending verification. Released as `v0.28.19` on 2026-07-16.
- [x] **Phase 9 / Medical Office Pack** — public MIT-licensed `medical_office` pack covering 13 states. Session 1 (v0.28.20–0.28.24): M1 MEDICAL profile registry, M3 clinical attestation (never-write-to-chart), M2 HIPAA governance, M7 CME mandatory topics, M5 controlled-substance guardrails. Session 2 (v0.28.25–0.28.29): M6 telemedicine cross-state gate (IMLC + FL §456.47 + PA/MA non-Compact), M4 minor-consent record gate, M9 records retention + patient-access workflow, M10 portal triage (clinical hold + admin allowlist), M8 ambient documentation + pack assembly (manifest/persona/knowledge/6 skills) + 13-state integration test + VerticalSpec/5 manual evals. 54/54 capability evals (24/24 vertical). Local HEAD `0d221ec` at v0.28.29. Pending: tag + push to origin.

## Historical Phase 3 Release Notes

- [x] **PP30** — Professional platform Phase 3: canonical sources and versions,
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

1. Freeze the Phase 5 implementation candidate and obtain three independent PASS reviews bound to its exact SHA.
2. Promote PP50 to `passing` only after all reviewers attest the same candidate and final status checks pass.
3. Push `feat/professional-platform-phase-5` once and verify the remote SHA and GitHub-visible documentation.

## Session Record

### 2026-07-11 — Harness engineering implementation
- **Goal:** Implement the 10 key takeaways from the Learn Harness Engineering course as Praxis harness artifacts.
- **Completed:** Audited Praxis against the five-subsystem model and 10 takeaways. Found a mature governed agent (v0.21.2, 926 tests, 40/40 evals, 80% cov, full CI) but missing the *agent-facing* harness pack the course prescribes. Shipped: `AGENTS.md` (≤100-line router), `feature_list.json` (10 features, H01–H04 passing, H05–H10 not_started), `PROGRESS.md` (this file), `docs/harness/` (evaluator-rubric, clean-state-checklist, session-handoff, quality-document).
- **Verification run:** pytest 926 pass / 16 skip; `praxis eval` 40/40; ruff clean; mypy clean; `praxis demo` runs.
- **Evidence recorded:** this file + `feature_list.json` evidence fields + `docs/harness/`.
- **Commits:** (this session) `harness: implement Learn Harness Engineering course artifacts`
- **Known risks:** evaluator rubric un-tuned; maker-checker at review layer is docs-only; model-specific compaction not implemented.
- **Next best action:** H05 (maker-checker separation behavior change in the review loop).
