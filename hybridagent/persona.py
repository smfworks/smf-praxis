"""User persona / model — durable preferences that shape Praxis behavior.

Stored under ``agents.persona`` in praxis.json and mirrored into durable memory
so retrieval and system prompts can ground on "who you are".
"""
from __future__ import annotations

from typing import Any

from . import config as cfg

_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "role": "",
    "tone": "professional, concise, helpful",
    "never_do": [],
    "work_hours": "",
    "preferred_channel": "dashboard",
    "timezone": "",
    "goals": "",
    "onboarding_complete": False,
    "first_win_complete": False,
}


def load_persona() -> dict:
    conf = cfg.load_config()
    raw = ((conf.get("agents") or {}).get("persona") or {})
    out = dict(_DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out or k in _DEFAULTS:
                out[k] = v
            else:
                out[k] = v
    if not isinstance(out.get("never_do"), list):
        nd = out.get("never_do") or []
        out["never_do"] = [nd] if isinstance(nd, str) and nd else list(nd or [])
    return out


def save_persona(updates: dict) -> dict:
    conf = cfg.load_config()
    agents = conf.setdefault("agents", {})
    persona = dict(agents.get("persona") or {})
    for k, v in (updates or {}).items():
        if v is None:
            continue
        if k == "never_do" and isinstance(v, str):
            persona[k] = [x.strip() for x in v.split(",") if x.strip()]
        else:
            persona[k] = v
    agents["persona"] = persona
    cfg.save_config(conf)
    return load_persona()


def persona_system_prefix() -> str:
    """Short system-prompt fragment from the persona, or empty."""
    p = load_persona()
    if not any(p.get(k) for k in ("display_name", "role", "tone", "never_do", "goals")):
        return ""
    parts = ["User model (durable preferences):"]
    if p.get("display_name"):
        parts.append(f"- Name: {p['display_name']}")
    if p.get("role"):
        parts.append(f"- Role: {p['role']}")
    if p.get("tone"):
        parts.append(f"- Preferred tone: {p['tone']}")
    if p.get("goals"):
        parts.append(f"- Goals: {p['goals']}")
    if p.get("work_hours"):
        parts.append(f"- Work hours: {p['work_hours']}")
    never = p.get("never_do") or []
    if never:
        parts.append("- Never: " + "; ".join(str(x) for x in never))
    parts.append("Honor these unless the user overrides in the current turn.")
    return "\n".join(parts)


def mirror_to_memory(memory) -> None:
    """Best-effort write of persona summary into durable memory."""
    if memory is None:
        return
    p = load_persona()
    if not p.get("display_name") and not p.get("role"):
        return
    text = persona_system_prefix() or f"User: {p.get('display_name') or 'operator'}"
    try:
        if hasattr(memory, "add_durable"):
            memory.add_durable(text, provenance="persona")
        elif hasattr(memory, "note_working"):
            memory.note_working(text, provenance="persona")
    except Exception:  # noqa: BLE001
        pass
