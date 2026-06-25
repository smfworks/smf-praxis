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
