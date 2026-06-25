"""Contradiction detection across retrieved knowledge.

Two retrieved chunks (KB, wiki, or memory) "contradict" when they share a topic
but contain opposite polarity claims about the same subject. This is a coarse
heuristic — it looks for shared salient nouns/numbers and flags pairs where one
chunk negates a claim the other asserts (or where numeric assertions disagree
by more than a tolerance).

It is deliberately *cheap and local*: real semantic contradiction detection
needs an NLI model, but a regulated deployment still wants *something* between
"silently overwrite the old wiki page" and "send conflicting answer to the
user." Findings are surfaced as warnings, not blocking errors.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NEG_TOKENS = {
    "not", "no", "never", "cannot", "can't", "won't", "didn't", "isn't",
    "wasn't", "weren't", "shouldn't", "without",
}
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


@dataclass
class Contradiction:
    a_source: str
    b_source: str
    score: float
    explanation: str


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text or ""))


def _has_negation(tokens: set[str]) -> bool:
    return bool(tokens & _NEG_TOKENS)


def detect(chunks: list, min_overlap: int = 3,
           numeric_tolerance: float = 0.0) -> list[Contradiction]:
    """Pairwise contradiction scan over retrieved chunks.

    ``chunks`` is a sequence of objects with ``text`` and ``source`` attributes
    (e.g. ``RetrievedChunk`` from :mod:`hybridagent.rag`)."""
    out: list[Contradiction] = []
    if not chunks or len(chunks) < 2:
        return out
    enriched = []
    for c in chunks:
        text = getattr(c, "text", "") or ""
        toks = _tokens(text)
        enriched.append((c, toks, _has_negation(toks), _numbers(text)))
    for i in range(len(enriched)):
        ci, ti, neg_i, ni = enriched[i]
        for j in range(i + 1, len(enriched)):
            cj, tj, neg_j, nj = enriched[j]
            overlap = (ti & tj) - _NEG_TOKENS
            shared = len(overlap)
            if shared < min_overlap:
                continue
            # Polarity flip: one asserts, the other negates with same vocabulary.
            if neg_i != neg_j:
                out.append(Contradiction(
                    a_source=getattr(ci, "source", "?"),
                    b_source=getattr(cj, "source", "?"),
                    score=shared / max(1, len(ti | tj)),
                    explanation=("polarity differs; shared terms: "
                                 + ", ".join(sorted(overlap))[:160]),
                ))
                continue
            # Numeric disagreement on the same topic.
            if ni and nj:
                disagree = ni.symmetric_difference(nj)
                if disagree:
                    # Tolerance: ignore numbers within ±tolerance ratio.
                    if numeric_tolerance > 0.0:
                        floats_i = {float(x) for x in ni}
                        floats_j = {float(x) for x in nj}
                        within_tol = all(
                            any(abs(a - b) / max(1.0, abs(a)) <= numeric_tolerance
                                for b in floats_j) for a in floats_i)
                        if within_tol:
                            continue
                    out.append(Contradiction(
                        a_source=getattr(ci, "source", "?"),
                        b_source=getattr(cj, "source", "?"),
                        score=shared / max(1, len(ti | tj)),
                        explanation=("numeric values disagree: "
                                     + ", ".join(sorted(disagree))[:160]),
                    ))
    return out


def render(contradictions: list[Contradiction]) -> str:
    if not contradictions:
        return "no contradictions detected"
    lines = [f"{len(contradictions)} contradiction(s) detected:"]
    for c in contradictions:
        lines.append(f"  [{c.score:.2f}] {c.a_source} <-> {c.b_source}: "
                     f"{c.explanation}")
    return "\n".join(lines)
