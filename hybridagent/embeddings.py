"""Embeddings — offline-deterministic mock by default, real provider on opt-in.

Mirrors :class:`LLMClient`'s mode model (env ``PRAXIS_EMBED``):

* ``auto`` (default) — use the configured embedding model if one is set
  (``agents.defaults.embedModel`` in ``praxis.json``), else the offline mock;
* ``mock`` — always the deterministic feature-hashing embedder (no deps, no net);
* ``real`` — always call the configured embeddings provider.

The mock is a signed feature-hashing embedder: tokens are hashed into a fixed-dim
vector and L2-normalised, so cosine similarity tracks lexical overlap. It is
deterministic (great for tests and fully-offline RAG) and good enough to make
retrieval useful before a real embedding model is wired in.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field

from . import config as cfg
from .logging_util import get_logger
from .providers import CATALOG, embed as provider_embed
from .router import ModelRouter, classify_sensitivity, SENSITIVE

_log = get_logger("praxis.embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9]+")
DEFAULT_DIM = 256


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class EmbeddingClient:
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_EMBED", "auto"))
    dim: int = DEFAULT_DIM

    def _effective_mode(self) -> str:
        if self.mode in ("mock", "real"):
            return self.mode
        return "real" if cfg.get_embed_model() else "mock"  # auto

    # ------------------------------------------------------------------ public
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._effective_mode() == "real" and not self._must_stay_local(texts):
            return self._embed_real(texts)
        return [self._mock_embed(t) for t in texts]

    def _must_stay_local(self, texts: list[str]) -> bool:
        """Keep sensitive content off a cloud embedder. If the configured embed
        model isn't local and any input is sensitive, fall back to the local
        mock embedder rather than transmitting the text (same invariant the
        router enforces for chat)."""
        model_ref = cfg.get_embed_model()
        if not model_ref or ModelRouter.is_local_ref(model_ref):
            return False
        if any(classify_sensitivity(t) == SENSITIVE for t in texts):
            _log.warning("sensitive content detected; embedding locally instead "
                         "of sending to cloud embedder '%s'", model_ref)
            return True
        return False

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    # -------------------------------------------------------------------- mock
    def _mock_embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokenize(text):
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    # -------------------------------------------------------------------- real
    def _embed_real(self, texts: list[str]) -> list[list[float]]:
        model_ref = cfg.get_embed_model()
        if not model_ref:
            raise RuntimeError(
                "No embedding model configured. Set agents.defaults.embedModel "
                "(e.g. 'ollama/nomic-embed-text') or use PRAXIS_EMBED=mock.")
        provider_id, model = cfg.split_model_ref(model_ref)
        provider = CATALOG.get(provider_id)
        if not provider:
            raise RuntimeError(f"Unknown embedding provider '{provider_id}'.")
        entry = cfg.provider_entry(provider_id) or {}
        api_key = cfg.resolve_api_key(provider_id)
        return provider_embed(provider=provider, model=model, texts=texts,
                              api_key=api_key, base_url=entry.get("baseUrl"))
