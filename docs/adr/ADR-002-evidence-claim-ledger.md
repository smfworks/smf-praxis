# ADR-002: Immutable evidence, observation, claim and custody ledgers

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

Professional outputs must preserve exact support, source versions, transformations, contradictions and review state. Provenance alone does not establish physical or digital chain of custody. Free-form research notes and citations cannot safely support legal, clinical, engineering, architecture, education or consulting release gates.

## Decision

Create related but distinct records:

- `SourceRecord` and immutable `SourceVersion` for origin, authority, hash, original and parser lineage;
- `EvidenceSpan` for exact page/text/table/image/time/repository coordinates;
- `Observation` for who/what/when/where/how, method, instrument and uncertainty;
- `EvidenceItem` and append-only `CustodyEvent` for possession, transfer, condition, seal, location and fixity;
- `ClaimRecord`, evidence links and contradictions for facts, inferences, assumptions, recommendations and professional opinions.

Generated text, OCR, captions, transcripts and enhancements are derivatives linked to originals. Material unsupported claims block professional release. Authority, applicability and freshness filtering occurs before relevance ranking.

## Alternatives considered

1. **Store citations only in generated prose.** Rejected because citations cannot support impact analysis, contradiction handling or exact verification.
2. **Treat vector chunks as evidence.** Rejected because chunks are retrieval derivatives, not immutable originals or stable professional locators.
3. **Use one provenance log for custody.** Rejected because custody requires possession, condition, transfers and disposition beyond informational lineage.

## Consequences

- Artifacts become reproducible and independently reviewable.
- Source changes can invalidate dependent claims and approvals.
- Storage and migration are more complex but deterministic.
- Licensed standards and restricted sources retain license/purpose metadata rather than copied content.

## Security and integrity

Originals are read-only; events are append-only; hashes are verified at acquisition and review. Retrieved content remains untrusted data and cannot issue tool instructions. Corrections supersede records rather than overwrite history.

## Dependency rule

Hashing, JSON records and the core ledger use the standard library. OCR, document parsing, imaging and specialized stores remain lazy optional adapters.
