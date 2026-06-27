"""Voice as a configurable, operator-selectable agent capability.

Voice is a first-class backend chosen from agent config (``agents.voice`` in
praxis.json): the operator picks a **mode** (off / turn / realtime) and the
STT/TTS providers, exactly like ``agents.roles`` and ``agents.tiers``. Turn-based
and realtime are two implementations behind one :class:`VoiceBackend` interface,
so the governed agent loop and the broker are unchanged regardless of mode.

* **turn**     — implemented now: speech-to-text via the multimodal transcribe
  seam, text-to-speech via an OpenAI-compatible ``/audio/speech`` call, with an
  offline silent-WAV fallback so it degrades honestly without keys.
* **realtime** — registered but reports *unavailable* until a WebSocket/WebRTC
  bridge is wired, so the selector can advertise it without it being pickable.

Everything degrades safely: with nothing configured, ``turn`` still runs in an
offline "preview" mode and ``realtime`` stays disabled.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import wave
from dataclasses import dataclass

from . import config as cfg

OFF = "off"
TURN = "turn"
REALTIME = "realtime"
MODES = (OFF, TURN, REALTIME)


@dataclass
class VoiceConfig:
    mode: str = OFF
    stt_provider: str = ""
    stt_model: str = ""
    tts_provider: str = ""
    tts_model: str = ""
    tts_voice: str = "alloy"
    realtime_provider: str = ""
    realtime_model: str = ""
    push_to_talk: bool = True

    @classmethod
    def load(cls) -> "VoiceConfig":
        v = cfg.get_voice_config()
        stt = v.get("stt", {}) or {}
        tts = v.get("tts", {}) or {}
        rt = v.get("realtime", {}) or {}
        mode = v.get("mode", OFF)
        if mode not in MODES:
            mode = OFF
        return cls(
            mode=mode,
            stt_provider=stt.get("provider", ""), stt_model=stt.get("model", ""),
            tts_provider=tts.get("provider", ""), tts_model=tts.get("model", ""),
            tts_voice=tts.get("voice", "alloy"),
            realtime_provider=rt.get("provider", ""),
            realtime_model=rt.get("model", ""),
            push_to_talk=bool(v.get("pushToTalk", True)),
        )


@dataclass
class VoiceResult:
    text: str = ""
    audio: bytes = b""
    mime: str = ""
    detail: str = ""


def _silent_wav(seconds: float = 0.25, rate: int = 8000) -> bytes:
    """A tiny valid silent WAV — the honest offline placeholder for TTS."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _ext_for(mime: str) -> str:
    return {
        "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp4": ".mp4",
        "audio/m4a": ".m4a",
    }.get((mime or "").split(";")[0].strip(), ".webm")


class VoiceBackend:
    mode = OFF

    def __init__(self, config: VoiceConfig | None = None) -> None:
        self.config = config or VoiceConfig.load()

    def available(self) -> bool:
        return False

    def degraded(self) -> bool:
        return False

    def reason(self) -> str:
        return ""

    def transcribe(self, audio: bytes, mime: str = "audio/webm") -> VoiceResult:
        raise NotImplementedError

    def synthesize(self, text: str) -> VoiceResult:
        raise NotImplementedError


class OffVoice(VoiceBackend):
    mode = OFF

    def available(self) -> bool:
        return True


