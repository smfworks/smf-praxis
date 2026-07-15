"""Slice 1 — Active Memory Consolidation schema + Store methods.

Tests the additive schema (memory_connections table, entities/topics/
last_consolidated_at columns) and the six new Store methods. No behavior
change to the daemon or agent — this slice is storage-only and inert until
Slice 3 wires the daemon tick.
"""
import json
import time

from hybridagent.persistence import Store


# ------------------------------------------------------------- connections
def test_add_and_list_connection(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "fact A", "seed", "note")
    b = s.add_memory("durable", "fact B", "seed", "note")
    cid = s.add_memory_connection(a, b, "addresses reliability gap")
    assert cid is not None

    conns = s.connections_of(a)
    assert len(conns) == 1
    c = conns[0]
    assert c["from_id"] == a and c["to_id"] == b
    assert c["relationship"] == "addresses reliability gap"
    # linked item text surfaced without a second round-trip
    assert c["linked_text"] == "fact B"
    assert c["linked_kind"] == "note"


def test_connection_is_unique_on_triple(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "A", "seed", "note")
    b = s.add_memory("durable", "B", "seed", "note")
    first = s.add_memory_connection(a, b, "same rel")
    second = s.add_memory_connection(a, b, "same rel")
    assert first is not None
    assert second is None  # UNIQUE(from_id, to_id, relationship)
    # different relationship is allowed
    third = s.add_memory_connection(a, b, "different rel")
    assert third is not None
    assert len(s.connections_of(a)) == 2


def test_delete_memory_cascades_connections(tmp_path):
    """Deleting a memory must drop all connections touching it (FK CASCADE)."""
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "A", "seed", "note")
    b = s.add_memory("durable", "B", "seed", "note")
    c = s.add_memory("durable", "C", "seed", "note")
    s.add_memory_connection(a, b, "a-b")
    s.add_memory_connection(b, c, "b-c")
    assert len(s.connections_of(b)) == 2

    # FK ON DELETE CASCADE should fire when memory_items row is deleted.
    # (SQLite needs PRAGMA foreign_keys=ON; Store sets this in __init__.)
    assert s.delete_memory(b) is True
    assert s.connections_of(a) == []
    assert s.connections_of(c) == []
    assert s.connections_of(b) == []


def test_delete_connections_for_memory_explicit(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "A", "seed", "note")
    b = s.add_memory("durable", "B", "seed", "note")
    s.add_memory_connection(a, b, "rel")
    n = s.delete_memory_connections_for(a)
    assert n == 1
    assert s.connections_of(a) == []


def test_insight_id_set_null_on_insight_delete(tmp_path):
    """Deleting an insight memory SET NULLs the insight_id column (not cascade)."""
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "A", "seed", "note")
    b = s.add_memory("durable", "B", "seed", "note")
    insight = s.add_memory("durable", "cross-cutting insight", "consolidation", "insight")
    s.add_memory_connection(a, b, "connected by insight", insight_id=insight)

    assert s.delete_memory(insight) is True
    conns = s.connections_of(a)
    assert len(conns) == 1
    assert conns[0]["insight_id"] is None  # SET NULL, connection survives


# ------------------------------------------------------------- metadata
def test_update_memory_metadata(tmp_path):
    s = Store(tmp_path / "t.db")
    mid = s.add_memory("durable", "Anthropic reports 62% code usage", "seed", "note")
    s.update_memory_metadata(mid, ["Anthropic", "Claude"], ["code-usage", "agents"])
    rows = s.list_memory(tier="durable")
    row = [r for r in rows if r["id"] == mid][0]
    # list_memory doesn't return entities/topics; read directly
    with s._lock:
        raw = s._conn.execute(
            "SELECT entities, topics FROM memory_items WHERE id=?", (mid,)
        ).fetchone()
    assert json.loads(raw["entities"]) == ["Anthropic", "Claude"]
    assert json.loads(raw["topics"]) == ["code-usage", "agents"]


def test_metadata_defaults_to_empty_json(tmp_path):
    """New memory items must default to '[]' for entities/topics (not NULL)."""
    s = Store(tmp_path / "t.db")
    mid = s.add_memory("durable", "raw fact", "seed", "note")
    with s._lock:
        raw = s._conn.execute(
            "SELECT entities, topics, last_consolidated_at FROM memory_items WHERE id=?",
            (mid,),
        ).fetchone()
    assert json.loads(raw["entities"]) == []
    assert json.loads(raw["topics"]) == []
    assert raw["last_consolidated_at"] is None


# ------------------------------------------------------------- salience
def test_update_memory_salience_clamps(tmp_path):
    s = Store(tmp_path / "t.db")
    mid = s.add_memory("durable", "fact", "seed", "note", salience=0.5)
    s.update_memory_salience(mid, 0.9)
    rows = s.list_memory(tier="durable")
    assert [r for r in rows if r["id"] == mid][0]["salience"] == 0.9

    # over-cap clamps to 1.0
    s.update_memory_salience(mid, 1.5)
    rows = s.list_memory(tier="durable")
    assert [r for r in rows if r["id"] == mid][0]["salience"] == 1.0

    # under-cap clamps to 0.0
    s.update_memory_salience(mid, -0.3)
    rows = s.list_memory(tier="durable")
    assert [r for r in rows if r["id"] == mid][0]["salience"] == 0.0


# ------------------------------------------------------------- mark_consolidated
def test_mark_consolidated_sets_timestamp_not_boolean(tmp_path):
    """last_consolidated_at is a timestamp, NOT a consolidated=1 boolean.
    Items remain eligible for re-consolidation as the corpus grows."""
    s = Store(tmp_path / "t.db")
    a = s.add_memory("durable", "A", "seed", "note")
    b = s.add_memory("durable", "B", "seed", "note")
    t0 = time.time()
    s.mark_consolidated([a, b], ts=t0)
    with s._lock:
        rows = s._conn.execute(
            "SELECT last_consolidated_at FROM memory_items WHERE id IN (?,?)",
            (a, b),
        ).fetchall()
    assert all(r["last_consolidated_at"] == t0 for r in rows)


def test_mark_consolidated_empty_list_noop(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_memory("durable", "A", "seed", "note")
    s.mark_consolidated([])  # must not raise
    with s._lock:
        row = s._conn.execute(
            "SELECT last_consolidated_at FROM memory_items LIMIT 1"
        ).fetchone()
    assert row["last_consolidated_at"] is None


def test_mark_consolidated_is_idempotent_re_eligible(tmp_path):
    """Re-marking with a later timestamp updates it — items stay eligible."""
    s = Store(tmp_path / "t.db")
    mid = s.add_memory("durable", "A", "seed", "note")
    s.mark_consolidated([mid], ts=1000.0)
    s.mark_consolidated([mid], ts=2000.0)
    with s._lock:
        row = s._conn.execute(
            "SELECT last_consolidated_at FROM memory_items WHERE id=?", (mid,)
        ).fetchone()
    assert row["last_consolidated_at"] == 2000.0