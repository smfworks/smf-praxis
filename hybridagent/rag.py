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
                 min_score: float = 0.0) -> list[RetrievedChunk]:
        ns = ns or self.ns
        if not query.strip() or self.store.count_vectors(ns) == 0:
            return []
        qv = self.embed.embed_one(query)
        metas, index = self._get_index(ns)
        if index.skipped:
            _log.warning(
                "ns=%s: skipped %d chunk(s) embedded with a different model "
                "(dim != %d). Re-ingest after changing the embedding model.",
                ns, index.skipped, index.dim)
        out: list[RetrievedChunk] = []
        for hit in index.query(qv, k=k, min_score=min_score):
            row = metas[hit.index]
            out.append(RetrievedChunk(
                text=row["text"], source=row["doc_id"], score=hit.score,
                kind=row["kind"], provenance=row["provenance"]))
        return out

    def stats(self, ns: str | None = None) -> dict:
        ns = ns or self.ns
        return {"chunks": self.store.count_vectors(ns),
                "docs": len(self.store.doc_ids(ns)), "ns": ns}
