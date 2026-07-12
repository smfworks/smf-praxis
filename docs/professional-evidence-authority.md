# Professional Evidence, Claims, Custody, and Authority

Praxis `0.25.x` adds the governed evidence substrate used by professional workspaces.
It does not provide legal conclusions, clinical recommendations, or autonomous
professional approval.

## Canonical evidence

`EvidenceRegistry` stores tenant/workspace-owned canonical sources and immutable
source versions. Each version records a SHA-256 content hash, MIME type, retrieval
time, parser/version/configuration, license, original object path, and actor. SQLite
triggers reject version updates and deletion. Supersession is an append-only edge;
prior versions remain addressable.

## Exact extraction lineage

`ExtractionRegistry` links exact locations to immutable versions. Supported locator
shapes cover document page/section/paragraph/character range, table/cell, image
bounding box, media time range, and repository commit/path/line range. OCR,
captions, transcripts, summaries, and extractions are append-only derived artifacts
with extractor name, version, configuration, and parent span.

## Chain of custody

`CustodyLedger` records acquisition, transfer, copy, transformation, analysis,
verification, and disposition. Sequence allocation occurs inside `BEGIN IMMEDIATE`.
Each event hashes its canonical fields and the previous event hash. Database triggers
prevent update or deletion; `verify_chain()` detects gaps or tampering.

## Claims and professional release

`ClaimLedger` separates assertions from evidence. Evidence-span links explicitly
support or contradict a claim. Material claims are one of supported, contradicted,
unresolved, or abstained. A material claim can become supported only after a scoped
support link exists. `AnswerVerifier` fails closed with `material_claims` while any
material workspace claim is not supported.

## Authority policy

`filter_authority()` rejects sources for retraction, supersession, wrong jurisdiction,
unaccepted authority tier, staleness, or population mismatch before similarity
ranking. Accepted sources rank by configured authority tier and then similarity.
Legal, medical, dental, forensic-engineering, architecture, and education policy
modules provide closed defaults; deployments must supply the actual jurisdiction
and, where relevant, population.

## Runtime wiring

- `ingest.register_evidence()` persists source → version → document span.
- `MediaClient.process_with_lineage()` persists original media, a precise source span,
  and derived caption/transcript lineage.
- `AnswerVerifier.verify(..., claim_ledger=..., organization_id=...,
  workspace_id=...)` blocks professional release on unresolved material claims.

All operations derive ownership from explicit organization/workspace scope. Callers
must still enforce authenticated workspace selection at the transport boundary before
exposing these services over HTTP.
