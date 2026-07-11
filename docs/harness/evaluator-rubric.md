# Evaluator Rubric

> Scorecard for reviewing agent-contributed PRs to Praxis. Use after a session or at milestones to evaluate whether the work meets the bar.
> Course ref: Learn Harness Engineering L11 — "Turn 'is it good' into quantifiable, reproducible scoring."

## How to use

1. After a session (or a set of sessions), score the agent's work across the six dimensions below.
2. Each dimension is scored 0–2.
3. Reach a conclusion: Accept / Revise / Block.
4. **The reviewer must be independent from the agent that did the work.** A model grading its own output is systematically over-generous (course L9: confidence calibration bias). Use a different session, ideally a different model, prompted to be *nitpicky*.

## The six dimensions (0–2 each)

### 1. Correctness
Does the implementation match the target behavior from `feature_list.json`?
- **2** — Behavior matches exactly; edge cases handled; matches the feature's `user_visible_behavior`.
- **1** — Main path works; some edge cases miss or behavior is close but not exact.
- **0** — Behavior doesn't match, or the feature's `verification` command fails.

### 2. Verification
Were the required checks actually run, with evidence?
- **2** — Full verification block from `AGENTS.md` run and green; evidence recorded in `PROGRESS.md` / `feature_list.json` `evidence` field.
- **1** — Some checks run (e.g. pytest but not evals, or evals but not mypy); evidence partial.
- **0** — "The code looks fine" with no executable verification; or checks run but results not recorded.

### 3. Scope discipline
Did the agent stay within the selected feature?
- **2** — Only the selected feature touched; no drive-by refactors; `feature_list.json` states accurate.
- **1** — Minor scope creep (a related fix or rename), documented in the PR.
- **0** — Multiple features started and none finished; WIP>1; unrequested refactors mixed in.
  - *Course L7: lines of code is weakly negatively correlated with feature completion. "Do less but finish" beats "do more but leave half-done."*

### 4. Reliability
Does the result survive a restart or re-run?
- **2** — Idempotent; `praxis demo` and the daemon restart cleanly; no flakiness in repeated runs.
- **1** — Works once; some state not persisted or a race on concurrent runs.
- **0** — Broken on restart, or relies on session memory not on disk (course L5: "the agent forgets; the repo doesn't").

### 5. Maintainability
Is the code and documentation clear enough for the next session?
- **2** — Reads cleanly; no commented-out code/TODOs; hard constraints respected (dependency-free core, governance spine, sandboxing); `PROGRESS.md` + relevant topic docs updated.
- **1** — Mostly clear; a few rough edges or a missing doc note.
- **0** — Unclear, brittle, or violates a hard constraint from `AGENTS.md`.

### 6. Handoff readiness
Can a new session continue using only repo artifacts?
- **2** — Fresh session test passes: a new session can answer what / how-to-run / how-to-verify / what's-unfinished / next-step from repo alone.
- **1** — Most questions answerable; one or two require asking a human.
- **0** — State lives in chat memory or a human's head, not in the repo.
  - *Course L3: knowledge not in the repo doesn't exist for the agent.*

## Conclusion

| Total (0–12) | Conclusion | Action |
|---|---|---|
| 10–12 | **Accept** | Merge; mark feature `passing` in `feature_list.json` with evidence. |
| 6–9 | **Revise** | Request specific fixes (cite the dimension + the evidence). Re-score after revision. |
| 0–5 | **Block** | Fundamental issues. Don't merge. Diagnose which harness layer failed and fix that first. |

## ⚠️ This rubric needs tuning

Out of the box, agents — and human reviewers under time pressure — identify real issues then talk themselves into approving. Course L11 documented this exact failure in Anthropic's evaluator: early versions would flag problems, then dismiss them as "not severe." **Plan for 3–5 tuning rounds:**

1. Run this rubric on a completed sprint (or a few agent PRs).
2. Compare its scores against your own human judgment, dimension by dimension.
3. Where they diverge, make the pass/fail criteria above more specific and concrete.
4. Re-run and check alignment.
5. Repeat until the rubric consistently matches human review.

Record each tuning round in `docs/harness/quality-document.md` under "Rubric tuning log" so you can track what improved alignment.

## Maker-checker rule (non-negotiable)

> **The agent that wrote the code must not be the agent that scores this rubric.**

- Claude Code's `/goal` uses an independent supervisor session to judge completion — not the session that did the work.
- Codex's sub-agent system lets you define a verifier agent with a different model.
- For Praxis PRs: the verifier should be a fresh session, ideally a different model, prompted to find reasons to *reject*. The author's job is to make it hard to reject; the verifier's job is to try.

If the same model/session both wrote and reviewed the PR, mark dimension 2 (Verification) and dimension 6 (Handoff readiness) down by at least 1 — self-review is not verification.

---
*Evaluation is a harness architecture property, not a feature you add after the fact. Make it reproducible, or don't trust it.*