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
