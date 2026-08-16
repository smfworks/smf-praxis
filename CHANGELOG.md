# Changelog

All notable changes to the open-core `praxis-agent` distribution are recorded
here. The version is single-sourced from `hybridagent.__version__`.

## 0.30.6 — 2026-08-16

- **Security (PRA-001):** Universal Host-integrity gate on every HTTP request.
  Public endpoints (`/api/auth/status`, `/api/readiness`, `/`, `/favicon.ico`)
  previously skipped Host validation, allowing a DNS-rebinding browser to reach
  them with a foreign Host and leak auth/readiness state. `_enforce_host_integrity`
  now runs before all routing in `do_GET`/`do_POST`: loopback peers must send a
  loopback Host; remote peers must send a Host matching the bind host (or an IP
  when bound `0.0.0.0`). Foreign domain Hosts are rejected with 403.
- **Security (PRA-001):** Origin header check for browser-sent POSTs.
  `_enforce_same_origin` rejects POSTs whose `Origin` does not match the request
  Host (the CSRF / cross-site browser vector). CLI clients and server-to-server
  webhooks (Telegram/Slack) that omit `Origin` are unaffected. `/api/auth/login`
  and webhook endpoints are exempt.
- `auth_gate.py` docstring corrected: auth is decided per-request by the HTTP
  handler (Host integrity + token), not solely by the bind address.

## 0.30.5 — 2026-08-15

- `/api/v1/*` GET no longer runs the legacy `_require_auth` envelope first, so
  unauthenticated v1 reads return the structured `{api_version, error}` body.
- Docker HEALTHCHECK and CI probe `/api/readiness` (public). Host-mapped
  `/status` is still token/loopback-gated after 0.30.4.

## 0.30.4 — 2026-08-13

- GET control-plane requires the same Host integrity + token rules as POST.
- Login fail-closed when no token is configured. SSE no longer sets ACAO `*`.

## 0.30.3 — 2026-08-13

- Windows CI: read operator docs as UTF-8 in `tests/test_open_core_docs.py`
  so `pathlib.Path.read_text()` does not decode README/QUICKSTART as cp1252.

## 0.30.2 — 2026-08-13

- Recorded the AGENTS.md verification block against the honesty pass:
  1429 passed / 22 skipped, evals 30/30, ruff, mypy, architecture 4/4, demo.

## 0.30.1 — 2026-08-13

### Changed

- Operator docs now match the 0.29.0 open-core cutover: bundled pack is
  `general` only; `praxis eval` on a clean base install is **30/30**.
- Release-gate reviewer prompts no longer require 40/40 or 36/36 vertical
  cases on the base tree.
- `feature_list.json` HS11 closed as extracted. `PROGRESS.md` current state
  updated from the stale 0.28.32 / in-progress header.

### Added

- `SECURITY.md` — reporting path, supported versions, dashboard-auth posture.
- Contract tests: bundled pack dirs are `{general}`; `activate("homeschool")`
  raises; isolated base eval total is 30.

### Fixed

- QUICKSTART / README no longer instruct `praxis pack activate homeschool`
  against a pack that is not shipped.

## 0.30.0 — 2026-07-28

- Three-reviewer exact-SHA release-gate CLI (`scripts/release-gate.py`).
- Tagged GitHub Release with wheel + sdist.

## 0.29.1 — 2026-07-23

- Fail-open security fixes, pack registration, and vertical discovery.

## 0.29.0 — 2026-07-19

- Vertical extraction cutover. Regulated packs leave the open-core wheel and
  register through `praxis.verticals`. Base eval suite becomes 30 capability /
  safety cases (vertical cases live in the extracted packages).

## 0.28.x and earlier

See git tags `v0.28.32` … `v0.21.3` and historical notes in `PROGRESS.md`.
