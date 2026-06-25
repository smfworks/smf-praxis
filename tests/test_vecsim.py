"""Tests for the cached vector index, both numpy and pure-Python paths."""
import array

import pytest

from hybridagent import config as cfg
from hybridagent import vecsim
from hybridagent.embeddings import EmbeddingClient
from hybridagent.persistence import Store
from hybridagent.rag import Rag
from hybridagent.vecsim import VectorIndex


def _blob(vec):
    return array.array("f", vec).tobytes()


def _build(blobs, monkeypatch, use_numpy):
    monkeypatch.setattr(vecsim, "HAVE_NUMPY", use_numpy)
    return VectorIndex(blobs, version=1)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_vector_index_ranks_nearest_first(monkeypatch, use_numpy):
    if use_numpy and not vecsim.HAVE_NUMPY:
        pytest.skip("numpy not installed")
    blobs = [
        _blob([1.0, 0.0, 0.0]),   # row 0
        _blob([0.0, 1.0, 0.0]),   # row 1
        _blob([0.9, 0.1, 0.0]),   # row 2 (close to query)
    ]
    idx = _build(blobs, monkeypatch, use_numpy)
    hits = idx.query([1.0, 0.0, 0.0], k=2)
    assert hits[0].index == 0
    assert hits[1].index == 2
    assert hits[0].score > hits[1].score


@pytest.mark.parametrize("use_numpy", [True, False])
def test_vector_index_skips_dim_mismatch(monkeypatch, use_numpy):
    if use_numpy and not vecsim.HAVE_NUMPY:
        pytest.skip("numpy not installed")
    blobs = [_blob([1.0, 0.0, 0.0]), _blob([1.0, 0.0, 0.0]),
             _blob([1.0, 0.0])]   # different dim (modal is 3)
    idx = _build(blobs, monkeypatch, use_numpy)
    assert idx.dim == 3
    assert idx.skipped == 1
    assert len(idx) == 2


def test_empty_index_returns_nothing(monkeypatch):
    idx = VectorIndex([], version=0)
    assert idx.query([1.0, 2.0]) == []


def test_query_dim_mismatch_returns_nothing(monkeypatch):
    idx = VectorIndex([_blob([1.0, 0.0, 0.0])], version=1)
    assert idx.query([1.0, 0.0]) == []   # query dim != index dim


def test_rag_index_cache_invalidates_on_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    store = Store.open()
    rag = Rag(store, EmbeddingClient(mode="mock"))
    rag.ingest_text("alpha beta gamma revenue", source="a.txt")
    v1 = store.vector_version("kb")
    assert rag.retrieve("alpha revenue")[0].source == "a.txt"
    # Build cache, then ingest a more relevant doc; cache must invalidate.
    rag.ingest_text("alpha beta gamma revenue delta epsilon", source="b.txt")
    assert store.vector_version("kb") > v1
    hits = rag.retrieve("alpha beta gamma revenue delta epsilon")
    assert hits[0].source == "b.txt"


def test_rag_cache_reused_when_version_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    store = Store.open()
    rag = Rag(store, EmbeddingClient(mode="mock"))
    rag.ingest_text("alpha beta gamma", source="a.txt")
    rag.retrieve("alpha")                       # builds cache
    calls = {"n": 0}
    real_fetch = store.fetch_vectors

    def counting_fetch(ns):
        calls["n"] += 1
        return real_fetch(ns)

    monkeypatch.setattr(store, "fetch_vectors", counting_fetch)
    rag.retrieve("alpha")                        # version unchanged -> cache hit
    rag.retrieve("beta")
    assert calls["n"] == 0                        # never rebuilt