class TurnBasedVoice(VoiceBackend):
    mode = TURN

    def available(self) -> bool:
        return True

    def real_stt(self) -> bool:
        return bool(self.config.stt_provider and self.config.stt_model)

    def real_tts(self) -> bool:
        return bool(self.config.tts_provider and self.config.tts_model)

    def degraded(self) -> bool:
        return not (self.real_stt() and self.real_tts())

    def reason(self) -> str:
        missing = []
        if not self.real_stt():
            missing.append("STT (agents.voice.stt)")
        if not self.real_tts():
            missing.append("TTS (agents.voice.tts)")
        if not missing:
            return ""
        return "Offline preview — configure " + " and ".join(missing)

    def transcribe(self, audio: bytes, mime: str = "audio/webm") -> VoiceResult:
        from .multimodal import MediaClient
        tmp = tempfile.NamedTemporaryFile(suffix=_ext_for(mime), delete=False)
        try:
            tmp.write(audio)
            tmp.close()
            text = MediaClient().transcribe_audio(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        return VoiceResult(text=text, detail="stt" if self.real_stt() else "stt-offline")

    def synthesize(self, text: str) -> VoiceResult:
        c = self.config
        if c.tts_provider and c.tts_model:
            from .providers import CATALOG, synthesize_speech
            provider = CATALOG.get(c.tts_provider)
            if provider is not None:
                api_key = cfg.resolve_api_key(c.tts_provider)
                entry = cfg.provider_entry(c.tts_provider) or {}
                if not (provider.needs_key and not api_key):
                    try:
                        audio, mime = synthesize_speech(
                            provider, c.tts_model, text,
                            voice=c.tts_voice or "alloy", api_key=api_key,
                            base_url=entry.get("baseUrl"))
                        return VoiceResult(audio=audio, mime=mime, detail="tts")
                    except RuntimeError as exc:
                        return VoiceResult(audio=_silent_wav(), mime="audio/wav",
                                           detail=f"tts-offline ({exc})")
        return VoiceResult(audio=_silent_wav(), mime="audio/wav",
                           detail="tts-offline (no TTS provider configured)")


def realtime_available(config: "VoiceConfig | None" = None) -> bool:
    """Realtime is selectable when a realtime model is configured, or when the
    offline governed loopback is explicitly enabled (PRAXIS_VOICE_REALTIME=1)."""
    config = config or VoiceConfig.load()
    if os.environ.get("PRAXIS_VOICE_REALTIME", "").lower() in ("1", "true", "on"):
        return True
    return bool(config.realtime_provider and config.realtime_model)


class RealtimeVoice(VoiceBackend):
    mode = REALTIME

    def available(self) -> bool:
        return realtime_available(self.config)

    def reason(self) -> str:
        if self.available():
            return "Live governed voice session over WebSocket."
        return ("Realtime needs a realtime model + bridge — set agents.voice."
                "realtime, or PRAXIS_VOICE_REALTIME=1 for the offline governed "
                "loopback, then select it here.")


def get_voice_backend(config: VoiceConfig | None = None) -> VoiceBackend:
    config = config or VoiceConfig.load()
    if config.mode == REALTIME:
        return RealtimeVoice(config)
    if config.mode == TURN:
        return TurnBasedVoice(config)
    return OffVoice(config)


def voice_status() -> dict:
    """Selector payload for the dashboard: current mode + which modes are
    available (graceful degradation), mirroring the provider picker."""
    config = VoiceConfig.load()
    turn = TurnBasedVoice(config)
    realtime = RealtimeVoice(config)
    return {
        "mode": config.mode,
        "push_to_talk": config.push_to_talk,
        "modes": [
            {"id": OFF, "label": "Off", "available": True,
             "degraded": False, "reason": ""},
            {"id": TURN, "label": "Turn-based", "available": True,
             "degraded": turn.degraded(), "reason": turn.reason()},
            {"id": REALTIME, "label": "Realtime", "available": realtime.available(),
             "degraded": False, "reason": realtime.reason()},
        ],
        "stt": {"provider": config.stt_provider, "model": config.stt_model},
        "tts": {"provider": config.tts_provider, "model": config.tts_model,
                "voice": config.tts_voice},
    }


def transcribe_audio(audio: bytes, mime: str = "audio/webm") -> VoiceResult:
    """Module convenience: speech-to-text via the turn-based backend."""
    return TurnBasedVoice(VoiceConfig.load()).transcribe(audio, mime)


def synthesize_text(text: str) -> VoiceResult:
    """Module convenience: text-to-speech via the turn-based backend."""
    return TurnBasedVoice(VoiceConfig.load()).synthesize(text)


_REALTIME_SYSTEM = (
    "You are Praxis in a live voice session. Be concise and conversational. You "
    "can call tools: read and draft tools run automatically, while send and "
    "destructive actions are held for the user's approval — never claim a held or "
    "denied action ran."
)


class RealtimeBridge:
    """Drives a governed realtime turn over a WebSocket connection.

    JSON event protocol (text frames):
      client -> {type:"text", text} | {type:"commit"} | {type:"stop"}
      server -> {type:"ready"} | tool_call/tool_result/approval/denied/final
                | {type:"audio", mime, data(base64)} | {type:"done"}
                | {type:"error", error}

    Each ``commit`` runs the same :class:`~hybridagent.chat_agent.GovernedChatAgent`
    the Agent surface uses, so a live voice session is governed identically —
    consequential tools are still held for approval. The upstream today is the
    offline governed loopback; an OpenAI Realtime audio bridge can replace the
    per-commit responder behind this exact protocol with no client change.
    """

    MAX_TURNS = 50

    def __init__(self, agent, conn, system: str | None = None) -> None:
        self.agent = agent
        self.conn = conn
        self.system = system or _REALTIME_SYSTEM
        self.messages: list[dict] = []

    def _send(self, obj: dict) -> None:
        try:
            self.conn.send_text(json.dumps(obj, default=str))
        except OSError:
            pass

    def run(self) -> None:
        from .wsutil import OP_CLOSE, OP_PING
        self._send({"type": "ready", "mode": "loopback"})
        pending: list[str] = []
        turns = 0
        while turns < self.MAX_TURNS:
            frame = self.conn.recv()
            if frame is None:
                break
            opcode, data = frame
            if opcode == OP_CLOSE:
                break
            if opcode == OP_PING:
                self.conn.pong(data)
                continue
            try:
                ev = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            etype = ev.get("type")
            if etype == "text":
                pending.append(str(ev.get("text", "")))
            elif etype == "stop":
                break
            elif etype == "commit":
                text = " ".join(p for p in pending if p).strip()
                pending = []
                if not text:
                    continue
                self.messages.append({"role": "user", "content": text})
                self._respond()
                turns += 1
        self.conn.close()

    def _respond(self) -> None:
        from .chat_agent import GovernedChatAgent
        engine = GovernedChatAgent(
            self.agent.llm, self.agent.registry, self.agent.broker,
            memory=getattr(self.agent, "memory", None))
        final = ""
        try:
            for ev in engine.run(list(self.messages), system=self.system):
                self._send({"type": ev.type, **ev.data})
                if ev.type == "final":
                    final = ev.data.get("text", "")
        except Exception as exc:  # a model/tool failure must not kill the socket
            self._send({"type": "error", "error": str(exc)})
            self._send({"type": "done"})
            return
        if final:
            self.messages.append({"role": "assistant", "content": final})
            res = TurnBasedVoice(VoiceConfig.load()).synthesize(final)
            self._send({"type": "audio", "mime": res.mime,
                        "data": base64.b64encode(res.audio).decode(),
                        "detail": res.detail})
        self._send({"type": "done"})
