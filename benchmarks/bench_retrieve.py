"""Micro-benchmark for RAG retrieval scaling.

Usage:
    python benchmarks/bench_retrieve.py [n_chunks] [n_queries]

Ingests ``n_chunks`` synthetic documents into an isolated store and times
``Rag.retrieve``. Reports whether the numpy-accelerated path is active. Not part
of the test suite; run manually to validate performance work.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time


def main() -> None:
    n_chunks = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    n_queries = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    home = tempfile.mkdtemp(prefix="praxis-bench-")
    os.environ["PRAXIS_HOME"] = home
    os.environ["PRAXIS_EMBED"] = "mock"

    from hybridagent import vecsim
    from hybridagent.embeddings import EmbeddingClient
    from hybridagent.persistence import Store
    from hybridagent.rag import Rag

    rag = Rag(Store.open(), EmbeddingClient(mode="mock"))

    print(f"numpy acceleration: {vecsim.HAVE_NUMPY}")
    print(f"ingesting {n_chunks} chunks...")
    t0 = time.perf_counter()
    for i in range(n_chunks):
        rag.ingest_text(
            f"document {i} about revenue, milestones, owners, and topic {i % 50}",
            source=f"doc-{i}.txt")
    print(f"  ingest: {time.perf_counter() - t0:.2f}s")

    queries = [f"revenue topic {i % 50} milestones" for i in range(n_queries)]
    t0 = time.perf_counter()
    for q in queries:
        rag.retrieve(q, k=5)
    elapsed = time.perf_counter() - t0
    print(f"retrieve x{n_queries} over {n_chunks} chunks: {elapsed:.3f}s "
          f"({elapsed / n_queries * 1000:.1f} ms/query)")


if __name__ == "__main__":
    main()
