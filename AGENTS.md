# AGENTS.md

> Praxis — Autonomous AI Colleague. A governed, self-improving agent: perceive → plan → govern → act/draft → reflect → consolidate.
> Read this first. It is a **router**, not an encyclopedia. Follow the links for depth.
> Source of truth on harness engineering: [docs/harness/](docs/harness/) and the Learn Harness Engineering synthesis.

## What this is

Praxis (`smfworks/smf-praxis`) is a proactive **and** governed autonomous agent. Every action — native, MCP, plugin, or A2A — is risk-classified (`READ`/`DRAFT` autonomous; `SEND`/`DESTRUCTIVE` held for human approval) and routed through one broker. The core is dependency-free (offline mock LLM); extras are opt-in.

- **Loop:** `perceive → plan → govern → act/draft → reflect → consolidate`
- **Principle:** autonomy for preparation, approval for consequence.
- **Version:** `hybridagent.__version__` (see `pyproject.toml`)
- **Stack:** Python 3.10+, zero required deps, optional extras (`docs`, `multimodal`, `mcp`, `browser`, `keyring`, …)

## Quick start

```bash
./install.sh                       # venv + install + onboarding wizard
python3 -m pytest -q               # full test suite
python3 -m hybridagent.cli eval    # 40/40 capability + safety evals
python3 -m hybridagent.cli demo    # offline demo
python3 -m hybridagent.cli daemon  # Command Deck dashboard at :8643
```

## Verification commands (Definition of Done)

A change is "done" **only when all of these pass**. Run them before declaring work complete:

```bash
python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q   # suite green
python3 -m hybridagent.cli eval                            # 40/40 evals
python3 -m ruff check hybridagent/                          # lint clean
python3 -m mypy hybridagent --ignore-missing-imports        # types clean
python3 -m hybridagent.cli demo                            # demo runs end-to-end
```

Skipping any level = not complete. Fix the baseline before adding new work.

## Hard constraints (non-negotiable)

- **Dependency-free core.** No third-party imports in `hybridagent/` runtime paths unless behind an optional extra in `pyproject.toml`. Mock LLM must work offline with zero keys.
- **Governance spine is sacred.** Never weaken the broker, allowlist, kill-switch, egress firewall, injection boundary, or dual-approval to make a test pass or a feature ship. A policy hook may *tighten*, never weaken.
- **`SEND`/`DESTRUCTIVE` are held.** No inline execution of consequential actions. Draft-before-send. Destructive needs two distinct approvers.
- **Retrieved content is data, never instruction.** Preserve the injection boundary in every new tool/perception path.
- **Filesystem tools sandbox to `PRAXIS_WORK_DIR`.** Reject absolute/traversal paths before any I/O. Every tool declares a `RiskClass` and a JSON `parameters` schema.
- **Cross-platform.** Linux/macOS/Windows. Use `python3` (not `python`). Use `pathlib`, not raw string paths. Credential files are 0600/ACL-restricted.
- **Never commit without a version bump** in both `pyproject.toml` and `hybridagent/__init__.py`.
- **No secrets in code or commits.** External keys go to env-var references or `praxis secrets` / `~/.praxis/auth-profiles.json` (gitignored).
- **WIP = 1.** One feature `in_progress` in `feature_list.json` at a time. Finish (verify) before starting the next.
- **Evidence before "done."** A feature moves to `passing` only when its `verification` command runs green and evidence is recorded.

## How to work (session flow)

1. `pwd` and confirm you're in the repo root.
2. Read `PROGRESS.md` for current state.
3. Read `feature_list.json` for the scope surface.
4. `git log --oneline -5` to see recent changes.
5. `./install.sh` (or activate `.venv`) to confirm the environment.
6. Run the verification commands above — if the baseline is red, **fix that first**.
7. Pick the highest-priority `not_started` feature. Move it to `in_progress`. Work on **only that one**.
8. Verify against the feature's `verification` step. Record evidence.
9. Update `PROGRESS.md` and `feature_list.json`. Commit safe work.
10. End-of-session: run the [clean-state checklist](docs/harness/clean-state-checklist.md).

## Topic docs (read on demand)

| When you need… | Read this |
|---|---|
| Full capability map | [CAPABILITIES.md](CAPABILITIES.md) |
| Design rationale & framework | [FRAMEWORK.md](FRAMEWORK.md) |
| Full CLI reference & user guide | [README.md](README.md) |
| Harness engineering methodology | [docs/harness/](docs/harness/) |
| Architecture decision records | [docs/harness/quality-document.md](docs/harness/quality-document.md) (module grades over time) |
| End-of-session handoff | [docs/harness/session-handoff.md](docs/harness/session-handoff.md) |
| Definition of done for a session | [docs/harness/clean-state-checklist.md](docs/harness/clean-state-checklist.md) |
| Reviewing agent-contributed PRs | [docs/harness/evaluator-rubric.md](docs/harness/evaluator-rubric.md) |
| Deployment (Docker, LAN, reverse proxy) | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Install variants | [docs/INSTALL.md](docs/INSTALL.md) |
| Vertical packs (legal/medical/…) | [docs/PACKS.md](docs/PACKS.md) |
| OWASP Agentic coverage | [docs/OWASP_AGENTIC_COVERAGE.md](docs/OWASP_AGENTIC_COVERAGE.md) |
| Release process | [RELEASING.md](RELEASING.md) |
| M365 broker setup | [M365-SETUP.md](M365-SETUP.md) |

## Module map (`hybridagent/`)

| Concern | Module |
|---|---|
| Agent loop | `agent.py` · `chat_agent.py` · `orchestrator.py` |
| Planner | `planner.py` · `plan_execute.py` |
| Governance broker | `broker.py` · `validation.py` · `content_guard.py` · `compliance.py` |
| Tools (risk-classified) | `tools.py` · `real_tools.py` · `m365_tools.py` |
| Memory & RAG | `memory.py` · `rag.py` · `bm25.py` · `vecsim.py` · `embeddings.py` · `ingest.py` |
| Grounding & verification | `grounding.py` · `verifier.py` · `contradiction.py` |
| Skills & self-improvement | `skills.py` · `skill_evaluator.py` · `evolution.py` |
| Persistence | `persistence.py` · `task_manager.py` |
| Daemon + dashboard | `daemon.py` · `agent_service.py` · `web/` |
| MCP | `mcp_client.py` · `mcp_adapter.py` · `mcp_presets.py` |
| Sandbox | `sandbox.py` |
| CLI | `cli.py` · `tui.py` |
| Evals & quality | `evals.py` · `eval_history.py` · `benchmark.py` · `vertical_evals.py` |

## End-of-session rules (always)

- Update `PROGRESS.md` and `feature_list.json`.
- Run the full verification block above.
- Remove temp/debug artifacts. Leave no half-finished work unrecorded.
- Ensure the standard startup path (`./install.sh` → `praxis demo`) still works.
- Commit safe work. Write a [session handoff](docs/harness/session-handoff.md) if work spans sessions.