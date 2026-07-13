# Praxis HTTP API v1

- **Status:** Phase 0 stable contract
- **Introduced:** Praxis 0.22.0
- **Base path:** `/api/v1`
- **Legacy compatibility:** `/api/board` remains available until the advertised Sunset date

## Contract conventions

### Success

```json
{
  "api_version": "v1",
  "data": {},
  "meta": {
    "request_id": "..."
  }
}
```

### Error

```json
{
  "api_version": "v1",
  "error": {
    "code": "invalid_request",
    "message": "...",
    "details": {}
  },
  "meta": {
    "request_id": "..."
  }
}
```

`details` is optional. Callers should branch on `error.code`, not message text.

## Authentication

Loopback clients retain the local-first behavior. When the daemon is accessed remotely and `PRAXIS_AUTH_TOKEN` or `agents.auth.token` is configured, v1 reads and writes require either:

```http
Authorization: Bearer <token>
```

or:

```http
X-Praxis-Token: <token>
```

Unauthorized v1 requests return `401` in the stable error envelope.

## Board cards

### List

```http
GET /api/v1/board/cards?limit=50&cursor=<opaque>
```

Response data:

```json
{
  "data": {"items": []},
  "meta": {
    "request_id": "...",
    "next_cursor": null,
    "resource_version": "sha256"
  }
}
```

Rules:

- `limit` defaults to 50 and must be between 1 and 200.
- Cursors are HMAC-signed and bound to the board snapshot.
- A cursor is rejected if altered or if the board changed between pages. The caller must restart pagination.
- `ETag` contains the quoted resource version.
- `If-None-Match` returns `304 Not Modified` when the board is unchanged.

### Create

```http
POST /api/v1/board/cards
Content-Type: application/json
Idempotency-Key: client-generated-operation-id

{"title": "Review evidence", "goal": "Review evidence for matter 123"}
```

Rules:

- `title` or `goal` is required.
- JSON must be an object and at most 64 KiB.
- Idempotency keys are limited to 200 characters.
- The first request returns `201`; a replay returns `200` and `Idempotency-Replayed: true`.
- Reusing a key for a different payload returns `409 idempotency_conflict`.
- Receipts are bounded and persisted in SQLite when the daemon has a Store, so normal restarts preserve replay behavior.
- Simultaneous requests in one daemon are serialized around lookup, side effect and receipt storage.

## Resource versions

Resource versions are SHA-256 hashes of canonical JSON. In Phase 0 they support cache validation and pagination snapshot binding. Later mutating professional resources will additionally use them for optimistic concurrency (`If-Match`).

## Artifact Studio transport status

Phase 5 ships the governed Artifact Studio as a public Python service and durable
persistence boundary (`hybridagent.artifacts.ArtifactStudio`); it does not add a
legacy or unauthenticated HTTP route. See [Artifact Studio](artifacts/README.md).

A future `/api/v1/artifacts` transport must preserve the service invariants:

- derive organization, workspace, and actor from the authenticated session rather
  than trusting request-body scope fields;
- require expected-head identity for version creation and an `Idempotency-Key` for
  release creation;
- conceal cross-scope resources with the standard not-found envelope;
- keep rendered bytes and release bundles bounded and streamed as explicit media
  responses rather than embedding them in JSON;
- return stable conflict codes for stale heads and idempotency conflicts without
  exposing another tenant's identifiers.

## Legacy migration

`GET /api/board` remains unwrapped to avoid breaking the existing Command Deck. It now returns:

```http
Deprecation: true
Sunset: Tue, 12 Jan 2027 00:00:00 GMT
Link: </api/v1/board/cards>; rel="successor-version"
```

Migration:

1. Change the route from `/api/board` to `/api/v1/board/cards`.
2. Read cards from `response.data.items` rather than `response.cards`.
3. Preserve and display `meta.request_id` in support diagnostics.
4. Follow `meta.next_cursor` until null.
5. Store and send `ETag` with `If-None-Match` for refreshes.
6. Supply an `Idempotency-Key` for creates and treat `200` replay and `201` create as success.

## Compatibility policy

- New professional resources are added only under `/api/v1`.
- Existing legacy routes keep their response shape during the announced migration window.
- Breaking changes require a new major API path.
- New error codes may be added; clients must tolerate unknown codes and retain the request ID.
