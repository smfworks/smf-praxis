# H05 — Maker-Checker Separation: Design Doc

> Status: **draft** (2026-07-11). Not yet implemented. This doc lays out the tradeoffs for review before any code change to `hybridagent/verifier.py`.
> Related: [[Harness Engineering — Learn Harness Engineering Course]] (L9, L11), [[LLM-as-a-Verifier — General-Purpose Verification Framework]] (the SOTA implementation).
> Feature: `H05` in `feature_list.json`.

## The gap

Praxis already has `hybridagent/verifier.py` — an independent critic gate over a turn. But it's the **coarsest possible implementation**:

1. **Discrete verdict.** The optional `CriticFn` returns one of two strings: `APPROVE` or `REVISE: <reason>`. No score, no confidence, no way to rank candidates.
2. **Single criterion.** "Is this acceptable?" conflates specification-compliance, output-format, and error-freedom into one judgment. The verifier latches onto whichever factor is most salient.
3. **One evaluation.** No variance reduction. A single noisy pass decides.
4. **No best-of-N.** Praxis generates one trajectory per turn; there's no mechanism to sample N candidates and pick the best.
5. **No progress signal.** The verifier is a binary gate at turn end, not a continuous signal during the turn.

## What LLM-as-a-Verifier (arXiv:2607.05391) upgrades

The paper is the SOTA implementation of exactly the evaluator half of maker-checker. It's training-free, plug-and-play, MIT-licensed (`pip install llm-verifier`), and achieves SOTA on Terminal-Bench V2 (86.5%), SWE-Bench Verified (78.2%), RoboRewardBench (87.4%), MedAgentBench (73.3%).

| Today in Praxis | After the paper |
|---|---|
| Discrete `APPROVE`/`REVISE` string | Continuous reward via **expectation over 20 score-token logprobs** (A–T scale). Zero ties, better SNR. |
| Single criterion | **Criteria decomposition** (Specification / Output / Errors) — +3 points accuracy in the paper. |
| One evaluation | **K repeated evaluations** — variance shrinks O(1/K). |
| N/A | **Probabilistic Pivot Tournament** for best-of-N selection (O(Nk), ring pass cancels positional bias). |
| N/A | **`ProgressTracker`** — live per-step progress score, can stop hopeless rollouts early (fights the loop-engineering "cognitive surrender" cost). |
| Optional `CriticFn` | `llm-verifier` library — clean API, multimodal, caches directed comparisons. |

## The constraint that drives the design

> **Praxis principle (from AGENTS.md):** "Dependency-free core (`hybridagent` core deps `[]`); new modules stdlib-only."

`llm-verifier` pulls in `google-genai` / `openai` and requires a **logprob-exposing backend** (Gemini via Vertex AI, or a local vLLM/SGLang server). This is a hard architectural boundary:

- It **cannot** be a hard dependency of `hybridagent`.
- It **must** be an **optional extra** behind a flag, exactly like Praxis already gates its model router.
- Core `verifier.py` stays stdlib-only and deterministic-by-default.
- When the user has the backend configured, a richer critic path activates.

## Proposed design

### 1. Preserve the deterministic core

`AnswerVerifier`'s regex-based honesty checks (`_CLAIM_DONE`, non-evasive) stay. They are fast, offline-safe, and catch the most important class of failure (claiming a held/denied action completed). They run **first**, always, with no model call.

### 2. Add an optional `LLMVerifierCritic` path

A new module — **not** in `hybridagent/verifier.py` core, but alongside it (e.g. `hybridagent/verifier_llm.py` or behind a lazy import) — that implements the `CriticFn` protocol using `llm-verifier` when it's installed and a backend is configured.

```python
# Sketch — not committed, for review.
class LLMVerifierCritic:
    """CriticFn impl using llm-verifier's continuous reward.

    Optional dependency: `pip install llm-verifier` + VERTEX_API_KEY or
    OPENAI_BASE_URL pointing at a logprob-exposing server.
    """
    def __init__(self, criteria: dict, n_evaluations: int = 8,
                 threshold: float = 0.5):
        import llm_verifier  # lazy import — optional dep
        self._lv = llm_verifier
        self.criteria = criteria
        self.n_evaluations = n_evaluations
        self.threshold = threshold

    def __call__(self, task: str, answer: str) -> str:
        # Pairwise needs a reference; for single-answer gate, use track()
        # to score the answer as a 1-step trajectory and threshold the
        # final progress score. Or: compare against a "negative" template.
        # ...design choice to settle in the prototype...
        score = self._score(task, answer)
        if score >= self.threshold:
            return "APPROVE"
        return f"REVISE: verifier score {score:.2f} < {self.threshold}"
```

