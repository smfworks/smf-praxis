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


# ======================================================================
# Slice 2 — MemoryConsolidator core (offline-testable, no daemon wiring)
# ======================================================================
import json as _json

from hybridagent.consolidation import MemoryConsolidator, ConsolidationReport
from hybridagent.memory import Memory


class FakeLLM:
    """Returns canned JSON per call index. Matches LLMClient.complete()."""
    def __init__(self, metadata_resp=None, conn_resp=None, insight_resp=None):
        self.metadata_resp = metadata_resp
        self.conn_resp = conn_resp
        self.insight_resp = insight_resp
        self.calls = []

    def complete(self, prompt: str, system=None, role="general",
                 sensitivity="normal", difficulty=None) -> str:
        self.calls.append((role, prompt[:40]))
        if "entities" in prompt and "topics" in prompt:
            return self.metadata_resp or "[]"
        if "from_id" in prompt and "to_id" in prompt:
            return self.conn_resp or "[]"
        if "Insight:" in prompt:
            return self.insight_resp or ""
        return ""


def _seed_window(store, n=3):
    """Add n durable memories with distinct content for consolidation."""
    texts = ["Anthropic reports 62% code usage",
             "Q1 priority: reduce inference costs by 40%",
             "Current LLM memory approaches have reliability gaps"]
    ids = []
    for t in texts[:n]:
        ids.append(store.add_memory("durable", t, "seed", "note"))
    return ids


