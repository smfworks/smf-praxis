# Phase 5 Artifact Studio — Research and Architecture Decision

**Status:** implemented release candidate; final exact-head review and promotion pending

**Implementation:** Praxis `0.27.3` on `feat/professional-platform-phase-5`

**Baseline:** `v0.26.16`, checkpoint `b806fea57e6ec11d71786a74e5d0db29f82a2231`

**Phase:** PP50 — Artifact Studio and professional documents

## Problem

Praxis can ingest professional documents and preserve evidence, claims, reviews, approvals, custody, and durable workflow state, but it cannot yet produce a governed professional deliverable. Phase 5 adds a validated document intermediate representation (IR), deterministic renderers, immutable versioning, semantic comparison, and a release bundle that preserves the artifact together with the evidence needed to defend it.

## Implemented surface

- `hybridagent/artifacts/models.py` and `validation.py`: strict canonical IR and validation.
- `render_json.py`, `render_markdown.py`, `render_docx.py`, `render_pdf.py`, `render_pptx.py`, and `render_xlsx.py`: deterministic core and optional output formats.
- `versions.py` and `service.py`: append-only tenant-scoped versions, semantic comparison, exact review/signature binding, and atomic idempotent release.
- `bundles.py`: deterministic content-addressed package assembly and standalone verification.
- `hybridagent/persistence.py`: durable tables, ownership/parent/run foreign keys, idempotency index, and update/delete/replace immutability triggers.
- `docs/artifacts/README.md`: public API, lifecycle, bundle layout, guarantees, and interface boundary.

## Research sources

- RFC 8785, JSON Canonicalization Scheme: <https://www.rfc-editor.org/rfc/rfc8785>
- W3C PROV family overview: <https://www.w3.org/TR/prov-overview/>
- RO-Crate 1.1: <https://www.researchobject.org/ro-crate/1.1/>
- RFC 8493, BagIt 1.0: <https://www.rfc-editor.org/rfc/rfc8493>
- Pandoc AST/filter model: <https://pandoc.org/lua-filters.html>
- python-docx: <https://python-docx.readthedocs.io/>
- python-pptx: <https://python-pptx.readthedocs.io/>
- openpyxl: <https://openpyxl.readthedocs.io/>
- ReportLab: <https://docs.reportlab.com/>

## Decisions

### 1. Praxis owns a compact native IR

Pandoc, JATS, and DocBook are useful reference models, but none expresses Praxis-native controls such as workspace ownership, exact evidence spans, material claims, confidentiality, professional review, approval, run provenance, and release state. Praxis will use a versioned stdlib dataclass model and provide adapters later if needed.

The first schema supports:

- document metadata and confidentiality;
- ordered sections and appendices;
- paragraphs, lists, tables, figures, and explicit page breaks;
- citation references bound to exact Praxis evidence/source-version/span IDs;
- revision history;
- reviewer and signature records;
- a source manifest;
- stable IDs and complete referential-integrity validation.

### 2. Canonical JSON is the source of identity

The IR serializes to strict canonical JSON with sorted object keys, compact separators, UTF-8, finite/exact built-in JSON types, and no implicit coercion. To avoid Python-versus-ECMAScript number differences, the governed IR does not permit floating-point values; decimal quantities are strings with an explicit format. Strings are normalized to Unicode NFC before hashing.

`sha256(canonical_ir_bytes)` is the immutable version content hash. Rendered Office/PDF bytes are outputs, never the source of document identity.

### 3. Stable IDs and exact references

Every section, block, citation, source-manifest entry, revision, review, and signature has a stable non-empty identifier unique within the document. Citations refer to manifest entries, and manifest entries refer to immutable Praxis source/version/span identifiers and content hashes. Validation fails closed on dangling, duplicate, cross-scope, or malformed references.

Pure model validation checks structure and referential integrity. `ArtifactService` performs authoritative organization/workspace/store checks before persistence or release.

### 4. Determinism is format-specific

- Canonical JSON and Markdown: byte-for-byte deterministic.
- Release bundle ZIP: byte-for-byte deterministic through fixed timestamps, sorted names, fixed compression settings, normalized paths, and canonical manifests.
- DOCX/PPTX/XLSX: semantic determinism. Normalize ZIP member order/timestamps and volatile core properties where the library permits, then compare normalized OpenXML and extracted structure rather than raw producer bytes alone.
- PDF: deterministic ReportLab invariant mode where available, plus semantic extraction/metadata checks. Font and platform layout remain explicitly versioned renderer inputs.

