# Praxis Phase 5 Artifact Studio Implementation Plan

> **For Hermes:** Execute task-by-task with test-first slices, local commits, full independent review, and no remote push until the complete Phase 5 gate passes.

**Goal:** Add a governed Artifact Studio that turns validated Praxis evidence and workflow state into versioned professional documents and self-verifying release bundles.

**Architecture:** A compact stdlib-only dataclass IR is the canonical source. Markdown/JSON render in core; DOCX/PDF/PPTX/XLSX are lazy optional backends. A tenant-scoped append-only service persists canonical versions, computes semantic comparisons, gates release on claims/reviews/signatures, and assembles deterministic content-addressed ZIP bundles.

**Tech stack:** Python 3.10 stdlib (`dataclasses`, `json`, `hashlib`, `zipfile`, `sqlite3`); optional `python-docx`, `reportlab`, `python-pptx`, `openpyxl`; pytest; existing Praxis Store, evidence, claims, reviews, checkpoints, organization/workspace controls.

---

## Task 0 — Phase bootstrap and decisions

**Files:**
- Create: `docs/artifacts/phase-5-architecture.md`
- Create: `.hermes/plans/2026-07-13_212538-phase-5-artifact-studio.md`
- Modify: `feature_list.json`
- Modify: `PROGRESS.md`
- Modify: `hybridagent/__init__.py`

**Steps:**
1. Add PP50 as the sole `in_progress` feature with the complete verification command.
2. Record the research decision, baseline, branch, scope, and no-push-until-complete policy.
3. Bump the implementation line to `0.27.0`.
4. Run JSON, architecture, version, and clean-diff checks.
5. Commit locally.

## Task 1 — Validated document IR

**Files:**
- Create: `hybridagent/artifacts/__init__.py`
- Create: `hybridagent/artifacts/models.py`
- Create: `hybridagent/artifacts/validation.py`
- Create: `tests/test_artifact_models.py`
- Modify: `hybridagent/__init__.py`

**TDD slices:**
1. Define frozen models for metadata, confidentiality, citations, source entries, revisions, reviews, signatures, text/list/table/figure/page-break blocks, sections, and documents.
2. Implement exact-type constructors/decoders; reject subclasses and implicit coercions.
3. Implement strict canonical dictionaries/JSON/bytes and content hashes; normalize strings to NFC and forbid floats.
4. Validate required fields, limits, unique IDs, heading levels, table geometry, alt text, citation/source integrity, revision monotonicity, distinct signatures, and release-facing semantics.
5. Return deterministic `ValidationReport`/`ValidationIssue` values with stable paths and codes.
6. Round-trip all valid models and reject unknown schema versions, unknown fields, malformed unions, dangling references, and duplicate IDs.
7. Run focused tests, Ruff, mypy, architecture; bump to `0.27.1`; commit locally.

## Task 2 — Deterministic renderers

