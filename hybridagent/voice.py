"""Voice as a configurable, operator-selectable agent capability.

Voice is a first-class backend chosen from agent config (``agents.voice`` in
praxis.json): the operator picks a **mode** (off / turn / realtime) and the
STT/TTS providers, exactly like ``agents.roles`` and ``agents.tiers``. Turn-based
and realtime are two implementations behind one :class:`VoiceBackend` interface,
so the governed agent loop and the broker are unchanged regardless of mode.

* **turn**     — implemented now: speech-to-text via the multimodal transcribe
  seam, text-to-speech via an OpenAI-compatible ``/audio/speech`` call, with an
  offline silent-WAV fallback so it degrades honestly without keys.
* **realtime** — a live governed session over a hand-rolled WebSocket. The
  browser streams microphone audio as PCM16 to a bridge: the OpenAI Realtime API
  when a realtime model + key are configured, otherwise an offline governed
  loopback. Both speak the same browser-facing event protocol.

Everything degrades safely: with nothing configured, ``turn`` still runs in an
offline "preview" mode and ``realtime`` is available once a realtime model is set
or ``PRAXIS_VOICE_REALTIME=1`` enables the loopback.
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import wave
from dataclasses import dataclass
from typing import Any

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
    realtime_url: str = ""
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
            realtime_url=rt.get("url", ""),
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
        audio: list[str] = []
        audio_mime = "audio/webm"
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
            elif etype == "audio":
                if ev.get("data"):
                    audio.append(str(ev["data"]))
                    audio_mime = ev.get("mime", audio_mime)
            elif etype == "stop":
                break
            elif etype == "interrupt":
                # Barge-in: the user starts talking over Praxis. Drop the buffered
                # turn and acknowledge so the client can stop playback.
                pending = []
                audio = []
                self._send({"type": "interrupted"})
            elif etype == "commit":
                text = " ".join(p for p in pending if p).strip()
                pending = []
                if not text and audio:
                    text = self._transcribe(audio, audio_mime)
                    if text:
                        self._send({"type": "transcript", "text": text})
                audio = []
                if not text:
                    continue
                self.messages.append({"role": "user", "content": text})
                self._respond()
                turns += 1
        self.conn.close()

    def _transcribe(self, chunks: list[str], mime: str) -> str:
        try:
            raw = b"".join(base64.b64decode(c) for c in chunks if c)
        except (ValueError, TypeError):
            return ""
        if not raw:
            return ""
        base = (mime or "").split(";")[0].strip().lower()
        if base in ("audio/pcm", "audio/l16", "audio/x-pcm"):
            # The browser streams raw little-endian PCM16; wrap it in a WAV
            # container so the STT seam (and any real provider) can decode it.
            raw = _pcm16_to_wav(raw, _rate_from_mime(mime))
            mime = "audio/wav"
        return transcribe_audio(raw, mime).text

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


_OAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


def _pcm16_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _rate_from_mime(mime: str, default: int = 24000) -> int:
    """Parse a sample rate from a mime like 'audio/pcm;rate=24000'."""
    for part in (mime or "").split(";")[1:]:
        part = part.strip()
        if part.startswith("rate="):
            try:
                return int(part[5:])
            except ValueError:
                return default
    return default


class OpenAIRealtimeUpstream:
    """Bridge the Praxis realtime channel to the OpenAI Realtime API.

    Relays user text/audio up and the model's transcript/text/audio down, and
    routes every model function call through the GovernanceBroker — read/draft
    execute, send/destructive are held for approval, disallowed/killed are
    denied — returning the governed outcome to the model as a
    ``function_call_output``. Audio deltas (PCM16) are wrapped as WAV so the
    browser plays them with the same path as turn-based TTS. The browser-facing
    event protocol is identical to the loopback bridge, so the dashboard is
    unchanged.
    """

    def __init__(self, agent, conn, *, model: str, api_key: str,
                 base_url: str | None = None, system: str | None = None) -> None:
        self.agent = agent
        self.conn = conn              # browser-facing WebSocketConn
        self.model = model
        self.api_key = api_key
        self.base_url = base_url or _OAI_REALTIME_URL
        self.system = system or _REALTIME_SYSTEM
        self.up: Any = None                # upstream WebSocketConn (OpenAI)
        self._up_lock = threading.Lock()

    def _down(self, obj: dict) -> None:
        try:
            self.conn.send_text(json.dumps(obj, default=str))
        except OSError:
            pass

    def _up_send(self, obj: dict) -> None:
        with self._up_lock:
            if self.up is not None:
                try:
                    self.up.send_text(json.dumps(obj))
                except OSError:
                    pass

    def _oai_tools(self) -> list[dict]:
        out: list[dict] = []
        for t in self.agent.registry.catalog():
            out.append({"type": "function", "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters
                        or {"type": "object", "properties": {}}})
        return out

    def run(self) -> None:
        from .wsutil import ws_connect
        try:
            self.up = ws_connect(
                f"{self.base_url}?model={self.model}",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "OpenAI-Beta": "realtime=v1"})
        except Exception as exc:  # connect/auth failure -> tell the browser
            self._down({"type": "error", "error": f"realtime connect failed: {exc}"})
            self._down({"type": "done"})
            return
        self._up_send({"type": "session.update", "session": {
            "instructions": self.system, "tools": self._oai_tools(),
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16", "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": None}})  # push-to-talk: client commits explicitly
        self._down({"type": "ready", "mode": "openai"})
        up_thread = threading.Thread(target=self._pump_upstream, daemon=True)
        up_thread.start()
        try:
            self._pump_browser()
        finally:
            with self._up_lock:
                if self.up is not None:
                    try:
                        self.up.close()
                    except OSError:
                        pass
            up_thread.join(timeout=3)

    def _pump_browser(self) -> None:
        from .wsutil import OP_CLOSE, OP_PING
        pending_audio = False  # only commit the input buffer when audio was sent
        while True:
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
                self._up_send({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text",
                                 "text": str(ev.get("text", ""))}]}})
            elif etype == "audio":
                self._up_send({"type": "input_audio_buffer.append",
                               "audio": ev.get("data", "")})
                pending_audio = True
            elif etype == "commit":
                # Server VAD is off (push-to-talk), so finalize the audio buffer
                # ourselves before requesting a response. Committing an empty
                # buffer is an upstream error, so only do it when audio was sent.
                if pending_audio:
                    self._up_send({"type": "input_audio_buffer.commit"})
                    pending_audio = False
                self._up_send({"type": "response.create"})
            elif etype == "stop":
                break

    def _pump_upstream(self) -> None:
        from .wsutil import OP_CLOSE, OP_PING
        audio: list[str] = []
        try:
            while True:
                try:
                    frame = self.up.recv() if self.up is not None else None
                except OSError:
                    break
                if frame is None:
                    break
                opcode, data = frame
                if opcode == OP_CLOSE:
                    break
                if opcode == OP_PING:
                    with self._up_lock:
                        if self.up is not None:
                            self.up.pong(data)
                    continue
                try:
                    ev = json.loads(data.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._on_upstream_event(ev, audio)
        except Exception as exc:  # never let the bridge thread die silently
            self._down({"type": "error", "error": str(exc)})
        finally:
            # Unblock the browser-side pump so the session can't hang.
            try:
                self.conn.close()
            except OSError:
                pass

    def _on_upstream_event(self, ev: dict, audio: list[str]) -> None:
        etype = ev.get("type", "")
        if etype in ("response.text.delta", "response.audio_transcript.delta"):
            self._down({"type": "delta", "text": ev.get("delta", "")})
        elif etype == "response.audio.delta":
            if ev.get("delta"):
                audio.append(ev["delta"])
        elif etype == "response.audio.done":
            self._flush_audio(audio)
            audio.clear()
        elif etype == "response.function_call_arguments.done":
            self._handle_function_call(ev)
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._down({"type": "transcript", "text": ev.get("transcript", "")})
        elif etype == "response.done":
            self._down({"type": "done"})
        elif etype == "error":
            self._down({"type": "error", "error": str(ev.get("error", ev))})

    def _flush_audio(self, chunks: list[str]) -> None:
        try:
            pcm = b"".join(base64.b64decode(c) for c in chunks if c)
        except (ValueError, TypeError):
            return
        if not pcm:
            return
        wav = _pcm16_to_wav(pcm)
        self._down({"type": "audio", "mime": "audio/wav",
                    "data": base64.b64encode(wav).decode()})

    def _function_output(self, call_id: str, output: str) -> None:
        self._up_send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": output}})
        self._up_send({"type": "response.create"})

    def _handle_function_call(self, ev: dict) -> None:
        from .broker import Verdict
        from .validation import ValidationError, validate_tool_args
        name = str(ev.get("name", ""))
        call_id = str(ev.get("call_id") or ev.get("id", ""))
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        tool = self.agent.registry.get(name)
        broker = self.agent.broker
        if tool is None:
            self._down({"type": "denied", "tool": name, "reason": "unknown tool"})
            self._function_output(call_id, f"ERROR: unknown tool '{name}'")
            return
        try:
            validate_tool_args(tool, args)
        except ValidationError as exc:
            self._down({"type": "denied", "tool": name, "risk": tool.risk.value,
                        "reason": f"schema: {exc}"})
            self._function_output(call_id, f"SCHEMA-DENIED: {exc}")
            return
        decision = broker.authorize(
            actor="praxis-voice", tool=name, risk=tool.risk, args=args,
            preview=f"{name}({args})", provenance="voice",
            rationale=f"Realtime requested {tool.risk.value} tool '{name}'.")
        self._down({"type": "tool_call", "tool": name, "risk": tool.risk.value,
                    "verdict": decision.verdict.value})
        if decision.verdict is Verdict.ALLOW:
            try:
                result = str(tool.run(**args))
            except Exception as exc:
                result = f"ERROR: {exc}"
            safe = broker.redact(result)
            self._down({"type": "tool_result", "tool": name, "preview": safe[:240]})
            self._function_output(call_id, safe)
        elif decision.verdict is Verdict.NEEDS_APPROVAL:
            self._down({"type": "approval", "tool": name, "risk": tool.risk.value,
                        "approval_id": decision.approval_id})
            self._function_output(
                call_id,
                f"HELD for approval (id={decision.approval_id}); not executed.")
        else:
            reason = broker.redact(decision.reason)
            self._down({"type": "denied", "tool": name, "risk": tool.risk.value,
                        "reason": reason})
            self._function_output(call_id, f"DENIED: {reason}")


def run_realtime(agent, conn) -> None:
    """Pick the realtime upstream: the OpenAI Realtime bridge when a realtime
    model + key are configured, otherwise the offline governed loopback."""
    config = VoiceConfig.load()
    if config.realtime_provider and config.realtime_model:
        api_key = cfg.resolve_api_key(config.realtime_provider)
        if api_key:
            OpenAIRealtimeUpstream(
                agent, conn, model=config.realtime_model, api_key=api_key,
                base_url=config.realtime_url or None).run()
            return
    RealtimeBridge(agent, conn).run()