### 3. The two open design questions for the prototype

1. **Single-answer gating vs. best-of-N.** The paper's headline results are best-of-N selection (PPT). Praxis's current `VerifiedChatAgent` verifies a *single* answer. Two options:
   - **(a) Gate path** — use `llm_verifier.track()` / `ProgressTracker` to score the single answer as a trajectory, threshold the final score. Lower lift, fits the existing wrapper. Doesn't use PPT.
   - **(b) Selection path** — generate N candidate answers (requires plumbing parallel sampling through `GovernedChatAgent`), then `llm_verifier.select()` with PPT. Higher lift, uses the paper's SOTA results, needs a multi-candidate generation loop Praxis doesn't have yet.

   **Recommendation:** start with (a) — it upgrades the existing critic slot with continuous scoring and criteria decomposition, which is most of the win, without building a new generation loop. Leave (b) for H10 (loop engineering) when Praxis has a `/goal`-style loop.

2. **Backend selection.** Default to **Gemini 2.5 Flash via Vertex AI** (logprobs available, cheap, fast, the paper's default). Support **local vLLM/SGLang** serving Qwen3.5-9B as the offline path. For frontier models that hide logprobs (GPT-5.5, Opus 4.7), use the paper's two-stage workaround (closed model reasons → open verifier scores). Praxis's model router already has the config plumbing for this.

### 4. Config

```json
{
  "agents": {
    "verification": {
      "enabled": true,
      "maxRevisions": 1,
      "critic": "llm-verifier",
      "criteria": {"Spec": "Does the answer satisfy the task requirements?",
                    "Output": "Is the final format correct?",
                    "Errors": "Is it free of failure signals?"},
      "nEvaluations": 8,
      "threshold": 0.5,
      "model": "gemini-2.5-flash"
    }
  }
}
```

`critic: "deterministic"` (default, current behavior) or `critic: "llm-verifier"` (optional). `PRAXIS_VERIFY` env var already toggles verification on/off.

### 5. Tests

- **Unit (stdlib, no deps):** existing `verifier.py` tests stay green; the deterministic core is unchanged.
- **Integration (optional dep):** a `@pytest.mark.optional` test that skips when `llm-verifier` isn't installed or no backend is configured. Exercises `LLMVerifierCritic` against a fixture task and asserts the score is in [0,1] and the verdict is `APPROVE`/`REVISE`.
- **Eval:** add a `praxis eval` capability check (`verification_llm`) that reports whether the optional path is available. The existing `verification` eval stays.

## What this does NOT do (scope discipline)

- **Does not** add a multi-candidate generation loop to Praxis. That's H10 (loop engineering).
- **Does not** make `llm-verifier` a hard dependency. Core stays stdlib-only.
- **Does not** remove the deterministic checks. They run first, always.
- **Does not** change `GovernedChatAgent`'s governance spine. The verifier is still under the same READ/DRAFT/SEND/DESTRUCTIVE gates.

## Verification command (for feature_list.json)

```bash
python3 -c "import hybridagent.verifier" && \
grep -q 'independent verifier' AGENTS.md && \
grep -q 'maker-checker' docs/harness/evaluator-rubric.md && \
test -f docs/harness/h05-maker-checker-design.md && \
python3 -m pytest tests/test_verifier.py -q
```

Pass-state gating: the deterministic verifier tests stay green, the design doc exists, AGENTS.md and the rubric document the split, and (when the optional dep is installed) the integration test passes.

## Open questions for Michael

1. **Backend choice for SMF Works** — Gemini via Vertex (needs a Vertex API key, costs money, paper's default), or local vLLM/SGLang on the AMD box (Qwen3.5-9B, free, needs the GPU stack we discussed)? Or both, Vertex as default with local fallback?
2. **Threshold** — 0.5 is a starting guess. The paper shows separation is strong (SNR rises with G), but Praxis-specific calibration against real Praxis tasks (the way the harness course's evaluator rubric needs 3–5 tuning rounds) is the real work.
3. **Scope of H05** — is H05 just "document the split + optional critic path," or does it include wiring `ProgressTracker` into the autonomous loop too? The latter is arguably H10's territory.

## Next action (after sign-off)

1. Set `H05` to `in_progress` in `feature_list.json` (WIP=1).
2. Prototype `LLMVerifierCritic` behind the lazy import.
3. Add the `@pytest.mark.optional` integration test.
4. Wire the config key into `VerificationConfig.load`.
5. Calibrate threshold against 5–10 real Praxis tasks (record the score vs. human judgment, iterate).
6. Document the split in `AGENTS.md` and `evaluator-rubric.md`.
7. Set `H05` to `passing` with evidence.