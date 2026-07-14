# Praxis Artifact Studio

Artifact Studio converts governed evidence and durable professional workflow state
into validated, versioned deliverables and self-verifying release packages.

## Status

Implemented in Praxis `0.27.5` as the PP50 / Phase 5 capability. JSON and Markdown
remain dependency-free. DOCX, PDF, PPTX, and XLSX use the optional `artifacts`
extra.

## Install

```bash
# dependency-free model, validation, JSON/Markdown, persistence, and bundles
pip install .

# rich Office/PDF renderers
pip install ".[artifacts]"
```

## Public Python API

```python
from hybridagent.artifacts import (
    ArtifactDocument,
    ArtifactStudio,
    ArtifactValidationError,
    DocumentMetadata,
    ParagraphBlock,
    RevisionRecord,
    Section,
    compare_documents,
    render_artifact,
    validate_or_raise,
    verify_release_bundle,
)
```

`hybridagent.artifacts` is safe to import without optional dependencies. Rich
renderer modules load their third-party backend only when that format is requested.
A missing backend raises `MissingArtifactBackendError` with the exact install
command.

## Lifecycle

1. Build an immutable `ArtifactDocument` whose metadata carries the owning
   organization/workspace and whose citations reference exact source versions,
   evidence spans, and claims.
2. Call `validate_or_raise(document)`. Canonical JSON is the identity surface and
   `document.content_hash()` is its SHA-256.
3. Persist with `ArtifactStudio.create_version(...)`. Initial versions have no
   parent; later versions require the exact current `expected_parent_version_id`.
4. Render any persisted version with `render_version(...)`, or compare two versions
   semantically with `compare(...)`.
5. Request and approve an exact `professional_release` review whose subject is:

   ```python
   {
       "artifact_id": version.artifact_id,
       "version_id": version.version_id,
       "document_sha256": version.document_hash,
   }
   ```

6. Bind the reviewer decision with `sign_version(...)`.
7. Call `release_version(...)` with the exact durable run/checkpoint and a scoped
   idempotency key. The release transaction revalidates the current artifact head,
   supported material claims, exact review, signer membership/role, and run state.
8. Store or transmit `ArtifactRelease.bundle`. Any recipient can call
   `verify_release_bundle(bundle)` without access to the original database.

## Core service calls

```python
studio = ArtifactStudio(store)

version = studio.create_version(
    organization_id,
    workspace_id,
    document,
    created_by=actor_id,
    assets={"figure-asset-id": figure_bytes},
    expected_parent_version_id="",  # exact prior head for revisions
)

rendered_markdown = studio.render_version(
    organization_id, workspace_id, version.version_id, "markdown"
)

diff = studio.compare(
    organization_id, workspace_id, prior_version_id, version.version_id
)

signature = studio.sign_version(
    organization_id,
    workspace_id,
    version.version_id,
    review_id=approved_review_id,
    signer_user_id=reviewer_id,
    role="reviewer",
    meaning="approved for professional release",
)

release = studio.release_version(
    organization_id,
    workspace_id,
    version.version_id,
    formats=("json", "markdown"),
    run_id=run_id,
    checkpoint_id=checkpoint_id,
    created_by=actor_id,
    idempotency_key="matter-123-final-v1",
)

assert studio.verify_release(release) == release.manifest
```

Supported format names are `json`, `markdown`, `docx`, `pdf`, `pptx`, and `xlsx`.
Every governed release must include `json` and `markdown`. Requested format order is
canonicalized; repeated calls with the same scoped idempotency key and request
return the original release. Reusing the key for another request fails closed.

## Release bundle layout

```text
manifest.json
artifact/document.json
renders/document.json
renders/document.md
renders/document.docx      # when requested
renders/document.pdf       # when requested
renders/document.pptx      # when requested
renders/document.xlsx      # when requested
assets/<declared-asset-id>
governance/claims.json
governance/evidence.json
governance/reviews.json
governance/signatures.json
governance/run.json
validation/report.json
```

`manifest.json` is canonical JSON and records release identity plus the SHA-256,
byte length, media type, and safe relative path for every other member. ZIP member
names and timestamps are fixed and sorted for byte-for-byte reproduction.

## Security and integrity guarantees

- Organization/workspace ownership is applied to every artifact, version, asset,
  signature, release, evidence lookup, review, and run/checkpoint lookup.
- Versions, assets, signatures, and releases are append-only. SQLite triggers block
  update, delete, conflict-upsert, and `INSERT OR REPLACE` mutation paths.
- Expected-head version writes and release idempotency are serialized with
  `BEGIN IMMEDIATE`, including cross-process SQLite writers.
- Citations bind exact source IDs, source-version IDs, content hashes, span IDs, and
  linked claim IDs.
- Release-time authorization rechecks that each signer and creator remains active
  in the owning scope and that the signer still holds the recorded role.
- Bundle verification rejects malformed/noncanonical JSON and ZIP order/metadata,
  duplicate names, case-insensitive collisions, absolute, traversing, drive-qualified,
  Windows-reserved, or backslash paths, directories, symlinks, unsupported formats,
  unexpected members, member and aggregate size violations, unreadable payloads,
  hash/size or declared media-type mismatches, and document/release scope, artifact,
  asset, or digest identity mismatch.
- Renderers never retrieve external resources. Figure bytes must be supplied by the
  caller and match declared asset IDs exactly.

## Accessibility boundary

The IR requires document language and title semantics, heading levels, table header
structure, and non-empty figure alternative text. Renderers preserve supported
semantics and deterministic metadata. Phase 5 does **not** claim formal PDF/UA or
Office accessibility certification; certification and vertical-specific templates
remain separate release concerns.

## Interface boundary

Phase 5 exposes a stable Python API and persistence substrate. It does not add an
unauthenticated legacy HTTP route. A future `/api/v1` transport must derive
organization, workspace, and actor identity from an authenticated session and call
this service without accepting those authorization fields from untrusted request
bodies.

See [the Phase 5 architecture decision](phase-5-architecture.md) for research,
trade-offs, determinism rules, and the complete threat model.