**Files:**
- Create: `hybridagent/artifacts/render_common.py`
- Create: `hybridagent/artifacts/render_json.py`
- Create: `hybridagent/artifacts/render_markdown.py`
- Create: `hybridagent/artifacts/render_docx.py`
- Create: `hybridagent/artifacts/render_pdf.py`
- Create: `hybridagent/artifacts/render_pptx.py`
- Create: `hybridagent/artifacts/render_xlsx.py`
- Create: `tests/test_artifact_renderers.py`
- Modify: `pyproject.toml`
- Modify: `scripts/check_architecture.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `hybridagent/__init__.py`

**TDD slices:**
1. JSON renderer emits canonical IR bytes exactly.
2. Markdown renderer emits stable front matter, headings, blocks, citations, source manifest, revision/review/signature appendices, and confidentiality markings.
3. Add one actionable optional-backend error and prove core imports without extras.
4. DOCX renderer maps headings, tables, figures/alt text, headers/footers, language, metadata, citations, and appendices; normalize volatile ZIP metadata.
5. PDF renderer uses ReportLab invariant mode, fixed metadata, page templates, headings/tables/figures/citations, and no external resource fetches.
6. PPTX renderer creates a coherent title/section/content deck, speaker/source notes where supported, stable layout, alt text, and normalized package metadata.
7. XLSX renderer creates overview, sections/tables, citations/sources, validation, revisions, and reviews sheets with stable ordering, table headers, freeze panes, print settings, and no formulas/macros.
8. Golden tests compare exact JSON/Markdown and normalized semantic OpenXML/PDF structure. Test missing and failing optional backends.
9. Add cross-platform optional-artifacts CI and clean-wheel tests.
10. Run focused tests, optional tests, build/install checks, Ruff, mypy, architecture; bump to `0.27.2`; commit locally.

## Task 3 — Versioning, compare, and governed release bundles

**Files:**
- Create: `hybridagent/artifacts/service.py`
- Create: `hybridagent/artifacts/bundle.py`
- Create: `tests/test_artifact_versions.py`
- Create: `tests/test_artifact_release.py`
- Modify: `hybridagent/persistence.py`
- Modify: `tests/test_release_packaging.py`
- Modify: `README.md`
- Modify: `CAPABILITIES.md`
- Modify: `docs/artifacts/phase-5-architecture.md`
- Modify: `hybridagent/__init__.py`

**TDD slices:**
1. Add organization/workspace-owned artifact, immutable version, and immutable release tables, indexes, foreign keys, triggers, and historical migration checks.
2. Create artifacts and append expected-head versions atomically; enforce sequential revision, parent hash, canonical content hash, active membership, and exact scope.
3. Implement tenant-scoped get/list/read APIs and semantic version comparison.
4. Bind source-manifest entries to exact stored evidence/source/version/span records; bind review/signature IDs to exact active workspace identities and immutable decisions.
5. Implement deterministic ZIP bundle assembly with canonical payload manifest, fixed timestamps, sorted safe paths, bounded counts/sizes, and post-build hash verification.
6. Implement release state machine and idempotency. Require zero validation errors, supported material claims, exact approved professional review, satisfied signatures, successful requested renderers, and current expected version.
7. Protect all immutable rows against update/delete/replace/upsert attacks. Probe cross-process stale-head and release races.
8. Add historical migration, restart, cross-tenant, tamper, archive traversal, renderer failure, and clean-wheel packaging tests.
9. Document public API, optional extra, output guarantees, accessibility boundary, and release-bundle contents.
10. Run focused/full/fuzz/static/eval/demo/build/install/Docker/secret gates; bump to `0.27.3` or later; commit locally.

## Task 4 — Independent review and remediation

1. Freeze one exact clean commit.
2. Dispatch three independent domains: IR/renderer correctness; persistence/security/concurrency; distribution/documentation/release bundles.
3. Reproduce every blocker. Add exact regression tests and remediate with a new version bump/commit.
4. Any code change invalidates prior review; repeat all gates and exact-head review.
5. Require three PASS verdicts and zero blockers.

## Task 5 — Completion and push

1. Mark PP50 `passing` with exact test counts, review SHA, artifact evidence, and known non-blocking limitations.
2. Update `PROGRESS.md`; leave zero active WIP.
3. Run the full clean-state checklist and complete release-quality matrix.
4. Verify the wheel from a neutral directory imports every `hybridagent.artifacts` module and executes core renderers.
5. Commit the final checkpoint locally.
6. Push `feat/professional-platform-phase-5` exactly once to GitHub.
7. Verify remote branch SHA equals local SHA and report the branch URL. Do not merge, tag, or publish a release unless separately requested.

## Required final gates

```bash
python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q
python3 -m pytest -o addopts='' -q tests/test_fuzz_parsers.py
python3 -m hybridagent.cli eval
python3 -m ruff check hybridagent/
python3 -m mypy hybridagent --ignore-missing-imports
python3 scripts/check_architecture.py
python3 -m hybridagent.cli demo
python3 -m build
python3 -m twine check dist/*
```

Also require optional-artifacts renderer tests, historical migrations, concurrent expected-head/release stress, deterministic bundle reproduction, clean wheel install from a neutral directory, standard installer, rebuilt Docker smoke, added-line secret scan, and three exact-head independent PASS verdicts.
