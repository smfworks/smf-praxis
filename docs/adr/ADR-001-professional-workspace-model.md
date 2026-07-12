# ADR-001: Professional workspace as the primary product aggregate

- **Status:** Accepted
- **Date:** 2026-07-12
- **Decision owners:** SMF Works / Praxis maintainers

## Context

Praxis currently centers chat, runs, knowledge and work-board goals. Professional users instead organize work around a matter, patient encounter, dental case, forensic case, building project, course, learner portfolio or consulting engagement. These boundaries also determine confidentiality, retrieval, retention, responsibility and approval authority.

## Decision

Introduce one normalized `Workspace` aggregate with organization scope, kind, identifier, accountable owner, team, lifecycle, jurisdiction/location, sensitivity, policy version and external links. Vertical modules contribute validated extension schemas; they do not add nullable columns to a universal mega-table or fork the aggregate.

All durable professional objects—including chat threads, memory, retrieval, runs, tasks, evidence, claims, artifacts and external effects—must carry an authorized workspace context. Cross-workspace linking is explicit, policy-checked and audited. Chat becomes a contextual collaborator within a workspace rather than the sole record.

## Alternatives considered

1. **Keep chat/session as the root.** Rejected because sessions do not represent professional responsibility, retention or system-of-record boundaries.
2. **Independent schema per profession.** Rejected because it duplicates identity, evidence, workflow and audit infrastructure and encourages governance drift.
3. **Generic EAV object store.** Rejected for critical fields because validation, querying, migration and policy become opaque.

## Consequences

- Shared behavior can be built and tested once across nine professions.
- Vertical semantics remain explicit through versioned schemas and modules.
- Existing unscoped records require a compatibility/default workspace during migration.
- Every API and persistence path must prove workspace isolation.

## Migration

Add new tables and nullable compatibility references first. Create a local/default organization and workspace for existing single-user data. Backfill references transactionally, then make scope mandatory for new professional APIs. Keep legacy APIs until the workspace-first UI is migrated.

## Dependency rule

The Python core remains standard-library-only. Workspace validation uses built-in typed records and deterministic schema checks; third-party identity or validation systems are optional adapters.