### 5. Core and optional renderer boundaries

Markdown and JSON stay in the dependency-free core.

The `artifacts` optional extra provides:

- DOCX: `python-docx`;
- PDF: `reportlab`;
- PPTX: `python-pptx`;
- XLSX: `openpyxl`.

Imports occur only inside the relevant renderer call. Missing backends raise one actionable `MissingArtifactBackendError` naming `pip install "praxis-agent[artifacts]"`. Importing `hybridagent.artifacts` remains dependency-free.

Renderers accept validated IR and return bytes. They do not fetch URLs, execute macros, invoke shell commands, or write arbitrary paths. Figures require caller-supplied bytes keyed by declared asset ID; unresolved assets fail closed.

### 6. Accessibility is a release requirement

The IR requires figure alternative text, heading levels, table headers, document language, and meaningful titles. Renderers preserve those semantics where the target format exposes them. Tests inspect Markdown/JSON directly and normalized OpenXML/PDF metadata. The release validation report states unsupported accessibility semantics rather than claiming PDF/UA or Office conformance that was not proved.

### 7. Versioning is append-only and tenant-scoped

Artifact identities and versions are organization/workspace-owned. Versions are append-only, sequential, parent-linked, and content-addressed. SQLite enforces immutable version identity/content and rejects updates, deletes, replacement writes, cross-tenant references, stale-parent writes, and duplicate revision numbers.

`ArtifactStudio.compare()` is semantic: metadata changes, added/removed/changed blocks, citation changes, source-manifest changes, and review/signature changes. It does not present a raw JSON diff as a professional comparison.

### 8. Release bundles are self-describing evidence packages

A release bundle follows BagIt/RO-Crate principles without claiming conformance. It contains:

- canonical IR JSON;
- selected rendered deliverables;
- source and claim/evidence manifests;
- validation report;
- professional review and signature records;
- durable run/checkpoint manifest;
- canonical release manifest with SHA-256 and byte length for every payload.

No archive entry may be absolute, traverse directories, contain backslashes, collide under case-folding, or exceed configured count/size limits. Symlinks and externally fetched payloads are forbidden.

### 9. Release is a fail-closed state machine

`draft -> validated -> pending_review -> approved -> released`.

Release requires:

- current immutable version and expected-head match;
- structural and store-backed validation with zero errors;
- all material claims supported;
- required professional-release review approved by an authorized active reviewer;
- required signature blocks satisfied by distinct active identities;
- every requested renderer successful;
- manifest/hash verification after bundle assembly;
- one atomic release record for the exact version/hash.

Repeated release with the same idempotency key returns the existing result. Conflicting payloads fail. A released version and its release record are immutable.

## Threat model

Tests must cover:

- cross-organization/workspace reads, writes, versions, citations, reviews, and releases;
- duplicate IDs, dangling citations, malformed strict JSON, NaN/infinity, bool-as-int, Unicode ambiguity, and stateful subclasses;
- stale parent/version races and concurrent release attempts from separate SQLite connections;
- direct SQL update/delete/replace/upsert attacks against immutable rows;
- release without supported material claims, exact approval/review, signatures, or current run state;
- ZIP traversal, absolute paths, backslashes, case collisions, duplicate names, symlinks, decompression bombs, and tampered payloads/manifests;
- optional backends absent, partially installed, or raising during render;
- output metadata leaks, external resource fetches, formulas/macros, and unbounded asset sizes.

## Rejected alternatives

- **Pandoc AST as the canonical model:** broad conversion support, but an external executable and insufficient governed-professional semantics.
- **JATS/DocBook as the canonical model:** mature publication schemas, but XML complexity and poor fit for mixed professional verticals.
- **Raw HTML as the canonical model:** easy PDF conversion but unsafe, difficult to validate, and weak for evidence/reference integrity.
- **Office files as source of truth:** unstable producer metadata, difficult semantic comparison, and format-specific identity.
- **Full RO-Crate/PROV/BagIt implementation in Phase 5:** useful export targets, but unnecessary complexity for the first governed bundle. Praxis adopts their proven concepts and keeps a compact native schema.
- **LibreOffice as the primary renderer:** useful for compatibility smoke tests, but subprocess/native-install dependence and cross-version pagination variance make it unsuitable as the canonical rendering path.

## Phase 5 scope boundary

Phase 5 builds artifact infrastructure and generic professional documents. Vertical templates, richer visual design systems, collaborative editing UI, external e-signature providers, formal PDF/UA certification, and standards-export adapters belong to later phases or optional extensions.
