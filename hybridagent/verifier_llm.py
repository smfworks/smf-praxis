"""Optional LLM-verifier critic backend for ``AnswerVerifier``.

This module upgrades the discrete ``APPROVE``/``REVISE`` critic slot in
:mod:`hybridagent.verifier` to a continuous-reward verifier when an optional
logprob-exposing backend is configured. It is an **optional extra**:
``hybridagent`` core stays dependency-free (``deps = []``), and this module
lazy-imports ``llm-verifier`` only when the operator opts in via
``agents.verification.critic: "llm-verifier"`` in ``praxis.json``.

Source: arXiv:2607.05391 (LLM-as-a-Verifier, Kwok et al. 2026).
Repo: github.com/llm-as-a-verifier/llm-verifier (MIT, ``pip install
llm-verifier``).

The gate path (used here) scores the agent's single answer as a one-step
trajectory via ``llm_verifier.track`` and thresholds the final progress
score. This is most of the win from the paper — continuous rewards via
expectation over score-token logprobs, criteria decomposition, and K
repeated evaluations — without requiring a multi-candidate generation loop.
The selection path (Probabilistic Pivot Tournament, best-of-N) is deferred
to H10 (loop engineering) when Praxis has a ``/goal``-style loop.

The deterministic checks in :class:`AnswerVerifier` (regex honesty,
non-evasive) run FIRST and always; this critic only runs when those pass
and ``self.critic is not None``. So enabling this path never weakens the
offline-safe baseline.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# CriticFn protocol — matches hybridagent.verifier.CriticFn.
# (task, answer) -> "APPROVE" or "REVISE: <reason>"
# ---------------------------------------------------------------------------

CriticFn = Callable[[str, str], str]


class MissingVerifierBackendError(RuntimeError):
    """No ``llm-verifier`` backend available.

    Raised only when the operator has opted in (``critic: "llm-verifier"``)
    but the library is not installed or no logprob-exposing backend is
    configured. Install with ``pip install llm-verifier`` and set either
    ``VERTEX_API_KEY`` (Gemini via Vertex AI) or ``OPENAI_BASE_URL`` (a
    vLLM / SGLang / OpenAI-compatible server that returns token-level
    logprobs).
    """


# ---------------------------------------------------------------------------
# Default criteria — decompose "is this acceptable?" into three easier
# sub-judgments, following the paper's criteria-decomposition axis. The
# caller can override per-task in praxis.json.
# ---------------------------------------------------------------------------

DEFAULT_CRITERIA: dict[str, str] = {
    "Specification": "Does the answer satisfy all explicit task requirements?",
    "Output": "Is the final output format and content what the task asked for?",
    "Errors": "Is the answer free of failure signals — unverified claims, "
              "broken reasoning, or side effects that did not actually execute?",
}


@dataclass
class LLMVerifierConfig:
    """Operator-selected LLM-verifier settings (``agents.verification`` in
    ``praxis.json``). All fields optional; sensible defaults applied.
    """
    enabled: bool = False
    criteria: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CRITERIA))
    n_evaluations: int = 8          # K — repeated evaluations (variance reduction)
    threshold: float = 0.3          # calibrated 2026-07-11 on Qwen2.5-7B-Instruct
    # Q3_K_M (CPU): 9/10 tasks cleanly separated; min_correct=0.46,
    # max_wrong=0.13 -> threshold 0.3 gives zero false-rejects, zero
    # false-approvals on the calibration set. Known failure: single-token
    # number answers on trivial questions (model scores both 3 and 4 as
    # 1.0). Recalibrate when swapping the verifier model.
    # model: llm-verifier auto-detects the served model when
    # OPENAI_BASE_URL is set (vLLM/SGLang serve exactly one). For Gemini via
    # Vertex, default to the paper's verifier.
    model: str = "gemini-2.5-flash"

    @classmethod
    def from_verification_dict(cls, v: dict[str, Any]) -> "LLMVerifierConfig":
        """Build from the ``agents.verification`` sub-dict of praxis.json.

        ``critic`` selects the backend: ``"llm-verifier"`` opts in; anything
        else (or absent) keeps the deterministic-only default. The other
        keys are passed through with clamping.
        """
        critic = str(v.get("critic", "") or "").lower()
        enabled = critic in ("llm-verifier", "llm_verifier", "llmverifier")
        raw_crit = v.get("criteria") or {}
        criteria = {str(k): str(val) for k, val in raw_crit.items()} or dict(DEFAULT_CRITERIA)
        try:
            n_eval = max(1, min(64, int(v.get("nEvaluations", 8) or 8)))
        except (TypeError, ValueError):
            n_eval = 8
        try:
            threshold = float(v.get("threshold", 0.5) or 0.5)
        except (TypeError, ValueError):
            threshold = 0.5
        threshold = max(0.0, min(1.0, threshold))
        model = str(v.get("model", "gemini-2.5-flash") or "gemini-2.5-flash")
        return cls(enabled=enabled, criteria=criteria, n_evaluations=n_eval,
                   threshold=threshold, model=model)


# ---------------------------------------------------------------------------
# LLMVerifierCritic — the CriticFn implementation.
# ---------------------------------------------------------------------------

class LLMVerifierCritic:
    """A ``CriticFn`` backed by ``llm-verifier``'s continuous reward.

    Lazy-imports ``llm_verifier`` on first call so the core package never
    depends on it. Raises :class:`MissingVerifierBackendError` at
    construction only if the library is not installed — backend credential
    errors surface on the first scoring call instead, so a misconfigured
    verifier never blocks the deterministic path silently.

    The gate path: score the agent's single answer as a one-step trajectory
    via ``llm_verifier.track`` (which internally takes the expectation over
    the verifier's score-token logprob distribution, across C criteria and K
    repeated evaluations), then threshold the final progress score in
    ``[0, 1]``. Scores below ``threshold`` return
    ``"REVISE: verifier score <s> < <t>"`` with the concrete number, so the
    agent sees the gap rather than a bare rejection.
    """

    def __init__(self, config: LLMVerifierConfig) -> None:
        self.config = config
        # Lazy import — optional dependency. Failure here means the operator
        # opted in but never installed the library; surface it immediately.
        try:
            import llm_verifier  # noqa: F401  (import check only)
        except ImportError as e:
            raise MissingVerifierBackendError(
                "agents.verification.critic is 'llm-verifier' but the "
                "llm-verifier package is not installed. Install with "
                "`pip install llm-verifier` and set VERTEX_API_KEY or "
                "OPENAI_BASE_URL to a logprob-exposing backend.") from e
        self._llm_verifier = llm_verifier

    def _score(self, task: str, answer: str) -> float:
        """Score one (task, answer) pair, returning a continuous reward in
        ``[0, 1]``. Wraps the per-call exceptions so a transient backend
        error never fabricates a verdict — it surfaces as a runtime error
        that the caller (AnswerVerifier) catches and treats as APPROVE,
        matching the existing critic-slot contract.

        Note: ``llm_verifier.track()`` uses a built-in skeptical progress
        prompt ("would the agent's CURRENT state satisfy the task's hidden
        grader?") and does NOT accept custom criteria — criteria
        decomposition is a ``select()``/``compare()`` feature. So in the
        gate path, ``self.config.criteria`` is not used here; it is
        preserved on the config for the future selection path (H10) and
        for documentation. The progress prompt's built-in calibration
        rules ("trust observed output, not narration"; "effort is NOT
        progress") already encode the anti-premature-victory behavior we
        want from the harness-engineering course (L9).
        """
        result = self._llm_verifier.track(
            problem=task,
            steps=[answer],
            n_evaluations=self.config.n_evaluations,
            model=self.config.model,
        )
        # track() returns ProgressResult.scores — the progress curve. For a
        # one-step trajectory there is exactly one checkpoint; .final is
        # that score in [0, 1].
        return float(result.final)

    def __call__(self, task: str, answer: str) -> str:
        """CriticFn protocol: return ``"APPROVE"`` or ``"REVISE: <reason>"``.

        A failed backend call raises (the existing ``AnswerVerifier.verify``
        wraps the critic call in try/except and treats an exception as
        APPROVE, so a broken verifier never blocks a clean answer — see
        verifier.py line ~88). We deliberately do NOT swallow the error
        here: the operator should see the traceback once, then fix the
        backend, rather than have verification silently degrade to
        deterministic-only.
        """
        score = self._score(task, answer)
        if score >= self.config.threshold:
            return "APPROVE"
        return (f"REVISE: llm-verifier score {score:.3f} is below the "
                f"acceptance threshold {self.config.threshold:.3f} "
                f"(criteria={list(self.config.criteria)}, "
                f"K={self.config.n_evaluations}, model={self.config.model})")


# ---------------------------------------------------------------------------
# Factory — called from VerificationConfig.load when critic == "llm-verifier".
# ---------------------------------------------------------------------------

def build_llm_verifier_critic(v: dict[str, Any]) -> LLMVerifierCritic | None:
    """Construct an :class:`LLMVerifierCritic` if the operator opted in,
    else return ``None`` (deterministic-only path). Raises
    :class:`MissingVerifierBackendError` if opted in but the library is
    missing — the caller decides whether to fall back to deterministic-only
    or fail loud.
    """
    config = LLMVerifierConfig.from_verification_dict(v)
    if not config.enabled:
        return None
    return LLMVerifierCritic(config)