# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.30.x | Yes |
| 0.29.x | Security fixes only |
| < 0.29 | No |

Praxis publishes GitHub Releases only. `pip install praxis-agent` from PyPI is not an official channel.

## Reporting a vulnerability

Email **security@smfworks.com** or open a **private** GitHub security advisory on
[`smfworks/smf-praxis`](https://github.com/smfworks/smf-praxis). Do not file a
public issue for a live exploit.

Please include: affected version (`praxis --version`), a minimal reproduction,
and whether the report touches the governance spine (broker, allowlist,
kill-switch, egress firewall, injection boundary, dual-approval).

We will acknowledge receipt and say whether the report is in scope.

## What is in scope

- Bypass of `SEND` / `DESTRUCTIVE` approval, dual-approval, or the kill-switch
- Egress-firewall or prompt-injection-boundary bypass
- Policy-hook `allow` weakening the spine (allowlist, kill-switch, egress)
- Path traversal out of `PRAXIS_WORK_DIR`
- Secret leakage from `~/.praxis/` credential files
- Unauthenticated control-plane exposure when bound to a routable address
  *without* the documented reverse-proxy / VPN / SSH front door

## What is not a vulnerability

- The Command Deck HTML shell (`/` and `/web/*`) is public. **GET and POST
  control-plane routes require loopback Host integrity or a shared token.**
  Binding `0.0.0.0` without `PRAXIS_AUTH_TOKEN` mints a token; if minting
  fails the daemon refuses to start. Loopback clients remain the local
  operator.
- Offline mock-LLM behavior
- Extracted vertical packs (`praxis-legal`, `praxis-medical`, …) — report those
  against their own repositories

## Operator defaults

- Bind loopback unless you have put TLS + auth in front. See `docs/DEPLOYMENT.md`.
- Keys live in env vars, the OS keychain (`[keyring]` extra), or
  `~/.praxis/auth-profiles.json` (gitignored). Never commit secrets.
- Credential files are restricted to the current user (`chmod 0600` on POSIX,
  `icacls` on Windows) via `config.secure_file`.
- A policy hook may **tighten** the broker. It may never weaken allowlist,
  kill-switch, or egress.

## Governance spine

Every tool — native, MCP, plugin, or A2A — is risk-classified and authorized by
one broker. Do not propose patches that execute `SEND`/`DESTRUCTIVE` inline,
skip dual-approval, or treat retrieved content as instructions.

## GLM-5.3 security audit — acknowledged by-design decisions

A one-shot static security audit (GLM-5.3, 2026-08-18) reviewed 12 key files and
recorded 19 findings. The full interactive report is at
<https://www.smfclearinghouse.com/demos/glm-5.3-security-report/>. The following
findings are acknowledged as **by-design** and will not be "fixed" without an
explicit design discussion — they are intentional trade-offs documented here so
future reviewers do not re-flag them:

- **PRA-007 — Plaintext credential storage (`config.py`).**
  `~/.praxis/auth-profiles.json` stores API keys in plaintext. Encrypting at
  rest would require a crypto dependency, violating the dependency-free core
  constraint (AGENTS.md). The file is restricted to the current user via
  `config.secure_file` (`chmod 0600` on POSIX, `icacls /inheritance:r` on
  Windows). This risk is acceptable for a local-first agent. Do **not** add
  encryption dependencies to the core.

- **PRA-008 — Permissive compliance mode (`broker.py`).**
  The three-mode system (`enforced` / `autonomous` / `permissive`) is a
  deliberate feature. The default is `enforced`. `permissive` is an explicit
  operator opt-in for trusted or sandboxed environments (e.g. an isolated
  coding workspace) where the egress firewall and injection detection are
  intentionally relaxed but the kill-switch remains active. Do **not** remove
  or restrict `permissive` mode.

P0/P1 findings from the same audit are addressed in dedicated PRs (PRA-001, -002,
-005, -008, -015). P3 informational findings are tracked as GitHub issues.
