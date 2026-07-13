# Durable Professional Workflows

Status: Phase 4 release candidate (`0.26.4`)

## Scope

Praxis persists professional workflow runs under immutable organization and workspace IDs. The runtime supports:

- append-only checkpoints and fork provenance;
- revocable lifecycle transitions;
- `GoalRunner` turn checkpoints;
- resumable `PlanExecutor` step graphs;
- typed professional-review interruptions;
- structured research findings and review outcomes;
- durable consequential-effect intent and receipt records.

Human/organization identity remains separate from cryptographic agent identity.

## Run lifecycle

A run starts as `running`. Legal transitions are transactionally enforced:

```text
running -> interrupted -> running
running -> cancelled
interrupted -> cancelled
running -> completed
running -> failed
```

Terminal runs cannot be resurrected. Every transition uses a conditional update inside `BEGIN IMMEDIATE`; a stale or concurrent transition fails.

Cancellation is checked before each next GoalRunner turn and PlanExecutor step. Once an external provider call has begun, cancellation cannot retract an effect already accepted by that provider.

## Checkpoints

Each checkpoint contains strict JSON state and a schema manifest. Exact-domain validation rejects:

- non-string object keys;
- tuples and custom container subclasses;
- non-finite floats;
- unsupported values;
- malformed persisted `PlanStep` field types or unknown statuses.

SQLite triggers make checkpoints append-only and bind every checkpoint's organization/workspace scope to its owning run. Sequence allocation and head advancement are transactional.

Forks copy checkpoint state and schema into a new run while preserving source-run and source-checkpoint provenance. Effect receipts are never inherited.

## Durable PlanExecutor

A durable executor requires all of:

- `CheckpointRegistry`;
- `organization_id`;
- `workspace_id`;
- `run_id`;
- `actor_id`.

It checkpoints the plan and every step transition. Restart reconstruction preserves completed work and evaluates held or interrupted work as follows:

- `done`: never re-executed;
- `held`: remains held unless its durable approval is `approved` and exactly matches organization, tool, and arguments;
- `running` read/draft: may be retried;
- `running` consequential intent: may be retried only with a provider idempotency key and the exact durable approved action;
- consequential intent without safe reconciliation evidence: fails closed for manual reconciliation.

Approval resumption is a separate executor invocation. `SEND` and `DESTRUCTIVE` actions remain held in enforced mode and are never executed in the same invocation that created their approval.

## Effect delivery semantics

Praxis does **not** claim exactly-once network execution.

The protocol is:

1. validate and authorize the exact action;
2. persist an immutable checkpoint containing a `pending_execution` intent;
3. call the external provider with its idempotency key when available;
4. persist an immutable effect receipt;
5. checkpoint the completed outbox entry and receipt reference.

Two crash windows are handled explicitly:

- **Provider accepted, receipt absent:** retry only when the durable approved action and provider idempotency key match. Delivery is provider-idempotent at-least-once.
- **Receipt committed, completion checkpoint absent:** reconstruct completion from the immutable receipt without calling the provider again.

Providers without idempotency support require manual reconciliation after an ambiguous crash.

## Professional reviews

Supported review types:

- `quality`;
- `professional_release`;
- `research_findings`.

Supported decisions:

- `approved`;
- `revise`;
- `rejected`.

A review is organization/workspace scoped, role bound, strict-JSON validated, and maker-checker separated. The reviewer must be an active member with the required role and must differ from the creator.

Decision submission uses `BEGIN IMMEDIATE` and a pending-only conditional update. Across concurrent processes, exactly one decision wins. Database triggers enforce run-scope matching, prohibit self-review, prevent changes after decision, and prevent deletion.

Creating a normal run-backed review and interrupting its run occurs in one transaction, so a failed or concurrent interrupt cannot leave an orphan review.

## Research supervision

`ResearchSupervisor` stores:

- the query;
- hypotheses;
- structured findings with source IDs and confidence;
- pending review identity;
- the final review decision and payload.

Decision outcomes control lifecycle:

| Decision | Research state | Run state |
|---|---|---|
| `approved` | `reviewed` | `running` |
| `revise` | `collecting` | `running` |
| `rejected` | `rejected` | `failed` |

This substrate records and governs research work. It does not provide legal advice, clinical decision support, or autonomous release of high-consequence conclusions.

## Verification

Release candidates must pass:

```bash
python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q
python3 -m hybridagent.cli eval
python3 -m ruff check hybridagent/
python3 -m mypy hybridagent --ignore-missing-imports
python3 -m hybridagent.cli demo
python3 scripts/check_architecture.py
```

The wheel and source distribution must also build, install in a clean virtual environment, and report the expected package version. Independent exact-head maker-checker approval remains mandatory before Phase 4 is marked passing or released.
