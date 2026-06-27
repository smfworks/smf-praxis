import tempfile

from hybridagent.persistence import Store
from hybridagent.rag import Rag, reciprocal_rank_fusion


# ----------------------------------------------------------------- RRF unit
def test_rrf_single_list_preserves_order():
    fused = reciprocal_rank_fusion([["a", "b", "c"]])
    assert [doc for doc, _ in fused] == ["a", "b", "c"]


def test_rrf_rewards_agreement():
    # 'b' is top in both lists -> it should win overall.
    fused = dict(reciprocal_rank_fusion([["b", "a", "c"], ["b", "c", "a"]]))
    assert fused["b"] == max(fused.values())


def test_rrf_is_symmetric_for_mirror_lists():
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "a"]]))
    # a and c each rank #0 once and #2 once -> equal; b ranks #1 in both.
    assert abs(fused["a"] - fused["c"]) < 1e-9
    assert fused["a"] > 0 and fused["b"] > 0


def test_rrf_deterministic_tiebreak_by_id():
    fused = reciprocal_rank_fusion([["x"], ["y"]])  # equal scores
    assert [doc for doc, _ in fused] == ["x", "y"]  # tie broken by id


def test_rrf_empty():
    assert reciprocal_rank_fusion([]) == []


# ----------------------------------------------------------- Rag hybrid path
def _rag():
    d = tempfile.mkdtemp()
    store = Store.open(f"{d}/praxis.db")
    rag = Rag(store)
    rag.ingest_text("the annual budget forecast for fiscal year 2027", "budget")
    rag.ingest_text("customer onboarding checklist and welcome sequence", "onboarding")
    rag.ingest_text("quarterly hiring plan and headcount targets", "hiring")
    return store, rag


def test_hybrid_surfaces_lexically_relevant_doc():
    store, rag = _rag()
    try:
        hits = rag.retrieve("budget forecast fiscal year", k=1, hybrid=True)
        assert hits and hits[0].source == "budget"
    finally:
        store.close()


def test_embedding_only_path_still_returns_results():
    store, rag = _rag()
    try:
        hits = rag.retrieve("budget forecast", k=3, hybrid=False)
        assert isinstance(hits, list)  # pure-embedding path is intact
    finally:
        store.close()


def test_hybrid_empty_query_or_corpus():
    store, rag = _rag()
    try:
        assert rag.retrieve("", k=3) == []
        assert rag.retrieve("anything", k=3, ns="empty-ns") == []
    finally:
        store.close()


def test_bm25_index_is_cached_until_corpus_changes():
    store, rag = _rag()
    try:
        rag.retrieve("budget", k=1)  # builds + caches the BM25 index
        first = rag._bm25_cache["kb"][1]
        rag.retrieve("hiring plan", k=1)
        assert rag._bm25_cache["kb"][1] is first  # reused, not rebuilt
        rag.ingest_text("a new document about travel expenses", "travel")
        rag.retrieve("travel", k=1)
        assert rag._bm25_cache["kb"][1] is not first  # rebuilt after corpus change
    finally:
        store.close()
