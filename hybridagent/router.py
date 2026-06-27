"""Model router — contextual model switching across local + cloud providers.

Picks which model handles a given call based on:

* **role** — planner / summarizer / vision / transcribe / general can each map to a
  different model (``agents.roles.<role>`` in ``praxis.json``), else the default.
* **data sensitivity** — content classified as sensitive (secrets, SSNs, card
  numbers, MIP-style "confidential" markers) is *never* routed to a cloud
  provider. The router returns local-only candidates, or the offline mock if no
  local model is configured. Private data stays on the user's hardware.
* **availability** — :class:`~hybridagent.llm.LLMClient` walks the returned
  candidate list in order, falling back to the next model when one errors.

Everything degrades safely: with nothing configured, every role resolves to the
offline mock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config as cfg

# Reused from the broker's redactor + a few high-signal PII / classification cues.
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|password|token|secret)\s*[:=]\s*\S+")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_LABEL_RE = re.compile(r"(?i)\b(highly\s+confidential|confidential|restricted|"
                       r"secret|internal only|mip:)\b")

SENSITIVE = "sensitive"
NORMAL = "normal"


def classify_sensitivity(text: str) -> str:
    """Conservative classifier: returns 'sensitive' or 'normal'.

    Deliberately does NOT trip on bare emails/phone numbers (every mail has
    those); callers may always pass an explicit sensitivity to force local.
    """
    if not text:
        return NORMAL
    if (_SECRET_RE.search(text) or _SSN_RE.search(text)
            or _LABEL_RE.search(text) or _CARD_RE.search(text)):
        return SENSITIVE
    return NORMAL


SIMPLE = "simple"
STANDARD = "standard"
HARD = "hard"

# Difficulty -> config key under agents.tiers.<tier>.
_TIER_FOR_DIFFICULTY = {SIMPLE: "fast", STANDARD: "balanced", HARD: "strong"}

# Cues that a request needs the strongest model (multi-step reasoning, code,
# analysis, design). Conservative + deterministic so routing stays predictable.
_HARD_CUES = re.compile(
    r"(?i)\b(analyz|architect|debug|refactor|prove|derive|optimi[sz]|"
    r"trade[- ]?off|step[- ]by[- ]step|reason through|root cause|algorithm|"
    r"complexity|strateg|implement|compare|evaluate|design (a|an|the)|"
    r"explain why|walk me through|plan (a|an|the|out))")
_SIMPLE_CUES = re.compile(
    r"(?i)^\s*(hi|hey|hello|yo|sup|thanks|thank you|ty|ok|okay|k|yes|yep|"
    r"no|nope|got it|cool|nice|great|good morning|good night|gm|gn)\b")


def classify_difficulty(text: str) -> str:
    """Heuristic request difficulty: 'simple' | 'standard' | 'hard'.

    Routes easy turns to a fast/cheap model and hard turns to the strongest
    configured model. Deterministic and conservative — when in doubt it returns
    'standard'.
    """
    t = (text or "").strip()
    if not t:
        return STANDARD
    words = t.split()
    if ("```" in t or _HARD_CUES.search(t) or len(t) > 600
            or len(words) > 80 or t.count("\n") >= 8):
        return HARD
    if _SIMPLE_CUES.search(t) or len(words) <= 4:
        return SIMPLE
    return STANDARD


@dataclass
class ModelRouter:
    def role_model(self, role: str) -> str | None:
        roles = cfg.load_config().get("agents", {}).get("roles", {})
        return roles.get(role)

    @staticmethod
    def is_local_ref(model_ref: str) -> bool:
        if not model_ref or model_ref == "mock":
            return model_ref == "mock"  # 'mock' is local-safe; '' is not
        provider_id, _ = cfg.split_model_ref(model_ref)
        if provider_id == "ollama":
            return True
        entry = cfg.provider_entry(provider_id) or {}
        base = (entry.get("baseUrl") or "").lower()
        return "127.0.0.1" in base or "localhost" in base or "://0.0.0.0" in base

    def tier_model(self, difficulty: str | None) -> str | None:
        """Model ref configured for a difficulty's tier (agents.tiers.<tier>)."""
        if not difficulty:
            return None
        tiers = cfg.load_config().get("agents", {}).get("tiers", {})
        return tiers.get(_TIER_FOR_DIFFICULTY.get(difficulty, "balanced"))

    def select(self, role: str = "general", sensitivity: str = NORMAL,
               difficulty: str | None = None) -> list[str]:
        """Ordered list of model refs to try (primary first, then fallbacks).

        When a difficulty tier is configured (``agents.tiers.{fast,balanced,
        strong}``), the tier model for the request's difficulty leads the list,
        then the role model, then the default — so hard turns prefer the
        strongest model and easy turns a fast one. Sensitivity still pins to a
        local model regardless of tier.
        """
        refs: list[str] = []
        for r in (self.tier_model(difficulty), self.role_model(role),
                  cfg.get_default_model()):
            if r and r not in refs:
                refs.append(r)
        if sensitivity == SENSITIVE:
            local = [r for r in refs if self.is_local_ref(r)]
            # Never fall through to a cloud model for sensitive content.
            return local if local else ["mock"]
        return refs

    def explain(self, role: str = "general", sensitivity: str = NORMAL,
                difficulty: str | None = None) -> dict:
        chosen = self.select(role, sensitivity, difficulty)
        return {
            "role": role,
            "sensitivity": sensitivity,
            "difficulty": difficulty,
            "candidates": chosen,
            "primary": chosen[0] if chosen else None,
            "primary_is_local": self.is_local_ref(chosen[0]) if chosen else False,
        }
