import time

from hybridagent import config as cfg
from hybridagent.memory import Memory
from hybridagent.persistence import Store
from hybridagent.rag import Rag
from hybridagent.wiki import KBSourceManager


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_memory_recall_honors_salience_and_records_access(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    low = mem.add_durable("alpha beta low priority", "fact", "test", salience=0.1)
    high = mem.add_durable("alpha beta high priority", "fact", "test", salience=5.0)

    hits = mem.recall("alpha beta", k=1)
    assert hits[0].text == high.text
    assert high.access_count == 1

    fresh = Memory(store=Store.open())
    loaded_high = [m for m in fresh.durable if m.text == high.text][0]
    loaded_low = [m for m in fresh.durable if m.text == low.text][0]
    assert loaded_high.access_count == 1
    assert loaded_low.access_count == 0


def test_memory_recall_skips_expired_items(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    mem = Memory(store=Store.open())
    mem.add_durable("alpha expired item", "fact", "test",
                    expires_at=time.time() - 10)
    mem.add_durable("alpha active item", "fact", "test",
                    expires_at=time.time() + 100)
    hits = mem.recall("alpha", k=5)
    assert [h.text for h in hits] == ["alpha active item"]


def test_kb_source_add_and_refresh_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    doc = tmp_path / "wiki.md"
    doc.write_text("# Wiki\n\nAdventHealth Q3 revenue guidance.", encoding="utf-8")
    mgr = KBSourceManager(store)
    src = mgr.add(str(doc), refresh_interval_seconds=0)
    assert src.status == "pending"

    refreshed = mgr.refresh(src.source_id, rag=Rag(store))
    assert refreshed.status == "refreshed"
    assert refreshed.last_hash
    assert refreshed.last_ingested_ts is not None
    assert Rag(store).retrieve("AdventHealth revenue")


def test_kb_source_refresh_unchanged_does_not_reingest(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    doc = tmp_path / "wiki.md"
    doc.write_text("alpha beta gamma", encoding="utf-8")
    mgr = KBSourceManager(store)
    src = mgr.add(str(doc), refresh_interval_seconds=0)
    first = mgr.refresh(src.source_id, rag=Rag(store))
    chunks = store.count_vectors("kb")

    second = mgr.refresh(src.source_id, rag=Rag(store))
    assert second.status == "unchanged"
    assert second.last_hash == first.last_hash
    assert store.count_vectors("kb") == chunks


def test_kb_due_sources_respects_refresh_interval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    doc = tmp_path / "wiki.md"
    doc.write_text("alpha", encoding="utf-8")
    mgr = KBSourceManager(store)
    src = mgr.add(str(doc), refresh_interval_seconds=3600)
    assert src.source_id in [s.source_id for s in mgr.due()]  # never ingested
    mgr.refresh(src.source_id, rag=Rag(store))
    assert src.source_id not in [s.source_id for s in mgr.due()]
