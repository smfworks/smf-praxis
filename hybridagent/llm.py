"""Deterministic mock LLM (offline). Swap _complete_real for a real backend."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field


@dataclass
class LLMClient:
    model: str = "praxis-local"
    mode: str = field(default_factory=lambda: os.environ.get("PRAXIS_LLM", "mock"))

    def complete(self, prompt: str, system: str | None = None) -> str:
        if self.mode == "real":
            return self._complete_real(prompt, system)
        seed = hashlib.sha256(((system or "") + prompt).encode()).hexdigest()[:8]
        head = prompt.strip().splitlines()[0][:100] if prompt.strip() else "(empty)"
        return f"[{seed}] {head}"

    def summarize(self, text: str) -> str:
        seed = hashlib.sha256(text.encode()).hexdigest()[:6]
        first = text.strip().splitlines()[0][:120] if text.strip() else ""
        return f"summary[{seed}]: {first}"

    def _complete_real(self, prompt: str, system: str | None) -> str:
        raise NotImplementedError(
            "Set PRAXIS_LLM=mock for offline runs, or implement _complete_real."
        )
