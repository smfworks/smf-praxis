import time

from hybridagent import config as cfg
from hybridagent.bm25 import BM25Index, tokenize
from hybridagent.memory import Memory
from hybridagent.persistence import Store


def test_tokenize_lowercases_alnum():
    assert tokenize("Hello, HIPAA-2 world!") == ["hello", "hipaa", "2", "world"]


def test_bm25_ranks_discriminative_doc_first():
    idx = BM25Index.build([
        ("d1", "the quarterly compliance audit found no HIPAA violations"),
        ("d2", "the team shipped the new streaming chat feature this week"),
        ("d3", "remember to renew the SOC 2 certification next month"),
    ])
    top = idx.search("hipaa compliance audit", k=3)
    assert top[0][0] == "d1"
    assert all(score > 0 for _id, score in top)


def test_bm25_out_of_vocab_query_returns_empty():
    idx = BM25Index.build([("d1", "alpha beta"), ("d2", "gamma delta")])
    assert idx.search("kangaroo", k=5) == []


def test_bm25_all_docs_term_stays_nonnegative():
    # A term present in every doc would give classic Okapi IDF a negative value;
    # Lucene-style IDF keeps it >= 0 so matches are not wrongly dropped.
    idx = BM25Index.build([("d1", "alpha shared"), ("d2", "beta shared")])
    hits = dict(idx.search("shared", k=5))
    assert set(hits) == {"d1", "d2"}
    assert all(s >= 0 for s in hits.values())


def test_bm25_length_normalization_prefers_focused_doc():
    idx = BM25Index.build([
        ("short", "budget forecast"),
        ("long", "budget " + " ".join(f"w{i}" for i in range(80))),
    ])
    top = idx.search("budget forecast", k=2)
    assert top[0][0] == "short"  # the concise, on-topic doc wins


def test_bm25_ties_are_deterministic_by_insertion_order():
    idx = BM25Index.build([("a", "same text here"), ("b", "same text here")])
    top = idx.search("same text", k=2)
    assert [doc_id for doc_id, _ in top] == ["a", "b"]


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_recall_uses_bm25_discrimination(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    mem.add_durable("renew the SOC 2 certification audit deadline", "fact", "t")
    mem.add_durable("the the the the the the the the the the the", "fact", "t")
    hits = mem.recall("SOC 2 certification audit", k=1)
    assert hits and "SOC 2" in hits[0].text


def test_recall_skips_expired_and_records_access(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    mem.add_durable("alpha expired", "fact", "t", expires_at=time.time() - 10)
    active = mem.add_durable("alpha active", "fact", "t", expires_at=time.time() + 100)
    hits = mem.recall("alpha", k=5)
    assert [h.text for h in hits] == ["alpha active"]
    assert active.access_count == 1


def test_recall_honors_salience_tiebreak(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    mem.add_durable("alpha beta low", "fact", "t", salience=0.1)
    high = mem.add_durable("alpha beta high", "fact", "t", salience=5.0)
    hits = mem.recall("alpha beta", k=1)
    assert hits[0].text == high.text


def test_recall_context_formats_block_and_empties(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    mem.add_durable("the launch date is March 14", "fact", "t")
    ctx = mem.recall_context("when is the launch date")
    assert "Relevant memory" in ctx and "March 14" in ctx
    assert mem.recall_context("totally unrelated kangaroo") == ""
