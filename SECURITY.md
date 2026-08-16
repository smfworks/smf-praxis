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

- The Command Deck HTML shell (`/` and `/web/*`) is public. **Every HTTP
  request — including public endpoints — is validated against DNS-rebinding:
  the Host header must be loopback (localhost / 127.x / ::1) for loopback
  peers, or match the configured bind host for remote peers. GET and POST
  control-plane routes additionally require a shared token when the peer is
  not a loopback Host. Browser-sent POSTs with an `Origin` header must match
  the request Host (same-origin); CLI clients without `Origin` are
  unaffected.** Binding `0.0.0.0` without `PRAXIS_AUTH_TOKEN` mints a token;
  if minting fails the daemon refuses to start. Loopback clients remain the
  local operator.
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
