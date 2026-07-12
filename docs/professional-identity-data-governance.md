# Professional Identity and Data Governance

- **Introduced:** Praxis 0.23.0
- **Status:** Phase 1 release candidate
- **Core dependencies:** none

## Organization directory

Praxis now models:

- organizations;
- normalized global user identities;
- organization-scoped memberships and roles;
- organization-scoped teams;
- team membership constrained by organization membership.

Professional role vocabulary:

- `organization_admin`
- `workspace_admin`
- `professional`
- `reviewer`
- `member`
- `external_collaborator`
- `auditor`

Unknown roles are rejected. Membership and team queries require an explicit organization or team identifier; cross-organization membership is never inferred.

## Professional sessions

`POST /api/v1/auth/session` exchanges an already authenticated deployment/bootstrap context for an opaque professional session. The request identifies an existing active user and organization membership.

The response sets `praxis_session` with:

- `HttpOnly`;
- `SameSite=Strict`;
- `Path=/api/v1`;
- bounded `Max-Age`;
- `Secure` for non-loopback clients.

Only SHA-256 hashes of session and CSRF secrets are stored. Mutation requests made with a professional cookie must include the matching `X-CSRF-Token`. Sessions are bounded, revocable by session or device, and become invalid immediately when the user, organization, or membership is disabled.

Loopback operation remains frictionless when no professional cookie is presented. Presenting an invalid/expired cookie does not fall through to loopback trust.

`POST /api/v1/auth/logout` requires the session cookie plus CSRF token and revokes the server-side session.

The stdlib `IdentityProvider` protocol is the adapter boundary for optional OIDC or SAML implementations. Provider SDKs remain optional extras and are not imported by the core.

## Authorization

`AuthorizationPolicy` combines:

1. organization scope;
2. explicit role/action grants;
3. data classification;
4. purpose of use;
5. optional audited break glass.

Rules are deny-by-default. Reviewer roles can read/comment/review content but cannot mutate records or execute tools. Cross-organization access is denied unless an eligible read-only break-glass request includes a reason. Every break-glass decision produces an audit event.

Professional approvals use `POST /api/v1/approvals/{approval_id}/approve`. The route requires a professional session and CSRF token, checks current membership and role, and records the authenticated user ID and role in the persisted approval signature. Caller-supplied actor names are not trusted on this route.

## Classification and retention

Closed classification vocabulary:

- `public`
- `internal`
- `confidential`
- `privileged`
- `phi`
- `education_record`
- `evidence`

`DataPolicy` provides deterministic retention disposition, legal-hold override, deletion authorization, redaction-required export decisions, and connector egress allowlists. The governance broker enforces classified connector egress before autonomous or approval behavior, so permissive routing cannot accidentally send PHI, privileged material, education records, or evidence to unapproved connectors.

Default retention periods are conservative policy defaults, not legal advice. Deployments must supply jurisdiction- and organization-specific rules before regulated production use.

## Compatibility and boundaries

- Existing shared-token access remains available for compatibility.
- Existing loopback single-user behavior remains available.
- Existing legacy approval routes remain compatible.
- Professional deployments should migrate to session-backed `/api/v1` routes.
- Workspace-level scoping begins in Phase 2; Phase 1 establishes organization identity and policy boundaries only.
- High-consequence legal or clinical functionality remains out of scope.
