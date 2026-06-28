"""Tests for the voice backend, config, and graceful degradation."""

import base64
import json

from hybridagent import config as cfg
from hybridagent import voice
from hybridagent.wsutil import OP_TEXT


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


# --- Streaming mic audio (P8) -------------------------------------------------

class _RecordConn:
    """Captures send_text payloads (stands in for a WebSocketConn)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)


class _FakeBrowser(_RecordConn):
    """Feeds a scripted list of (opcode, bytes) frames to a pump loop."""

    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        super().__init__()
        self._frames = list(frames)

    def recv(self) -> tuple[int, bytes] | None:
        return self._frames.pop(0) if self._frames else None

    def pong(self, data: bytes = b"") -> None:
        pass

    def close(self) -> None:
        pass


def _text_frame(obj: dict) -> tuple[int, bytes]:
    return (OP_TEXT, json.dumps(obj).encode())


def test_rate_from_mime_parses_and_defaults():
    assert voice._rate_from_mime("audio/pcm;rate=16000") == 16000
    assert voice._rate_from_mime("audio/pcm") == 24000
    assert voice._rate_from_mime("audio/pcm;rate=bogus") == 24000


def test_pcm16_to_wav_is_valid_wav():
    pcm = b"\x01\x02" * 240  # 240 mono 16-bit samples
    wav = voice._pcm16_to_wav(pcm, rate=24000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + len(pcm)  # 44-byte header + payload


def test_realtime_bridge_transcribes_streamed_pcm(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    bridge = voice.RealtimeBridge(agent=None, conn=None)
    chunk = base64.b64encode(b"\x01\x00" * 1000).decode()
    text = bridge._transcribe([chunk, chunk], "audio/pcm;rate=24000")
    # Raw PCM is wrapped to WAV then run through the offline STT seam, which
    # returns honest metadata rather than a fabricated transcript.
    assert isinstance(text, str) and text


def test_openai_upstream_commits_audio_buffer_before_response():
    up = voice.OpenAIRealtimeUpstream(
        agent=None, conn=_RecordConn(), model="gpt-4o-realtime", api_key="k")
    up.conn = _FakeBrowser([
        _text_frame({"type": "audio", "data": "AAAB"}),
        _text_frame({"type": "commit"}),
        _text_frame({"type": "stop"}),
    ])
    up.up = _RecordConn()
    up._pump_browser()
    types = [json.loads(s)["type"] for s in up.up.sent]
    assert types == [
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "response.create",
    ]


def test_openai_upstream_text_commit_skips_buffer_commit():
    up = voice.OpenAIRealtimeUpstream(
        agent=None, conn=_RecordConn(), model="m", api_key="k")
    up.conn = _FakeBrowser([
        _text_frame({"type": "text", "text": "hello"}),
        _text_frame({"type": "commit"}),
        _text_frame({"type": "stop"}),
    ])
    up.up = _RecordConn()
    up._pump_browser()
    types = [json.loads(s)["type"] for s in up.up.sent]
    # Committing an empty input buffer is an upstream error; only audio commits.
    assert "input_audio_buffer.commit" not in types
    assert types[0] == "conversation.item.create"
    assert types[-1] == "response.create"


def test_openai_upstream_relays_input_transcript():
    up = voice.OpenAIRealtimeUpstream(
        agent=None, conn=_RecordConn(), model="m", api_key="k")
    up._on_upstream_event(
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hi there"}, [])
    sent = [json.loads(s) for s in up.conn.sent]
    assert {"type": "transcript", "text": "hi there"} in sent

