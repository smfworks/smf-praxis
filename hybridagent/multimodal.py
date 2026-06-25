"""Multimodal intake — turn images, audio, and video into retrievable text.

Like the LLM and embedding clients, this is **offline-first** (env ``PRAXIS_MM``
= auto/mock/real):

* **mock** (default offline) emits honest *metadata* — filename, size, and
  dimensions/duration when cheaply available — explicitly labelled as a mock. It
  never invents visual or spoken content, so nothing hallucinated enters memory
  or the knowledge base.
* **real** routes images to a vision model (router role ``vision``) and audio to a
  speech-to-text endpoint (router role ``transcribe``, or a local Whisper install
  if present). Video needs optional frame/audio tooling.

The extracted text flows through the same RAG/perception pipeline, so multimodal
content is retrieved and injection-screened exactly like documents.
"""
from __future__ import annotations

import base64
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .ingest import ExtractedDoc, MissingDependencyError
from .logging_util import get_logger
from .providers import CATALOG, chat_multimodal, transcribe as provider_transcribe
from .router import ModelRouter

_log = get_logger("praxis.multimodal")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | AUDIO_SUFFIXES | VIDEO_SUFFIXES


def is_media(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MEDIA_SUFFIXES


def image_to_part(path: str | Path) -> dict:
    p = Path(path)
    media_type = mimetypes.guess_type(p.name)[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode()
    return {"media_type": media_type, "data": data}


@dataclass
class MediaClient:
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_MM", "auto"))
    router: ModelRouter = field(default_factory=ModelRouter)

    def _effective_mode(self) -> str:
        if self.mode in ("mock", "real"):
            return self.mode
        return "real" if cfg.is_configured() else "mock"  # auto

    def is_media(self, path: str | Path) -> bool:
        return is_media(path)

    # ----------------------------------------------------------------- dispatch
    def process(self, path: str | Path) -> ExtractedDoc:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        suffix = p.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            text, kind = self.describe_image(p), "image"
        elif suffix in AUDIO_SUFFIXES:
            text, kind = self.transcribe_audio(p), "audio"
        elif suffix in VIDEO_SUFFIXES:
            text, kind = self.process_video(p), "video"
        else:
            raise ValueError(f"not a media file: {suffix}")
        return ExtractedDoc(text=text, source=p.name, kind=kind,
                            metadata={"path": str(p), "suffix": suffix})

    # -------------------------------------------------------------------- image
    def describe_image(self, path: str | Path,
                       prompt: str = "Describe this image in detail for search "
                                     "and retrieval. List any visible text.") -> str:
        if self._effective_mode() == "mock":
            return self._image_meta(path)
        provider, model, entry = self._resolve("vision")
        key = cfg.resolve_api_key(provider.id)
        caption = chat_multimodal(provider, model, prompt, [image_to_part(path)],
                                  None, key, entry.get("baseUrl"))
        return f"[image: {Path(path).name}]\n{caption}"

    # -------------------------------------------------------------------- audio
    def transcribe_audio(self, path: str | Path) -> str:
        if self._effective_mode() == "mock":
            return self._audio_meta(path)
        # Prefer a local Whisper install (fully local STT) when available.
        try:
            import whisper  # type: ignore
            model = whisper.load_model(os.environ.get("PRAXIS_WHISPER", "base"))
            result = model.transcribe(str(path))
            return f"[audio: {Path(path).name}]\n{result.get('text', '').strip()}"
        except Exception:
            pass
        provider, model_id, entry = self._resolve("transcribe")
        key = cfg.resolve_api_key(provider.id)
        text = provider_transcribe(provider, model_id, str(path), key,
                                   entry.get("baseUrl"))
        return f"[audio: {Path(path).name}]\n{text}"

    # -------------------------------------------------------------------- video
    def process_video(self, path: str | Path) -> str:
        if self._effective_mode() == "mock":
            return self._video_meta(path)
        try:
            import cv2  # type: ignore
        except Exception:
            raise MissingDependencyError(
                "real video processing needs frame tooling: "
                'pip install "praxis-agent[multimodal]" (opencv-python)')
        cap = cv2.VideoCapture(str(path))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        captions = []
        for frac in (0.1, 0.5, 0.9):                  # sample 3 keyframes
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_count * frac))
            ok, frame = cap.read()
            if not ok:
                continue
            tmp = Path(path).with_suffix(f".frame{int(frac*100)}.jpg")
            cv2.imwrite(str(tmp), frame)
            try:
                captions.append(self.describe_image(tmp))
            finally:
                tmp.unlink(missing_ok=True)
        cap.release()
        return f"[video: {Path(path).name}]\n" + "\n".join(captions)

    # ------------------------------------------------------------ mock metadata
    @staticmethod
    def _size(path: str | Path) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _image_meta(self, path: str | Path) -> str:
        name, size = Path(path).name, self._size(path)
        dims = ""
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                dims = f" {im.width}x{im.height} {im.mode}"
        except Exception:
            pass
        return (f"[image: {name}]{dims}, {size} bytes. Offline mock — no visual "
                f"description generated. Configure a vision model (role 'vision') "
                f"and set PRAXIS_MM=real to caption images.")

    def _audio_meta(self, path: str | Path) -> str:
        name, size = Path(path).name, self._size(path)
        dur = ""
        if Path(path).suffix.lower() == ".wav":
            try:
                import wave
                with wave.open(str(path), "rb") as w:
                    seconds = w.getnframes() / float(w.getframerate() or 1)
                    dur = f" ~{seconds:.1f}s"
            except Exception:
                pass
        return (f"[audio: {name}]{dur}, {size} bytes. Offline mock — no transcript "
                f"generated. Configure speech-to-text (local Whisper or role "
                f"'transcribe') and set PRAXIS_MM=real.")

    def _video_meta(self, path: str | Path) -> str:
        name, size = Path(path).name, self._size(path)
        return (f"[video: {name}], {size} bytes. Offline mock — no frame or audio "
                f"analysis. Install praxis-agent[multimodal] and set PRAXIS_MM=real.")

    # --------------------------------------------------------------- resolution
    def _resolve(self, role: str):
        ref = (self.router.role_model(role) or cfg.get_default_model())
        if not ref:
            raise RuntimeError(
                f"No model configured for role '{role}'. Set agents.roles.{role} "
                f"in praxis.json, or use PRAXIS_MM=mock.")
        provider_id, model = cfg.split_model_ref(ref)
        provider = CATALOG.get(provider_id)
        if not provider or not model:
            raise RuntimeError(f"Bad model ref for role '{role}': {ref!r}")
        return provider, model, (cfg.provider_entry(provider_id) or {})
