"""Okapi BM25 lexical ranking — pure-stdlib, dependency-free.

This powers always-available retrieval that needs *no embedding model*: ranking
the agent's own memory (durable + episodic) by relevance to a query so the most
pertinent past facts, decisions, and notes can surface into context. It
complements the embedding-based RAG (:mod:`hybridagent.rag`) — BM25 works
offline with zero configuration and is strong at exact-term and rare-term
matching.

Implementation notes:

* **Lucene-style IDF** — ``log(1 + (N - df + 0.5)/(df + 0.5))`` — is always
  non-negative, so the ranker behaves sensibly on the tiny corpora a single
  agent's memory produces (classic Okapi IDF goes negative when a term appears
  in more than half the documents, which would wrongly zero out good matches).
* The index is cheap to build per query over a small in-memory pool, so callers
  don't have to maintain or invalidate a persistent structure.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens."""
    return _TOKEN_RE.findall((text or "").lower())


@dataclass
class BM25Index:
    """An in-memory BM25 index over ``(doc_id, text)`` documents."""

    k1: float = 1.5
    b: float = 0.75
    doc_ids: list[str] = field(default_factory=list)
    _tf: list[Counter] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _len: list[int] = field(default_factory=list)

    def add(self, doc_id: str, text: str) -> "BM25Index":
        tokens = tokenize(text)
        counts = Counter(tokens)
        self.doc_ids.append(doc_id)
        self._tf.append(counts)
        self._len.append(len(tokens))
        for term in counts:
            self._df[term] += 1
        return self

    @property
    def n(self) -> int:
        return len(self.doc_ids)

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        """Return up to ``k`` ``(doc_id, score)`` pairs, highest score first.

        Docs sharing no query term score 0 and are omitted. Ties break by
        insertion order so results are deterministic.
        """
        if not self.n:
            return []
        avgdl = sum(self._len) / self.n or 1.0
        q_terms = [t for t in tokenize(query) if t in self._df]
        if not q_terms:
            return []
        scored: list[tuple[float, int, str]] = []
        for i, doc_id in enumerate(self.doc_ids):
            tf = self._tf[i]
            dl = self._len[i] or 1
            score = 0.0
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / avgdl)
                score += self._idf(term) * (f * (self.k1 + 1)) / denom
            if score > 0.0:
                scored.append((score, i, doc_id))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [(doc_id, score) for score, _i, doc_id in scored[:k]]

    @classmethod
    def build(cls, docs, **kw) -> "BM25Index":
        """Build an index from an iterable of ``(doc_id, text)`` pairs."""
        index = cls(**kw)
        for doc_id, text in docs:
            index.add(doc_id, text)
        return index
