"""RAG — chunk, embed, store, and retrieve document context.

Backed by the SQLite ``vectors`` table (``persistence.Store``) with pure-Python
cosine similarity, so retrieval works fully offline with the deterministic mock
embedder and no extra dependencies. Swap in a real embedding model (config
``agents.defaults.embedModel``) or a vector index (sqlite-vec / FAISS) without
changing callers.

Retrieved chunks carry ``source`` + ``provenance`` and are screened by the broker
when folded into perception, preserving the *data, never instruction* boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .embeddings import EmbeddingClient
from .ingest import ExtractedDoc, extract_text
from .logging_util import get_logger
from .vecsim import VectorIndex

_log = get_logger("praxis.rag")


@dataclass
class RetrievedChunk:
    text: str
    source: str
    score: float
    kind: str = "document"
    provenance: str = "document"


def reciprocal_rank_fusion(
    ranked_lists: "list[list]", *, k_const: int = 60,
) -> "list[tuple]":
    """Reciprocal Rank Fusion of several ranked id lists.

    Each list ranks the same ids by a different signal (e.g. embedding cosine and
    BM25). An id's fused score is ``sum(1 / (k_const + rank))`` across the lists in
    which it appears, so an item ranked highly by *either* signal rises, and items
    ranked highly by *both* rise most. Returns ``(id, score)`` pairs, best first,
    with ties broken deterministically by id.
    """
    scores: dict = {}
    for ranking in ranked_lists:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k_const + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _hybrid_enabled() -> bool:
    import os

    from . import config as cfg
    if os.environ.get("PRAXIS_HYBRID_RETRIEVAL", "").lower() in ("0", "false", "off"):
        return False
    return bool(cfg.load_config().get("agents", {}).get("hybridRetrieval", True))


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Paragraph-aware character chunking with a small inter-chunk overlap."""
    text = (text or "").strip()
    if not text:
        return []
    chunk_size = max(1, chunk_size)
    # Clamp overlap so the hard-split step stays positive (overlap >= chunk_size
    # would otherwise explode a long paragraph into one chunk per character).
    overlap = max(0, min(overlap, chunk_size // 2))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    base: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 1 <= chunk_size:
            cur = f"{cur}\n{para}".strip()
        else:
            if cur:
                base.append(cur)
            if len(para) <= chunk_size:
                cur = para
            else:                                   # hard-split an oversized para
                step = max(1, chunk_size - overlap)
                for i in range(0, len(para), step):
                    base.append(para[i:i + chunk_size])
                cur = ""
    if cur:
        base.append(cur)
    if overlap <= 0 or len(base) <= 1:
        return base
    out = [base[0]]
    for i in range(1, len(base)):
        tail = base[i - 1][-overlap:]
        out.append(f"{tail} {base[i]}".strip())
    return out


class Rag:
    def __init__(self, store, embedder: EmbeddingClient | None = None,
                 ns: str = "kb") -> None:
        self.store = store
        self.embed = embedder or EmbeddingClient()
        self.ns = ns
        # Per-namespace cached index: (version, metas, VectorIndex). Rebuilt
        # only when the store's vector_version for the namespace changes.
        self._index_cache: dict = {}
        self._bm25_cache: dict = {}  # ns -> (version, BM25Index), same invalidation

    def _get_bm25(self, ns: str, metas: list, index):
        version = self.store.vector_version(ns)
        cached = self._bm25_cache.get(ns)
        if cached is not None and cached[0] == version:
            return cached[1]
        from .bm25 import BM25Index
        # Only index chunks the vector index kept, so a chunk in an inconsistent
        # embedding state (dim mismatch) stays excluded from retrieval until
        # re-ingested — consistent with the embedding path.
        usable = set(index.kept_rows())
        bm = BM25Index.build((str(i), metas[i]["text"])
                             for i in range(len(metas)) if i in usable)
        self._bm25_cache[ns] = (version, bm)
        return bm

    def _get_index(self, ns: str):
        version = self.store.vector_version(ns)
        cached = self._index_cache.get(ns)
        if cached is not None and cached[0] == version:
            return cached[1], cached[2]
        metas, blobs = self.store.fetch_vectors(ns)
        index = VectorIndex(blobs, version=version)
        self._index_cache[ns] = (version, metas, index)
        return metas, index

    # ----------------------------------------------------------------- ingest
    def ingest_text(self, text: str, source: str, kind: str = "document",
                    provenance: str | None = None, ns: str | None = None,
                    chunk_size: int = 1000, overlap: int = 150) -> int:
        ns = ns or self.ns
        chunks = chunk_text(text, chunk_size, overlap)
        if not chunks:
            return 0
        vectors = self.embed.embed(chunks)
        prov = provenance or f"document:{source}"
        # Re-ingesting a doc replaces its old chunks (idempotent updates).
        self.store.delete_doc(ns, source)
        for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            self.store.add_vector(ns, source, i, chunk, prov, kind, vec)
        _log.info("ingested %s: %d chunks into ns=%s", source, len(chunks), ns)
        return len(chunks)

    def ingest_file(self, path, ns: str | None = None) -> tuple[ExtractedDoc, int]:
        from .multimodal import MediaClient, is_media
        if is_media(path):
            doc = MediaClient().process(path)
        else:
            doc = extract_text(path)
        n = self.ingest_text(
            doc.text, source=doc.source, kind=doc.kind,
            provenance=f"file:{doc.metadata.get('path', doc.source)}", ns=ns)
        return doc, n

    # --------------------------------------------------------------- retrieve
    def retrieve(self, query: str, k: int = 5, ns: str | None = None,
                 min_score: float = 0.0,
                 hybrid: bool | None = None) -> list[RetrievedChunk]:
        """Retrieve the top-``k`` chunks for ``query``.

        With ``hybrid`` (default on via ``agents.hybridRetrieval``), the embedding
        ranking is fused with a BM25 lexical ranking via Reciprocal Rank Fusion.
        Note: in hybrid mode ``min_score`` filters only the embedding candidate
        pool (BM25 matches are not cosine-thresholded) and ``RetrievedChunk.score``
        is the RRF score, not a cosine similarity.
        """
        ns = ns or self.ns
        if not query.strip() or self.store.count_vectors(ns) == 0:
            return []
        if hybrid is None:
            hybrid = _hybrid_enabled()
        qv = self.embed.embed_one(query)
        metas, index = self._get_index(ns)
        if index.skipped:
            _log.warning(
                "ns=%s: skipped %d chunk(s) embedded with a different model "
                "(dim != %d). Re-ingest after changing the embedding model.",
                ns, index.skipped, index.dim)
        pool = max(k * 5, 25)
        emb_hits = index.query(qv, k=pool, min_score=min_score)
        if not hybrid:
            chosen = [(h.index, h.score) for h in emb_hits[:k]]
        else:
            # Fuse the embedding ranking with a BM25 lexical ranking over the same
            # chunks, so exact/rare-term matches and semantic matches both surface
            # — and retrieval stays strong even with a weak/offline embedder.
            emb_rank = [h.index for h in emb_hits]
            bm25 = self._get_bm25(ns, metas, index)
            bm_rank = [int(doc_id) for doc_id, _ in bm25.search(query, k=pool)]
            chosen = reciprocal_rank_fusion([emb_rank, bm_rank])[:k]
        out: list[RetrievedChunk] = []
        for idx, score in chosen:
            row = metas[idx]
            out.append(RetrievedChunk(
                text=row["text"], source=row["doc_id"], score=float(score),
                kind=row["kind"], provenance=row["provenance"]))
        return out

    def retrieve_all_ns(self, query: str, k: int = 5,
                        hybrid: bool | None = None) -> list[RetrievedChunk]:
        """Retrieve across every namespace that holds indexed chunks, so a
        grounded answer can draw on *all* registered RAG repositories rather than
        only the default one. Results are merged and truncated to the top ``k``
        by score. Falls back to the default namespace if enumeration fails.
        """
        try:
            namespaces = self.store.list_namespaces()
        except Exception:
            namespaces = [self.ns]
        if not namespaces:
            namespaces = [self.ns]
        merged: list[RetrievedChunk] = []
        for ns in namespaces:
            try:
                merged.extend(self.retrieve(query, k=k, ns=ns, hybrid=hybrid))
            except Exception:
                continue
        merged.sort(key=lambda c: c.score, reverse=True)
        return merged[:k]

    def stats(self, ns: str | None = None) -> dict:
        ns = ns or self.ns
        return {"chunks": self.store.count_vectors(ns),
                "docs": len(self.store.doc_ids(ns)), "ns": ns}
