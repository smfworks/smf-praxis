# ADR-004: Controlled system-of-record integration boundary

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

Praxis must connect to legal DMS/practice systems, EHR/FHIR, dental PMS/DICOM, BIM/CDE, LMS/SIS, CRM/PSA, Git and work-management systems without replacing their mature transactional ledgers. External writes are consequential, retry-prone and capable of corrupting authoritative records.

## Decision

Define optional adapters behind one core contract: capabilities, risk class, scopes, external IDs, source provenance, sync cursor, refresh time, health, data residency, rate limits and webhook idempotency. Reads preserve source identifiers and access policy. Writes use preview, qualified action approval, immutable payload/version binding, idempotency key, effect receipt, reconciliation and compensation where supported.

Praxis is the intelligence, evidence and work-product layer. It does not become an EHR, dental ledger, trust-accounting system, CAD/BIM authoring tool, SIS/LMS or PSA/ERP.

## Alternatives considered

1. **Direct connector calls from vertical code.** Rejected because governance, retries and audit would diverge.
2. **Copy external systems into Praxis.** Rejected because it creates stale shadow records and excessive regulatory scope.
3. **Make integrations mandatory core dependencies.** Rejected because Praxis must remain local-first and dependency-free by default.

## Failure semantics

No silent overwrite. Every mutation has a deterministic idempotency key. Partial failures enter reconciliation/dead-letter state. Approval expires if destination, payload, evidence, connector mapping or policy changes. Webhook duplicates are rejected. Secrets are referenced through approved credential stores and never serialized into traces.

## Migration

Existing integrations are wrapped behind compatibility adapters, then moved to the common registry incrementally. Production claims require conformance, security and exact end-to-end scenario tests.
