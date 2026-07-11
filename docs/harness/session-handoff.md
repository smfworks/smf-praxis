# Session Handoff

> Compact handoff between sessions. Fill out at session end so the next session picks up quickly.
> Course ref: Learn Harness Engineering — "Weak handoff → End with an explicit handoff."

## Currently verified

- **Baseline:** [paste the verification block result from PROGRESS.md]
- **What's confirmed working:** [list features in `passing` state from feature_list.json]
- **Verification run this session:** [which commands you actually executed]

## Changes this session

- [files added/modified, with one-line why]
- [commits made: `<sha> <message>`]

## Still broken or unverified

- [known issues, risky areas, flaky tests]
- [anything left `in_progress` and why]
- [anything you suspect but didn't confirm]

## Next best action

- **Do:** [the next highest-priority `not_started` feature from feature_list.json]
- **Don't touch:** [areas that are fragile or that the next step doesn't need]

## Commands (quick reference)

```bash
./install.sh
python3 -m pytest --ignore=tests/test_fuzz_parsers.py -q
python3 -m hybridagent.cli eval
python3 -m ruff check hybridagent/
python3 -m mypy hybridagent --ignore-missing-imports
python3 -m hybridagent.cli demo
```

---
*Template — replace bracketed text each session. Keep it compact; this is a handoff, not a report.*