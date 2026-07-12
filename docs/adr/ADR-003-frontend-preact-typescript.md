# ADR-003: Workspace-first Preact and TypeScript frontend

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

The current static JavaScript Command Deck is effective for an operator but is chat-first and exposes run/infrastructure concepts before professional context. The target product needs workspace navigation, role-aware dashboards, evidence/claim review, artifact editing, external rooms, accessible interaction and versioned API consumption.

## Decision

Build a separate `frontend/` application using Preact, TypeScript and Vite. Frontend dependencies are build-time only; compiled static assets ship inside the Python wheel and are served by the existing daemon. The Python runtime remains dependency-free.

The primary information architecture is Today, Workspaces, Research, Documents, Tasks, Approvals, Knowledge and Reports. Admin/operator surfaces are separated. Chat is a collapsible workspace-scoped collaborator that always displays scope, classification, model locality and approval state.

Adopt design tokens and accessible components with WCAG 2.2 AA as the target. Use semantic HTML, keyboard-complete paths, visible focus, reduced motion, responsive layouts and accessible exports. Safety state is never represented by color alone.

## Alternatives considered

1. **Continue expanding inline vanilla JavaScript.** Rejected because typed API migration, complex state and accessible component reuse would become fragile.
2. **Next.js or another server framework.** Rejected because it adds a production runtime and conflicts with local single-binary-style deployment.
3. **Full React.** Viable, but Preact provides the required component model with a smaller bundled footprint.

## Migration

Serve the new application behind a feature flag. Maintain existing routes and the legacy dashboard until API parity and Playwright scenarios pass. Retire inline assets only after rollback is tested.

## Quality gates

Pinned/audited build dependencies, lint, typecheck, unit tests, production build, Playwright E2E, keyboard tests, accessibility scans, browser-console checks and visual regression on supported viewports.
