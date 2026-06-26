"""Structured JSON generation helpers (dependency-free, no circular imports).

Use these from any module that needs to ask a configured LLM for a single JSON
object and validate that required keys are present. The functions are kept here
(rather than in ``grounding.py``) so ``planner.py`` and ``grounding.py`` can both
import them without creating a circular import cycle.
"""
from __future__ import annotations

import json
import re

from .llm import LLMClient
from .router import classify_sensitivity

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _extract_json(text: str) -> dict | None:
    """Extract the first balanced JSON object, respecting strings/escapes so
    braces inside string values don't terminate the scan early."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def generate_json(llm: LLMClient, prompt: str, required_keys: list[str],
                  role: str = "planner", retries: int = 2,
                  sensitivity: str | None = None) -> dict:
    """Ask the LLM for a single JSON object containing ``required_keys``.

    The prompt is auto-tagged as sensitive when it contains likely secrets (SSN,
    credentials, etc.) so cloud providers are skipped by :class:`LLMClient`.
    """
    if sensitivity is None:
        sensitivity = classify_sensitivity(prompt)
    system = ("Respond with ONLY a single valid JSON object — no prose, no "
              "markdown code fences, no commentary.")
    last = ""
    for _ in range(retries + 1):
        out = llm.complete(prompt + "\nJSON:", system, role=role,
                           sensitivity=sensitivity)
        obj = _extract_json(out)
        if obj is not None and all(k in obj for k in required_keys):
            return obj
        last = out
    raise RuntimeError(
        f"model did not return JSON with keys {required_keys}: {last[:200]}")


def _tok(text: str) -> set[str]:
    """Lowercase token set used by grounding/verification helpers."""
    return set(_TOKEN_RE.findall((text or "").lower()))
