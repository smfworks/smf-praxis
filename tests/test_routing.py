"""D5b routing observability: per-run routing records + dashboard surfacing."""
from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# ------------------------------------------------------------------- store
def test_run_routing_roundtrip_with_goal(tmp_path):
    s = Store(tmp_path / "r.db")
    s.start_run("run-1", goal="summarize the report", kind="plan")
    s.record_run_routing("run-1", "openai/gpt-4o-mini", 1200, 300, 0.0004, 3,
                         local=False, fallbacks=1)
    rows = s.list_run_routing()
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == "run-1" and r["model"] == "openai/gpt-4o-mini"
    assert r["prompt_tokens"] == 1200 and r["completion_tokens"] == 300
    assert r["calls"] == 3 and r["fallbacks"] == 1
    assert r["local"] == 0                       # cloud
    assert r["goal"] == "summarize the report"   # joined from the runs table
    s.close()


def test_run_routing_upsert(tmp_path):
    s = Store(tmp_path / "r.db")
    s.record_run_routing("run-x", "a", 1, 1, 0.0, 1, local=True)
    s.record_run_routing("run-x", "b", 2, 2, 0.0, 2, local=True)   # replaces
    rows = s.list_run_routing()
    assert len(rows) == 1 and rows[0]["model"] == "b" and rows[0]["calls"] == 2
    s.close()


# ------------------------------------------------------------- accumulator
def test_accumulator_tracks_distinct_models():
    c = LLMClient(mode="mock")
    c.reset_usage()
    c._account("openai/gpt-4o-mini", {"prompt_tokens": 10, "completion_tokens": 5})
    c._account("openai/gpt-4o-mini", {"prompt_tokens": 4, "completion_tokens": 2})
    c._account("openai/gpt-4o", {"prompt_tokens": 1, "completion_tokens": 1})
    snap = c.usage_snapshot()
    # Distinct, insertion-ordered.
    assert snap["models"] == ["openai/gpt-4o-mini", "openai/gpt-4o"]
    assert snap["fallbacks"] == 0


def test_usage_accounting_is_atomic_under_threads():
    """P4b: the shared usage tally is lock-guarded, so concurrent _account calls
    (as happen when subagent fan-out runs LLM calls on worker threads) never lose
    a read-modify-write increment."""
    import threading

    c = LLMClient(mode="mock")
    c.reset_usage()
    workers, per = 16, 250

    def hammer():
        for _ in range(per):
            c._account("local/free", {"prompt_tokens": 1, "completion_tokens": 1})

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = c.usage_snapshot()
    total = workers * per
    assert snap["calls"] == total
    assert snap["prompt_tokens"] == total
    assert snap["completion_tokens"] == total


def test_note_escalation_captures_reason():
    c = LLMClient(mode="mock")
    c.reset_usage()
    c.note_escalation("escalated")
    c.note_escalation("unverified")          # latest reason wins
    snap = c.usage_snapshot()
    assert snap["escalations"] == 2
    assert snap["escalation_reason"] == "unverified"


def test_run_routing_records_escalation_reason(tmp_path):
    s = Store(tmp_path / "r.db")
    s.record_run_routing("run-er", "openai/gpt-4o", 100, 50, 0.01, 4,
                         local=False, escalations=1, escalation_reason="unverified")
    rows = s.list_run_routing()
    assert rows[0]["escalations"] == 1
    assert rows[0]["escalation_reason"] == "unverified"
    s.close()


# -------------------------------------------------------------------- daemon
def test_agent_run_records_routing(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    res = d.agent_run("draft a short note")
    rid = res["run_id"]

    routes = d.store.list_run_routing()
    rec = next(r for r in routes if r["run_id"] == rid)
    assert rec["model"] == "mock" and rec["local"] == 1   # mock routes local
    assert rec["cost_usd"] == 0.0

    info = d.inference_info()
    assert "routes" in info
    assert any(r["run_id"] == rid for r in info["routes"])


def test_run_routing_records_escalations(tmp_path):
    s = Store(tmp_path / "r.db")
    s.record_run_routing("run-e", "openai/gpt-4o", 100, 50, 0.01, 4,
                         local=False, fallbacks=1, escalations=2)
    rows = s.list_run_routing()
    assert rows[0]["escalations"] == 2 and rows[0]["fallbacks"] == 1
    s.close()


# ------------------------------------------------------------- cost stats (P6)
def test_routing_cost_stats_aggregates(tmp_path):
    s = Store(tmp_path / "r.db")
    s.start_run("run-a", goal="cloud task", kind="plan")
    s.record_run_routing("run-a", "openai/gpt-4o", 1000, 500, 0.02, 3, local=False)
    s.record_run_routing("run-b", "openai/gpt-4o-mini", 800, 200, 0.004, 2, local=False)
    s.record_run_routing("run-c", "openai/gpt-4o", 200, 100, 0.006, 1, local=False)
    s.record_run_routing("run-d", "ollama/llama3.1", 50, 50, 0.0, 1, local=True)

    stats = s.routing_cost_stats()
    assert round(stats["total_cost_usd"], 4) == 0.03    # 0.02 + 0.004 + 0.006 + 0
    assert stats["total_tokens"] == 2900               # 1500 + 1000 + 300 + 100
    assert stats["total_runs"] == 4
    assert stats["local_runs"] == 1

    # Per-model: gpt-4o is costliest (0.026 across 2 runs) so it sorts first.
    by_model = {m["model"]: m for m in stats["by_model"]}
    assert stats["by_model"][0]["model"] == "openai/gpt-4o"
    assert round(by_model["openai/gpt-4o"]["cost_usd"], 4) == 0.026
    assert by_model["openai/gpt-4o"]["runs"] == 2
    assert by_model["openai/gpt-4o"]["tokens"] == 1800
    assert by_model["ollama/llama3.1"]["local_runs"] == 1

    # Trend carries every routed run + the joined goal/local flag (order-free:
    # rapid inserts can share a time.time() tick, so don't assert sequence).
    assert len(stats["trend"]) == 4
    assert {t["run_id"] for t in stats["trend"]} == {"run-a", "run-b", "run-c", "run-d"}
    goals = {t["run_id"]: t["goal"] for t in stats["trend"]}
    locals_ = {t["run_id"]: t["local"] for t in stats["trend"]}
    assert goals["run-a"] == "cloud task"
    assert locals_["run-d"] is True and locals_["run-a"] is False
    s.close()


def test_routing_cost_stats_empty(tmp_path):
    s = Store(tmp_path / "r.db")
    stats = s.routing_cost_stats()
    assert stats["total_cost_usd"] == 0.0 and stats["total_runs"] == 0
    assert stats["by_model"] == [] and stats["trend"] == []
    s.close()
