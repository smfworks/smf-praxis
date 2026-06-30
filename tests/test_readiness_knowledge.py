"""Out-of-the-box readiness, knowledge-source management, and cross-namespace
retrieval — the capabilities that make a fresh install usable from the dashboard.
"""
from hybridagent import config as cfg
from hybridagent import readiness
from hybridagent.persistence import Store
from hybridagent.rag import Rag


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_readiness_reports_all_checks(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    rep = readiness.readiness(Store.open())
    keys = {c["key"] for c in rep["checks"]}
    assert keys == {"model", "memory", "search", "wiki", "embed", "skills"}
    # Web research is ready out of the box via the keyless default.
    search = next(c for c in rep["checks"] if c["key"] == "search")
    assert search["status"] == "ok"
    # Memory recall is on by default.
    mem = next(c for c in rep["checks"] if c["key"] == "memory")
    assert mem["status"] == "ok"
    # A brand-new install has no knowledge sources yet -> 'off', not an error.
    wiki = next(c for c in rep["checks"] if c["key"] == "wiki")
    assert wiki["status"] == "off"


def test_readiness_render_is_human_readable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    text = readiness.render(Store.open())
    assert "Praxis readiness:" in text
    assert "Web research" in text
    assert "Knowledge base" in text


def test_list_namespaces_enumerates_repositories(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    rag = Rag(store)
    rag.ingest_text("alpha content about governance", "d1", ns="docs")
    rag.ingest_text("beta content about retrieval", "d2", ns="research")
    assert set(store.list_namespaces()) == {"docs", "research"}


def test_retrieve_all_ns_spans_every_repository(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    store = Store.open()
    rag = Rag(store)
    # Two separate RAG repositories (namespaces), neither the default 'kb'.
    rag.ingest_text(
        "The governance broker holds SEND and DESTRUCTIVE actions for approval.",
        "gov", ns="docs")
    rag.ingest_text(
        "Hybrid retrieval fuses BM25 with embeddings via Reciprocal Rank Fusion.",
        "ret", ns="research")
    # A plain retrieve on the default ns finds nothing...
    assert rag.retrieve("reciprocal rank fusion", k=3) == []
    # ...but retrieve_all_ns spans both repositories and surfaces the match.
    hits = rag.retrieve_all_ns("reciprocal rank fusion retrieval", k=3)
    assert hits, "expected a cross-namespace hit"
    assert any(h.source == "ret" for h in hits)


def test_daemon_sources_lifecycle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    doc = tmp_path / "corpus.txt"
    doc.write_text("Praxis is a governed autonomous AI colleague.", encoding="utf-8")
    d = Daemon.from_env()

    added = d.sources_add(str(doc), ns="kb", title="Intro")
    assert added.get("error") in (None, "")
    assert added["status"] in ("refreshed", "unchanged")

    listing = d.sources_list()
    assert len(listing["sources"]) == 1
    assert listing["stats"]["chunks"] >= 1

    sid = listing["sources"][0]["source_id"]
    refreshed = d.sources_refresh(sid)
    assert "error" not in refreshed or not refreshed["error"]

    deleted = d.sources_delete(sid)
    assert deleted["deleted"] is True
    assert len(d.sources_list()["sources"]) == 0


def test_daemon_sources_add_rejects_empty_uri(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    assert "error" in d.sources_add("")


def test_research_synthesizes_cited_answer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import search
    from hybridagent.daemon import Daemon
    from hybridagent.search import SearchResult

    def fake_web_search(q, max_results=5):
        return [
            SearchResult("RRF", "https://ex.com/rrf",
                         "Reciprocal Rank Fusion combines ranked lists by "
                         "summing one over k plus rank."),
            SearchResult("BM25", "https://ex.com/bm25",
                         "BM25 is a lexical ranking function."),
        ]
    # Patch the symbol the daemon imports at call time.
    monkeypatch.setattr(search, "web_search", fake_web_search)
    d = Daemon.from_env()
    res = d.research("how does reciprocal rank fusion work")
    assert res["abstained"] is False
    assert res["citations"], "expected at least one citation"
    assert any("ex.com" in u for u in [r["url"] for r in res["results"]])


def test_research_handles_no_results(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import search
    from hybridagent.daemon import Daemon
    monkeypatch.setattr(search, "web_search", lambda q, max_results=5: [])
    d = Daemon.from_env()
    res = d.research("a query with no hits")
    assert res["abstained"] is True
    assert res["results"] == []


def test_research_rejects_empty_query(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    assert "error" in d.research("")


def test_bootstrap_seeds_starter_knowledge(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import bootstrap, config
    from hybridagent.persistence import Store
    store = Store.open()
    res = bootstrap.run(store)
    assert res["knowledge"]["seeded"] is True
    assert res["knowledge"]["chunks"] > 0
    # Idempotent: a second run does not re-seed.
    again = bootstrap.run(store)
    assert again["knowledge"]["seeded"] is False
    # Defaults were written.
    conf = config.load_config()
    assert conf["agents"]["memoryRecall"] is True
    assert conf["agents"]["skillRecall"] is True


def test_query_knowledge_tool_is_registered_and_read_risk(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import RiskClass
    from hybridagent.tools import default_registry
    reg = default_registry()
    assert "query_knowledge" in reg.names()
    assert reg.get("query_knowledge").risk is RiskClass.READ


def test_query_knowledge_answers_from_seeded_kb(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import bootstrap
    from hybridagent.persistence import Store
    from hybridagent.real_tools import query_knowledge
    bootstrap.run(Store.open())
    out = query_knowledge("What is the Praxis governance broker?")
    assert "praxis-overview" in out
    assert "[query_knowledge]" in out


def test_recall_preview_surfaces_memory(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    d._ensure_agent()
    d.agent.memory.add_durable(
        "The project deadline is the end of Q3.", kind="fact",
        provenance="test")
    preview = d._recall_preview([{"role": "user", "content": "when is the deadline"}])
    assert isinstance(preview["memory"], list)
    assert isinstance(preview["skills"], list)


