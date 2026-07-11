# Clean-State Checklist

> Run through before ending **every** session. Clean state is a *necessary condition* for session completion, not optional housekeeping.
> Course ref: Learn Harness Engineering L12 — "Clean state = 5 conditions: build, tests, progress, artifacts, startup."

## The five dimensions (all must pass)

- [ ] **Build passes.** `python3 -m build` produces a wheel (or `pip install -e .` succeeds).
- [ ] **Tests pass.** `python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q` — green, no new failures vs. baseline in PROGRESS.md.
- [ ] **Evals pass.** `python3 -m hybridagent.cli eval` — 40/40.
- [ ] **Lint + types clean.** `python3 -m ruff check hybridagent/` and `python3 -m mypy hybridagent --ignore-missing-imports` both clean.
- [ ] **Progress recorded.** `PROGRESS.md` updated with this session's work; `feature_list.json` reflects actual state (no false `passing`).
- [ ] **No stale artifacts.** No debug `print`/`console.log`/`debugger`/TODO markers left in committed code. No temp files (`/tmp/debug-*.log`, `.coverage` is gitignored). Commented-out code removed.
- [ ] **Standard startup path available.** `./install.sh` → `praxis demo` works from a clean checkout. Next session can start working without manual fixes.
- [ ] **Version bumped (if shipping).** `pyproject.toml` and `hybridagent/__init__.py` `__version__` match and are bumped if behavior changed.

## Session integrity (DB-transaction analogy)

Either fully commit a clean state, or roll back to the last consistent state. No middle ground.

- If you can't make the baseline green before the session ends: `git stash` your work-in-progress, leave a note in `PROGRESS.md` under "Known Issues," and commit nothing that breaks the baseline.
- If a feature is `in_progress` but not verified, it stays `in_progress` — do not mark it `passing`.

## If something fails

- **Tests red:** fix before exit, or revert the change that broke them. Don't leave red tests for the next session.
- **Lint/types red:** fix the specific violation. Don't add `# type: ignore` or `# noqa` to make it go away unless you document why in the commit message.
- **Startup broken:** this is the highest priority — a session that leaves `praxis demo` broken has failed the clean-state check regardless of what else it accomplished.

## Idempotent cleanup (safe to run repeatedly)

```bash
rm -f /tmp/praxis-debug-*.log          # -f: no error if missing
git checkout -- .env.local 2>/dev/null  # restore known state, if present
python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q   # verify cleanup didn't break anything
```

## End-of-session mirror (from coding-agent startup flow)

1. Record progress in `PROGRESS.md`.
2. Update `feature_list.json` states.
3. Write a `session-handoff.md` if work spans sessions.
4. Commit safe work.
5. Leave a clean restart path.

---
*Clean up later means never clean up. Entropy is the default state; only active cleanup counteracts it.*