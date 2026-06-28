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