def test_consolidation_happy_path_writes_insight_and_connections(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    mem = Memory(store=s)

    fake = FakeLLM(
        metadata_resp=_json.dumps([
            {"id": ids[0], "entities": ["Anthropic", "Claude"], "topics": ["code-usage"]},
            {"id": ids[1], "entities": ["inference"], "topics": ["cost"]},
            {"id": ids[2], "entities": ["memory"], "topics": ["reliability"]},
        ]),
        conn_resp=_json.dumps([
            {"from_id": ids[0], "to_id": ids[1], "relationship": "usage drives cost pressure"},
            {"from_id": ids[1], "to_id": ids[2], "relationship": "cost cut needs reliability fix"},
        ]),
        insight_resp="Code-heavy usage is driving inference costs, and fixing memory "
                      "reliability is the precondition for any cost reduction to hold.",
    )
    c = MemoryConsolidator(mem, fake, s, window_size=20, min_items=3)
    report = c.run()
    assert report.items_reviewed == 3
    assert report.connections_made == 2
    assert report.insights_written == 1
    assert report.skipped_reason == ""

    # insight landed as a durable memory of kind=insight
    insights = [m for m in mem.durable if m.kind == "insight"]
    assert len(insights) == 1
    assert "cost" in insights[0].text.lower()

    # connections recorded against the insight
    conns = s.connections_of(ids[0])
    assert len(conns) == 1
    assert conns[0]["insight_id"] == insights[0].id

    # all three items marked consolidated
    with s._lock:
        rows = s._conn.execute(
            f"SELECT last_consolidated_at FROM memory_items WHERE id IN ({ids[0]},{ids[1]},{ids[2]})"
        ).fetchall()
    assert all(r["last_consolidated_at"] is not None for r in rows)


def test_consolidation_skips_below_min_items(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_memory("durable", "only one", "seed", "note")
    mem = Memory(store=s)
    fake = FakeLLM()
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report = c.run()
    assert report.items_reviewed == 1
    assert report.insights_written == 0
    assert "too few" in report.skipped_reason
    # LLM was never called
    assert fake.calls == []


def test_consolidation_malformed_metadata_json_does_not_block(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="this is not json at all",
        conn_resp=_json.dumps([]),
        insight_resp="A valid insight despite bad metadata upstream.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report = c.run()
    # metadata skipped, but pass still completed and wrote an insight
    assert report.insights_written == 1
    assert report.skipped_reason == ""


def test_consolidation_malformed_connections_json_still_writes_insight(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp="{not valid json",
        insight_resp="Insight with no connections found but still synthesized.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report = c.run()
    assert report.connections_made == 0
    assert report.insights_written == 1


def test_consolidation_empty_insight_response_skips_insight(tmp_path):
    s = Store(tmp_path / "t.db")
    _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp="[]",
        insight_resp="",   # empty
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report = c.run()
    assert report.insights_written == 0
    # but items still marked consolidated
    uncons = s.list_unconsolidated()
    assert len(uncons) == 0


def test_consolidation_rerates_salience_bounded_monotonic(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    # give them all some salience + access to verify the bump + clamp
    for mid in ids:
        s.update_memory_salience(mid, 0.95)
        s.record_memory_access(mid)  # access_count -> 1
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp=_json.dumps([
            {"from_id": ids[0], "to_id": ids[1], "relationship": "linked"},
        ]),
        insight_resp="An insight connecting the first two items.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3, rerate_salience=True)
    report = c.run()
    assert report.salience_rerated >= 2  # ids[0] + ids[1] bumped by connection, all 3 by access
    # connection bump (+0.1) on 0.95 would exceed 1.0 -> clamped
    rows = {r["id"]: r for r in s.list_memory(tier="durable")}
    assert rows[ids[0]]["salience"] == 1.0  # 0.95 + 0.1 + 0.05 -> clamped
    assert rows[ids[1]]["salience"] == 1.0  # 0.95 + 0.1 + 0.05 -> clamped
    assert rows[ids[2]]["salience"] == 1.0  # 0.95 + 0.05 -> 1.0


def test_consolidation_rerate_disabled_skips_bump(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    s.record_memory_access(ids[0])
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp=_json.dumps([
            {"from_id": ids[0], "to_id": ids[1], "relationship": "rel"},
        ]),
        insight_resp="Insight.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3, rerate_salience=False)
    report = c.run()
    assert report.salience_rerated == 0


def test_consolidation_idempotent_rerun_within_window(tmp_path):
    """Re-running with re_consolidate_after=None skips already-consolidated
    items (last_consolidated_at IS NOT NULL). This is the anti-one-shot-flag
    behavior: items STAY re-eligible if re_consolidate_after is set, but a
    plain re-run doesn't re-process them immediately."""
    s = Store(tmp_path / "t.db")
    _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]", conn_resp="[]",
        insight_resp="First insight.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report1 = c.run()
    assert report1.insights_written == 1

    # second run with no re_consolidate_after -> window empty -> skipped
    report2 = c.run()
    assert report2.items_reviewed == 0
    assert "too few" in report2.skipped_reason


def test_consolidation_re_eligible_after_window(tmp_path):
    """With re_consolidate_after set, already-consolidated items become
    eligible again — the anti-one-shot-flag behavior."""
    s = Store(tmp_path / "t.db")
    _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]", conn_resp="[]",
        insight_resp="First insight.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report1 = c.run()
    assert report1.insights_written == 1

    # mark all items consolidated far in the past -> re-eligible
    fake.insight_resp = "Second insight after re-eligibility window."
    report2 = c.run(re_consolidate_after=time.time() + 1)
    assert report2.items_reviewed == 3
    assert report2.insights_written == 1


def test_consolidation_connection_to_self_rejected(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp=_json.dumps([
            {"from_id": ids[0], "to_id": ids[0], "relationship": "self-link"},
            {"from_id": 99999, "to_id": ids[0], "relationship": "foreign id"},
            {"from_id": ids[0], "to_id": ids[1], "relationship": "valid link"},
        ]),
        insight_resp="Valid insight despite bad links.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3)
    report = c.run()
    assert report.connections_made == 1  # only the valid link survives


def test_consolidation_respects_max_connections_cap(tmp_path):
    s = Store(tmp_path / "t.db")
    ids = _seed_window(s, 3)
    mem = Memory(store=s)
    fake = FakeLLM(
        metadata_resp="[]",
        conn_resp=_json.dumps([
            {"from_id": ids[0], "to_id": ids[1], "relationship": f"rel {i}"}
            for i in range(10)
        ]),
        insight_resp="Insight.",
    )
    c = MemoryConsolidator(mem, fake, s, min_items=3, max_connections=2)
    report = c.run()
    assert report.connections_made == 2


def test_consolidation_json_parser_strips_fences_and_prose(tmp_path):
    """The strict parser must tolerate ```json fences and trailing prose,
    and reject responses with no JSON array."""
    parse = MemoryConsolidator._parse_json_list
    # fenced
    assert parse('```json\n[{"id": 1}]\n```') == [{"id": 1}]
    # trailing prose
    assert parse('Here you go:\n[{"a": 1}]\nThat is all.') == [{"a": 1}]
    # no array
    import pytest as _pt
    try:
        parse("no json here")
        assert False, "should have raised"
    except ValueError:
        pass


def test_consolidation_report_as_dict(tmp_path):
    r = ConsolidationReport(items_reviewed=5, connections_made=2,
                            insights_written=1, salience_rerated=3,
                            skipped_reason="")
    d = r.as_dict()
    assert d["items_reviewed"] == 5
    assert d["connections_made"] == 2
    assert d["insights_written"] == 1
    assert d["salience_rerated"] == 3
    assert d["skipped_reason"] == ""

# ======================================================================
# Slice 3 — Daemon wiring (gated off by default, fires when enabled)
# ======================================================================
from hybridagent import config as cfg
from hybridagent.daemon import Daemon
from hybridagent.llm import LLMClient


class _FakeConsolidator:
    """Replaces MemoryConsolidator in the daemon to detect when the tick fires."""
    instances: list = []
    next_report = ConsolidationReport(items_reviewed=3, connections_made=1,
                                      insights_written=1, salience_rerated=2)

    def __init__(self, memory, llm, store, **kwargs):
        self.kwargs = kwargs
        self.memory = memory
        self.llm = llm
        self.store = store
        self.run_called = False
        _FakeConsolidator.instances.append(self)

    def run(self, re_consolidate_after=None):
        self.run_called = True
        return _FakeConsolidator.next_report

    def stats(self):
        return {"pending": 0}


def test_consolidation_tick_is_noop_when_disabled(tmp_path, monkeypatch):
    """Off by default: the tick reads config, sees enabled=false, returns
    without touching the store or LLM."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    # ensure consolidation is off
    cfg.set_consolidation_config({"enabled": False})
    d = Daemon(llm=LLMClient(mode="mock"))
    _FakeConsolidator.instances.clear()
    d._consolidation_tick()
    assert _FakeConsolidator.instances == []  # never constructed
    assert d.consolidation_status()["enabled"] is False


def test_consolidation_tick_fires_when_enabled(tmp_path, monkeypatch):
    """When enabled, the tick constructs the consolidator and runs it."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({
        "enabled": True, "intervalMinutes": 30,
        "windowSize": 5, "minItemsToConsolidate": 1,
    })
    d = Daemon(llm=LLMClient(mode="mock"))
    # seed at least one memory so the consolidator has a window (not strictly
    # required since we mock, but realistic)
    d._ensure_agent()
    assert d.store is not None
    d.store.add_memory("durable", "seed fact for consolidation", "test", "note")

    import hybridagent.consolidation as cons_mod
    orig = cons_mod.MemoryConsolidator
    cons_mod.MemoryConsolidator = _FakeConsolidator
    try:
        d._consolidation_tick()
    finally:
        cons_mod.MemoryConsolidator = orig
    assert len(_FakeConsolidator.instances) == 1
    assert _FakeConsolidator.instances[0].run_called is True
    # interval scheduled forward
    import time as _t
    assert d._next_consolidation_ts > _t.time()


def test_consolidation_tick_deferred_by_pending_work(tmp_path, monkeypatch):
    """If the task queue has pending work, the tick defers 60s and does NOT
    run — consolidation never starves the user-facing loop."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": True, "intervalMinutes": 30})
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    # simulate pending work by monkeypatching the manager.list to return non-empty
    class _M:
        def list(self, status=None):
            return [{"id": "fake"}] if status == "pending" else []
    d.manager = _M()
    _FakeConsolidator.instances.clear()
    import hybridagent.consolidation as cons_mod
    orig = cons_mod.MemoryConsolidator
    cons_mod.MemoryConsolidator = _FakeConsolidator
    try:
        d._consolidation_tick()
    finally:
        cons_mod.MemoryConsolidator = orig
    assert _FakeConsolidator.instances == []  # deferred, never constructed
    import time as _t
    assert d._next_consolidation_ts >= _t.time() + 55  # ~60s


def test_consolidation_run_respects_disabled_gate(tmp_path, monkeypatch):
    """Manual trigger returns an error notice when consolidation is off."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": False})
    d = Daemon(llm=LLMClient(mode="mock"))
    result = d.consolidation_run()
    assert "error" in result
    assert "disabled" in result["error"]


def test_consolidation_run_fires_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": True, "minItemsToConsolidate": 1})
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    d.store.add_memory("durable", "manual-trigger seed", "test", "note")
    import hybridagent.consolidation as cons_mod
    orig = cons_mod.MemoryConsolidator
    cons_mod.MemoryConsolidator = _FakeConsolidator
    _FakeConsolidator.instances.clear()
    try:
        result = d.consolidation_run()
    finally:
        cons_mod.MemoryConsolidator = orig
    assert "report" in result
    assert result["report"]["insights_written"] == 1


# ======================================================================
# Slice 4 — Metadata extraction on ingest (Gap C, write path)
# ======================================================================

class _MetadataFakeLLM:
    """Returns canned JSON for the extract-on-write prompt."""
    def __init__(self, resp='{"entities": ["X"], "topics": ["y"]}'):
        self.resp = resp
        self.calls = 0

    def complete(self, prompt, system=None, role="general",
                 sensitivity="normal", difficulty=None):
        self.calls += 1
        return self.resp


def test_extract_on_write_populates_metadata(tmp_path):
    """When extract_metadata is enabled and an LLM is present, add_episodic
    and add_durable extract entities/topics before the method returns."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM('{"entities": ["Anthropic", "Claude"], "topics": ["code"]}')
    mem = Memory(store=s, llm=llm, extract_metadata=True)

    item = mem.add_durable("Anthropic reports 62% code usage", "note", "test")
    assert llm.calls == 1
    with s._lock:
        row = s._conn.execute(
            "SELECT entities, topics FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == ["Anthropic", "Claude"]
    assert _j.loads(row["topics"]) == ["code"]


def test_extract_on_write_episodic_also(tmp_path):
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM('{"entities": ["A"], "topics": ["b"]}')
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    item = mem.add_episodic("an episodic event", "test")
    assert llm.calls == 1
    with s._lock:
        row = s._conn.execute(
            "SELECT entities FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == ["A"]


def test_extract_off_by_default_no_llm_calls(tmp_path):
    """Default Memory (no llm, extract_metadata=False) never calls the LLM."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM()
    mem = Memory(store=s, llm=llm, extract_metadata=False)
    mem.add_durable("a fact", "note", "test")
    mem.add_episodic("an event", "test")
    assert llm.calls == 0


def test_extract_no_llm_no_extraction(tmp_path):
    """extract_metadata=True but no llm -> no extraction, write succeeds."""
    s = Store(tmp_path / "t.db")
    mem = Memory(store=s, llm=None, extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    assert item.id is not None  # write succeeded
    with s._lock:
        row = s._conn.execute(
            "SELECT entities FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == []  # default, no extraction


def test_extract_malformed_json_honest_fail(tmp_path):
    """Malformed LLM response -> write still succeeds, metadata stays []."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM("this is not json")
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    assert item.id is not None
    with s._lock:
        row = s._conn.execute(
            "SELECT entities, topics FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == []
    assert _j.loads(row["topics"]) == []


def test_extract_llm_raises_honest_fail(tmp_path):
    """LLM raising an exception -> write succeeds, metadata stays []."""
    s = Store(tmp_path / "t.db")

    class _BoomLLM:
        def complete(self, *a, **k):
            raise RuntimeError("network down")

    mem = Memory(store=s, llm=_BoomLLM(), extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    assert item.id is not None  # write succeeded despite LLM failure


def test_extract_skipped_for_insights(tmp_path):
    """Insights written by consolidation skip extraction (they're the output,
    not the input — extracting would compound LLM cost every pass)."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM()
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    mem.add_durable("a cross-cutting insight", "insight", "consolidation:123")
    assert llm.calls == 0  # no extraction on insights


def test_extract_working_tier_not_extracted(tmp_path):
    """Working tier is in-process, cleared each cycle — no metadata needed."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM()
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    mem.note_working("a working note")
    assert llm.calls == 0


def test_extract_fenced_json_parsed(tmp_path):
    """LLM wrapping JSON in ```json fences still extracts correctly."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM('```json\n{"entities": ["X"], "topics": ["y"]}\n```')
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    with s._lock:
        row = s._conn.execute(
            "SELECT entities FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == ["X"]


def test_extract_trailing_prose_parsed(tmp_path):
    """LLM adding trailing prose around the JSON object still parses."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM('Here you go:\n{"entities": ["A"], "topics": ["b"]}\nDone.')
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    with s._lock:
        row = s._conn.execute(
            "SELECT topics FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["topics"]) == ["b"]


def test_extract_returns_list_not_dict_honest_fail(tmp_path):
    """LLM returning a list instead of a dict -> no extraction, write ok."""
    s = Store(tmp_path / "t.db")
    llm = _MetadataFakeLLM('["entities", "topics"]')
    mem = Memory(store=s, llm=llm, extract_metadata=True)
    item = mem.add_durable("a fact", "note", "test")
    assert item.id is not None
    with s._lock:
        row = s._conn.execute(
            "SELECT entities FROM memory_items WHERE id=?", (item.id,)
        ).fetchone()
    import json as _j
    assert _j.loads(row["entities"]) == []


def test_agent_threads_llm_to_memory(tmp_path, monkeypatch):
    """PraxisAgent constructs Memory with its LLM so the write path can
    extract when extractMetadata is enabled in config."""
    from hybridagent.agent import PraxisAgent
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    # enable extraction in config so the agent constructor picks it up
    cfg.set_consolidation_config({"enabled": True, "extractMetadata": True})
    agent = PraxisAgent(store=Store(tmp_path / "p.db"))
    assert agent.memory.llm is agent.llm
    assert agent.memory.extract_metadata is True


def test_agent_extract_off_when_config_disabled(tmp_path, monkeypatch):
    """When extractMetadata is false in config, the agent's Memory starts
    with extraction off (the daemon tick may flip it later)."""
    from hybridagent.agent import PraxisAgent
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": False, "extractMetadata": False})
    agent = PraxisAgent(store=Store(tmp_path / "p.db"))
    assert agent.memory.extract_metadata is False


# ======================================================================
# Slice 5 — CLI + dashboard visibility
# ======================================================================
import socket
import urllib.parse
import urllib.request


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def test_consolidation_status_endpoint_returns_config(tmp_path, monkeypatch):
    """GET /api/consolidation returns the config + pending count + timestamps."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/consolidation", timeout=10
        ) as r:
            body = json.loads(r.read())
        assert body["enabled"] is False  # off by default
        assert "intervalMinutes" in body
        assert "windowSize" in body
        assert "pending" in body
        assert "next_run_ts" in body
    finally:
        d._stop_status_server()


def test_consolidation_dashboard_assets_served(tmp_path, monkeypatch):
    """The dashboard serves consolidation.js and consolidation.css, and the
    dashboard HTML embeds the consolidation mount + asset links."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        for asset in ("consolidation.js", "consolidation.css"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/web/{asset}", timeout=10
            ) as r:
                body = r.read().decode()
            assert "consolidation" in body.lower() or "cons-" in body
        # dashboard HTML references the mount + assets
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=10
        ) as r:
            html = r.read().decode()
        assert "consolidation-mount" in html
        assert "/web/consolidation.js" in html
        assert "/web/consolidation.css" in html
    finally:
        d._stop_status_server()


def test_consolidation_run_endpoint_respects_disabled_gate(tmp_path, monkeypatch):
    """POST /api/consolidation/run returns an error when consolidation is off."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": False})
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        result = _post(f"http://127.0.0.1:{port}/api/consolidation/run", {})
        assert "error" in result
        assert "disabled" in result["error"]
    finally:
        d._stop_status_server()


def test_consolidation_run_endpoint_fires_when_enabled(tmp_path, monkeypatch):
    """POST /api/consolidation/run triggers a pass and stores last_report
    when enabled."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({
        "enabled": True, "intervalMinutes": 30,
        "windowSize": 5, "minItemsToConsolidate": 1,
    })
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._ensure_agent()
    assert d.store is not None
    d.store.add_memory("durable", "seed for manual run", "test", "note")
    d._start_status_server()
    try:
        import hybridagent.consolidation as cons_mod
        orig = cons_mod.MemoryConsolidator
        cons_mod.MemoryConsolidator = _FakeConsolidator
        _FakeConsolidator.instances.clear()
        try:
            result = _post(f"http://127.0.0.1:{port}/api/consolidation/run", {})
        finally:
            cons_mod.MemoryConsolidator = orig
        assert "report" in result
        assert result["report"]["insights_written"] == 1
        # last_report stored on the daemon for status endpoint
        assert d._last_consolidation_report is not None
        assert d._last_consolidation_report["insights_written"] == 1
    finally:
        d._stop_status_server()


def test_consolidation_status_shows_last_report_after_run(tmp_path, monkeypatch):
    """After a pass, GET /api/consolidation includes last_report."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_consolidation_config({"enabled": True, "minItemsToConsolidate": 1})
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._ensure_agent()
    d.store.add_memory("durable", "seed", "test", "note")
    d._start_status_server()
    try:
        import hybridagent.consolidation as cons_mod
        orig = cons_mod.MemoryConsolidator
        cons_mod.MemoryConsolidator = _FakeConsolidator
        try:
            _post(f"http://127.0.0.1:{port}/api/consolidation/run", {})
        finally:
            cons_mod.MemoryConsolidator = orig
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/consolidation", timeout=10
        ) as r:
            body = json.loads(r.read())
        assert body["last_report"] is not None
        assert body["last_run_ts"] > 0
    finally:
        d._stop_status_server()


# --- CLI smoke (subprocess-free: call the cmd function directly) ---
import argparse


def test_cli_consolidation_enable_disable_flips_config(tmp_path, monkeypatch):
    """`praxis consolidation enable` then `disable` flips the config flag.
    These don't need the daemon to be running."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.cli import cmd_consolidation
    # reset to a known state
    cfg.set_consolidation_config({"enabled": False})
    assert cfg.get_consolidation_config()["enabled"] is False

    ns = argparse.Namespace(action="enable")
    assert cmd_consolidation(ns) == 0
    assert cfg.get_consolidation_config()["enabled"] is True

    ns = argparse.Namespace(action="disable")
    assert cmd_consolidation(ns) == 0
    assert cfg.get_consolidation_config()["enabled"] is False


def test_cli_consolidation_status_daemon_down_shows_config(tmp_path, monkeypatch):
    """`praxis consolidation status` with no daemon still prints config
    and returns exit 1 (daemon not running)."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.cli import cmd_consolidation
    cfg.set_consolidation_config({"enabled": False})
    ns = argparse.Namespace(action="status")
    rc = cmd_consolidation(ns)
    assert rc == 1  # daemon down, but config was still printed


def test_cli_consolidation_run_daemon_down_returns_1(tmp_path, monkeypatch):
    """`praxis consolidation run` with no daemon returns 1."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.cli import cmd_consolidation
    cfg.set_consolidation_config({"enabled": True})
    ns = argparse.Namespace(action="run")
    rc = cmd_consolidation(ns)
    assert rc == 1  # daemon not running
