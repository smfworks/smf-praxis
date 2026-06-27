"""Vector similarity index — numpy-accelerated with a pure-Python fallback.

``rag.retrieve`` originally (a) reloaded every vector from SQLite, (b)
deserialized each blob into a Python ``list[float]``, and (c) scored each row
with a pure-Python cosine loop — on *every* query. That is O(n*d) Python work
per query and dominates latency at scale.

This module builds a **cached, pre-normalized index** straight from the raw
embedding bytes (zero per-row Python float allocation when numpy is present), so
a query against a static knowledge base is one matrix-vector product. The index
is rebuilt only when the namespace's vector version changes (on ingest/delete).

With numpy absent it falls back to a pure-Python index so the core stays
dependency-free.
"""
from __future__ import annotations

import array
import math
from dataclasses import dataclass

try:  # optional acceleration
    import numpy as _np
    HAVE_NUMPY = True
except Exception:  # pragma: no cover - exercised only when numpy is absent
    _np = None  # type: ignore[assignment]
    HAVE_NUMPY = False


@dataclass
class Scored:
    index: int
    score: float


class VectorIndex:
    """Pre-normalized vectors for fast cosine top-k.

    Built from raw little-endian float32 blobs (as stored in the ``vectors``
    table). Rows whose byte length doesn't match the modal dimension are
    dropped from the index and counted in :attr:`skipped`.
    """

    def __init__(self, blobs: list[bytes], version: int = 0) -> None:
        self.version = version
        self.dim = 0
        self.skipped = 0
        self._row_index: list[int] = []   # index-row -> original row position
        self._matrix = None
        self._rows: list[list[float]] = []
        if not blobs:
            return
        lengths = [len(b) // 4 for b in blobs]
        self.dim = max(set(lengths), key=lengths.count)
        kept = [(i, b) for i, b in enumerate(blobs) if len(b) // 4 == self.dim]
        self.skipped = len(blobs) - len(kept)
        self._row_index = [i for i, _ in kept]

        if HAVE_NUMPY:
            buf = b"".join(b for _, b in kept)
            mat = _np.frombuffer(buf, dtype=_np.float32).reshape(len(kept), self.dim)
            norms = _np.linalg.norm(mat, axis=1)
            safe = _np.where(norms > 0, norms, 1.0)
            self._matrix = (mat / safe[:, None]).astype(_np.float32)
        else:
            for _, b in kept:
                vec = array.array("f")
                vec.frombytes(b)
                row = list(vec)
                n = math.sqrt(sum(x * x for x in row)) or 1.0
                self._rows.append([x / n for x in row])

    def __len__(self) -> int:
        return len(self._row_index)

    def kept_rows(self) -> list[int]:
        """Original row positions that survived the dim-consistency filter."""
        return list(self._row_index)

    def query(self, qv: list[float], k: int = 5,
              min_score: float = 0.0) -> list[Scored]:
        if not self._row_index or not qv or len(qv) != self.dim:
            return []
        if HAVE_NUMPY and self._matrix is not None:
            q = _np.asarray(qv, dtype=_np.float32)
            qn = _np.linalg.norm(q)
            if qn == 0:
                return []
            scores = self._matrix @ (q / qn)
            order = _np.argsort(-scores)
            out: list[Scored] = []
            for j in order:
                s = float(scores[int(j)])
                if s <= min_score:
                    continue
                out.append(Scored(index=self._row_index[int(j)], score=s))
                if len(out) >= k:
                    break
            return out
        # Pure-Python: query dotted against pre-normalized rows.
        qnorm_val = math.sqrt(sum(x * x for x in qv)) or 1.0
        qnorm = [x / qnorm_val for x in qv]
        scored = []
        for ri, row in enumerate(self._rows):
            s = sum(a * b for a, b in zip(row, qnorm, strict=False))
            if s > min_score:
                scored.append(Scored(index=self._row_index[ri], score=s))
        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:k]
