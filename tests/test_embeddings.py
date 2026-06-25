from hybridagent.embeddings import EmbeddingClient, cosine
from hybridagent import config as cfg


def test_mock_embedding_is_deterministic():
    e = EmbeddingClient(mode="mock")
    a = e.embed_one("customer follow-up after the sync")
    b = e.embed_one("customer follow-up after the sync")
    assert a == b
    assert len(a) == EmbeddingClient().dim


def test_mock_embedding_similarity_tracks_overlap():
    e = EmbeddingClient(mode="mock")
    q = e.embed_one("quarterly revenue report for AdventHealth")
    near = e.embed_one("the AdventHealth quarterly revenue report")
    far = e.embed_one("how to bake sourdough bread at home")
    assert cosine(q, near) > cosine(q, far)


def test_cosine_edge_cases():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9


def test_auto_mode_is_mock_without_embed_model(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_EMBED", raising=False)
    assert EmbeddingClient()._effective_mode() == "mock"
    out = EmbeddingClient().embed([])
    assert out == []


def test_sensitive_text_never_sent_to_cloud_embedder(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_embed_model("openai/text-embedding-3-small")          # cloud embedder
    import hybridagent.embeddings as emb

    def _boom(**_):
        raise AssertionError("cloud embedder was called with sensitive content")

    monkeypatch.setattr(emb, "provider_embed", _boom)
    e = emb.EmbeddingClient(mode="real")
    out = e.embed(["server password: hunter2 do not share"])
    assert len(out) == 1 and len(out[0]) == e.dim                 # local mock vector


def test_nonsensitive_text_uses_real_embedder(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    cfg.set_embed_model("openai/text-embedding-3-small")
    import hybridagent.embeddings as emb
    monkeypatch.setattr(emb, "provider_embed", lambda **k: [[0.1, 0.2, 0.3]])
    e = emb.EmbeddingClient(mode="real")
    assert e.embed(["quarterly status update meeting notes"]) == [[0.1, 0.2, 0.3]]
