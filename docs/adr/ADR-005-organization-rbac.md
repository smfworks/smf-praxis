# ADR-005: Organization identity with RBAC, attributes and purpose-of-use

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

A shared dashboard token is sufficient for local single-user deployment but cannot express professional identity, organization boundaries, qualified reviewer roles, ethical walls, patient relationships, guest access, delegated authority or purpose-of-use.

## Decision

Preserve frictionless loopback single-user mode while adding organizations, users, memberships, teams, role assignments and revocable sessions. Authorization combines RBAC with contextual attributes: organization, workspace, relationship, role, sensitivity, jurisdiction, purpose, action and time. Deny by default.

Browser authentication uses secure HttpOnly/SameSite cookies, CSRF protection, expiry and revocation. Passwordless local bootstrap is supported. OIDC/SAML/SCIM are optional adapters. Break-glass requires a reason, limited duration and retrospective review.

Qualified authority is explicit. A reviewer role cannot inherit mutation capability merely by task assignment. Approval records bind authenticated actor, role, policy, object/evidence version and decision.

## Alternatives considered

1. **Continue with one bearer token.** Rejected for team and regulated deployments.
2. **RBAC only.** Rejected because professional access depends on matter/patient relationship, purpose and sensitivity.
3. **External identity provider required.** Rejected because local-first and offline deployments must remain viable.

## Security consequences

- Cross-organization and cross-workspace access is denied before retrieval or ranking.
- Browser bearer tokens are not stored in local storage.
- Session fixation, CSRF, expiry, revocation, guest lifecycle and break-glass receive dedicated tests.
- Authentication does not weaken the existing broker; it supplies verified actor context to it.

## Migration

Existing loopback installs receive a local organization and admin identity automatically. Existing token behavior remains a compatibility path for CLI/API automation during a documented deprecation window.
