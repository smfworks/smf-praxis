from hybridagent import config as cfg
from hybridagent.persistence import Store
from hybridagent.rag import Rag
from hybridagent.wiki import KBSourceManager


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_kb_source_lifecycle_through_manager(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    doc = tmp_path / "kb.txt"
    doc.write_text("AdventHealth Q3 revenue guidance and outlook.", encoding="utf-8")

    mgr = KBSourceManager(store)
    src = mgr.add(str(doc), refresh_interval_seconds=0)

    # Appears in the list right after registration.
    assert src.source_id in [s.source_id for s in mgr.list()]

    # Refresh ingests it into the RAG store.
    refreshed = mgr.refresh(src.source_id, rag=Rag(store))
    assert refreshed.status == "refreshed"
    assert store.count_vectors(src.ns) > 0
    assert mgr.get(src.source_id) is not None

    # Delete removes the source row (and its vectors) and reports success.
    assert store.delete_kb_source(src.source_id) is True
    assert src.source_id not in [s.source_id for s in mgr.list()]
    assert mgr.get(src.source_id) is None
    assert store.count_vectors(src.ns) == 0

    # Deleting an unknown id returns False.
    assert store.delete_kb_source("src-does-not-exist") is False
