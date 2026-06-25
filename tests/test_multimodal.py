import wave

from hybridagent import config as cfg
from hybridagent import providers
from hybridagent.embeddings import EmbeddingClient
from hybridagent.multimodal import MediaClient, image_to_part, is_media
from hybridagent.persistence import Store
from hybridagent.rag import Rag


def _make_wav(path, seconds=1, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))


def test_is_media_detection():
    assert is_media("a.png") and is_media("a.mp3") and is_media("a.mp4")
    assert not is_media("a.txt") and not is_media("a.pdf")


def test_auto_mode_stays_mock_without_a_vision_model(tmp_path, monkeypatch):
    # A bare text model must NOT cause auto-mode to send an image to a chat model
    # (which would invent a caption). It must stay mock => honest metadata.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent import onboard
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")   # text only
    monkeypatch.delenv("PRAXIS_MM", raising=False)                   # auto
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    out = MediaClient().describe_image(p)
    assert "Offline mock" in out


def test_use_real_respects_role_model(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent import onboard
    onboard.run_noninteractive("openrouter", "openai/gpt-4o-mini")
    monkeypatch.delenv("PRAXIS_MM", raising=False)
    mc = MediaClient()
    assert mc._use_real("vision") is False                          # no vision role
    data = cfg.load_config()
    data["agents"]["roles"] = {"vision": "openai/gpt-4o"}
    cfg.save_config(data)
    assert mc._use_real("vision") is True                           # role configured


def test_image_to_part(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    part = image_to_part(p)
    assert part["media_type"] == "image/png"
    assert isinstance(part["data"], str) and part["data"]


def test_mock_image_describe_is_honest_metadata(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    out = MediaClient(mode="mock").describe_image(p)
    assert "[image: pic.png]" in out
    assert "Offline mock" in out                  # never fabricates content


def test_mock_audio_transcribe_reports_duration(tmp_path):
    p = tmp_path / "clip.wav"
    _make_wav(p, seconds=2)
    out = MediaClient(mode="mock").transcribe_audio(p)
    assert "[audio: clip.wav]" in out
    assert "~2.0s" in out and "Offline mock" in out


def test_mock_video_metadata(tmp_path):
    p = tmp_path / "movie.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    out = MediaClient(mode="mock").process_video(p)
    assert "[video: movie.mp4]" in out


def test_process_dispatches_by_kind(tmp_path):
    p = tmp_path / "s.wav"
    _make_wav(p)
    doc = MediaClient(mode="mock").process(p)
    assert doc.kind == "audio" and doc.source == "s.wav"


def test_chat_multimodal_openai_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers, payload, timeout, **k):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "a cat on a mat"}}]}

    monkeypatch.setattr(providers, "_post", fake_post)
    out = providers.chat_multimodal(
        providers.CATALOG["openai"], "gpt-4o", "describe",
        [{"media_type": "image/png", "data": "QUJD"}], None, "key")
    assert out == "a cat on a mat"
    content = captured["payload"]["messages"][-1]["content"]
    assert any(part.get("type") == "image_url" for part in content)


def test_chat_multimodal_anthropic_payload(monkeypatch):
    captured = {}

    def fake_post(url, headers, payload, timeout, **k):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "a dog"}]}

    monkeypatch.setattr(providers, "_post", fake_post)
    out = providers.chat_multimodal(
        providers.CATALOG["anthropic"], "claude-3-5-sonnet-latest", "describe",
        [{"media_type": "image/jpeg", "data": "QUJD"}], None, "key")
    assert out == "a dog"
    content = captured["payload"]["messages"][-1]["content"]
    assert any(part.get("type") == "image" for part in content)


def test_rag_ingests_media_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_MM", "mock")
    wav = tmp_path / "memo.wav"
    _make_wav(wav)
    rag = Rag(Store.open(), EmbeddingClient(mode="mock"))
    doc, n = rag.ingest_file(wav)
    assert doc.kind == "audio" and n >= 1
    assert rag.stats()["docs"] == 1
    hits = rag.retrieve("audio memo offline mock")
    assert hits and hits[0].source == "memo.wav"
