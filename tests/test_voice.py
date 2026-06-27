"""Tests for the voice backend, config, and graceful degradation."""

from hybridagent import config as cfg
from hybridagent import voice


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_voice_status_defaults(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    s = voice.voice_status()
    assert s["mode"] == "off"
    modes = {m["id"]: m for m in s["modes"]}
    assert modes["off"]["available"] and modes["turn"]["available"]
    assert modes["realtime"]["available"] is False
    # turn is available but degraded with no STT/TTS configured.
    assert modes["turn"]["degraded"] is True


def test_voice_mode_roundtrip(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg.set_voice_mode("turn")
    assert voice.VoiceConfig.load().mode == "turn"
    assert voice.voice_status()["mode"] == "turn"
    assert isinstance(voice.get_voice_backend(), voice.TurnBasedVoice)


def test_invalid_mode_falls_back_off(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg.set_voice_config({"mode": "bogus"})
    assert voice.VoiceConfig.load().mode == "off"
    assert isinstance(voice.get_voice_backend(), voice.OffVoice)


def test_transcribe_offline_returns_metadata(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    res = voice.transcribe_audio(b"\x00\x01\x02fake-audio", "audio/webm")
    # Honest metadata, no fabricated transcript, flagged offline.
    assert isinstance(res.text, str) and res.text
    assert "offline" in res.detail


def test_synthesize_offline_returns_silent_wav(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    res = voice.synthesize_text("hello world")
    assert res.mime == "audio/wav"
    assert res.audio[:4] == b"RIFF"          # a valid WAV
    assert "offline" in res.detail


def test_realtime_unavailable_with_reason(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    rt = voice.RealtimeVoice(voice.VoiceConfig.load())
    assert rt.available() is False
    assert "realtime" in rt.reason().lower()


def test_realtime_available_gating(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert voice.realtime_available() is False
    monkeypatch.setenv("PRAXIS_VOICE_REALTIME", "1")
    assert voice.realtime_available() is True
    modes = {m["id"]: m for m in voice.voice_status()["modes"]}
    assert modes["realtime"]["available"] is True
