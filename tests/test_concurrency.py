"""WAL / concurrency smoke tests for the SQLite store."""
import threading

from hybridagent import config as cfg
from hybridagent.persistence import Store


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_wal_mode_enabled(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_two_stores_share_writes_on_same_db(tmp_path, monkeypatch):
    # WAL lets a second connection read committed writes from the first.
    _isolate(tmp_path, monkeypatch)
    a = Store.open()
    a.add_memory("durable", "shared fact", "user", "fact")
    b = Store.open()
    assert any(r["text"] == "shared fact" for r in b.load_memory("durable"))


def test_concurrent_writes_do_not_corrupt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    errors = []

    def writer(n):
        try:
            for i in range(25):
                store.add_memory("episodic", f"w{n}-{i}", "agent", "note")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.load_memory("episodic")) == 100
