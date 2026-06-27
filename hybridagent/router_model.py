"""Learned goal -> role router — a tiny, transparent, dependency-free model.

The orchestrator's :class:`~hybridagent.orchestrator.PredictiveRouter` ships with
a keyword heuristic and a standing note that it is "ready to be replaced by
learned outcome statistics as subagent run data accumulates." This module is
that replacement: a multinomial Naive-Bayes classifier over goal tokens, trained
**only** on the outcomes the governance spine already records in
``subagent_runs`` (which role successfully handled which goal). It is
deterministic, serialises to plain JSON, and needs no third-party libraries.

Design choices that matter for a *governed* router:

* **Transparent, not a black box.** Multinomial NB is inspectable: every routing
  decision reduces to per-token log-probabilities you can print and audit. There
  is no opaque weight matrix and no stochastic training loop to reproduce.
* **Confidence-gated.** :meth:`RouterModel.confident` returns ``None`` unless the
  winning class clears a probability threshold, so a weak or ambiguous goal falls
  back to the heuristic instead of guessing.
* **Fails safe.** Trained from too little data (one role, too few samples) the
  trainer returns ``None`` and the caller keeps the heuristic. A learned label is
  always re-validated against the known role set by the caller, so a stale model
  can never route work to a role that no longer exists.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens. Deterministic, language-agnostic."""
    return _TOKEN_RE.findall((text or "").lower())


@dataclass
class RouterModel:
    """A serialisable multinomial Naive-Bayes goal->role classifier."""

    classes: list[str] = field(default_factory=list)
    vocab: list[str] = field(default_factory=list)
    log_prior: dict[str, float] = field(default_factory=dict)
    log_prob: dict[str, dict[str, float]] = field(default_factory=dict)
    log_unk: dict[str, float] = field(default_factory=dict)
    n_samples: int = 0

    # ------------------------------------------------------------------ training
    @classmethod
    def train(cls, samples: list[tuple[str, str]]) -> "RouterModel":
        """Fit from ``(goal, role)`` pairs with add-1 (Laplace) smoothing."""
        classes = sorted({role for _, role in samples})
        vocab = sorted({tok for goal, _ in samples for tok in tokenize(goal)})
        vsize = len(vocab)
        tok_counts: dict[str, dict[str, int]] = {c: {} for c in classes}
        tot_tokens: dict[str, int] = {c: 0 for c in classes}
        doc_counts: dict[str, int] = {c: 0 for c in classes}
        for goal, role in samples:
            doc_counts[role] += 1
            for tok in tokenize(goal):
                tok_counts[role][tok] = tok_counts[role].get(tok, 0) + 1
                tot_tokens[role] += 1
        n = len(samples)
        log_prior = {c: math.log(doc_counts[c] / n) for c in classes}
        log_prob: dict[str, dict[str, float]] = {}
        log_unk: dict[str, float] = {}
        for c in classes:
            denom = tot_tokens[c] + vsize  # +V for Laplace smoothing
            log_prob[c] = {
                tok: math.log((tok_counts[c].get(tok, 0) + 1) / denom)
                for tok in vocab
            }
            # Probability mass reserved for a token never seen in this class.
            log_unk[c] = math.log(1 / denom)
        return cls(classes=classes, vocab=vocab, log_prior=log_prior,
                   log_prob=log_prob, log_unk=log_unk, n_samples=n)

    # ---------------------------------------------------------------- inference
    def _scores(self, goal: str) -> dict[str, float]:
        toks = tokenize(goal)
        scores: dict[str, float] = {}
        for c in self.classes:
            s = self.log_prior.get(c, 0.0)
            probs = self.log_prob.get(c, {})
            unk = self.log_unk.get(c, 0.0)
            for tok in toks:
                s += probs.get(tok, unk)
            scores[c] = s
        return scores

    def predict(self, goal: str) -> tuple[str | None, float]:
        """Return ``(role, confidence)`` where confidence is the posterior of the
        winning class (softmax over class log-scores). ``(None, 0.0)`` if untrained."""
        if not self.classes:
            return None, 0.0
        scores = self._scores(goal)
        top = max(scores.values())  # subtract max for a numerically stable softmax
        exps = {c: math.exp(s - top) for c, s in scores.items()}
        z = sum(exps.values()) or 1.0
        # Deterministic argmax: highest posterior, ties broken by class order.
        best = max(self.classes, key=lambda c: (exps[c], -self.classes.index(c)))
        return best, exps[best] / z

    def confident(self, goal: str, threshold: float = 0.60) -> str | None:
        """The predicted role iff its posterior clears ``threshold``, else ``None``."""
        role, conf = self.predict(goal)
        return role if role is not None and conf >= threshold else None

    # -------------------------------------------------------------- (de)serialise
    def to_json(self) -> str:
        return json.dumps({
            "classes": self.classes, "vocab": self.vocab,
            "log_prior": self.log_prior, "log_prob": self.log_prob,
            "log_unk": self.log_unk, "n_samples": self.n_samples,
        })

    @classmethod
    def from_json(cls, blob: str) -> "RouterModel":
        d = json.loads(blob)
        return cls(
            classes=list(d.get("classes", [])),
            vocab=list(d.get("vocab", [])),
            log_prior=dict(d.get("log_prior", {})),
            log_prob={k: dict(v) for k, v in d.get("log_prob", {}).items()},
            log_unk=dict(d.get("log_unk", {})),
            n_samples=int(d.get("n_samples", 0)),
        )


def samples_from_runs(
    runs: list[dict],
    *,
    success_states: tuple[str, ...] = ("completed", "waiting_approval"),
) -> list[tuple[str, str]]:
    """Extract ``(goal, role)`` training pairs from persisted subagent runs.

    Only *successful* runs are positive evidence that a role suited a goal;
    ``failed`` runs are dropped so a role that errored on a goal is never learned
    as the right home for it.
    """
    out: list[tuple[str, str]] = []
    for r in runs:
        goal = (r.get("goal") or "").strip()
        role = (r.get("role") or "").strip()
        if goal and role and r.get("status") in success_states:
            out.append((goal, role))
    return out


def train_from_runs(runs: list[dict], *, min_samples: int = 8,
                    min_classes: int = 2) -> "RouterModel | None":
    """Train from run history, or ``None`` when there is too little signal.

    Returning ``None`` is the safe default: the caller keeps the keyword
    heuristic until enough governed outcomes have accumulated to learn from.
    """
    samples = samples_from_runs(runs)
    if len(samples) < min_samples:
        return None
    if len({role for _, role in samples}) < min_classes:
        return None
    return RouterModel.train(samples)
