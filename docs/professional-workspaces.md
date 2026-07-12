# Professional workspaces and context isolation

Praxis `0.24.0` introduces tenant-owned professional workspaces as the mandatory
context boundary for session-backed professional operations.

## Workspace model

Each workspace has immutable `workspace_id` and `organization_id` values, a
case-insensitive tenant-unique human identifier, one of nine closed workspace
kinds, an active owner, optional in-tenant team, confidentiality, lifecycle,
legal-hold, vertical custom fields, and external-system links.

Archive state and legal hold are independent. Archiving does not remove a legal
hold or permit deletion.

## Professional API

All endpoints use the `/api/v1` envelope and professional session/CSRF rules.

- `GET /api/v1/workspaces` lists only the authenticated organization.
- `POST /api/v1/workspaces` creates a workspace in that organization.
- `GET|POST /api/v1/board/cards` requires `X-Praxis-Workspace-ID`.
- `GET|POST /api/v1/workspace/timeline` requires the workspace header.
- `POST /api/v1/workspace/rooms` creates a controlled collaboration room.

A session-backed request without the workspace header receives
`workspace_required` (`400`). A workspace outside the session organization is
reported as `workspace_not_found` (`404`) to avoid tenant enumeration. Legacy
loopback/shared-token calls retain the explicit unscoped compatibility path.

## Isolation guarantees

The canonical `WorkspaceScope` binds memory, knowledge namespaces, runs, traces,
board cards, and context keys to the immutable workspace ID. Professional board
idempotency keys are namespaced by both organization and workspace. The same key
can therefore be used independently in two workspaces without aliasing effects.

Timeline events are append-only and receive a transactionally allocated sequence.
Consequential deadlines retain calculation source/rule and require explicit
review.

## External collaboration

External rooms belong to exactly one workspace. Invitations are explicit,
expiring, and revocable. Permissions use a closed vocabulary (`read_shared`,
`comment`, `upload`); tool execution and workspace memory access cannot be
granted. Shared content is item-allowlisted rather than workspace-wide.

## Migration

SQLite migration is additive. Existing memory, runs, and board cards receive an
empty workspace ID and remain visible only through legacy/admin compatibility
methods. Professional session routes never select these unowned records.
