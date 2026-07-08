"""Client-aligned intent routing for friendly default (Auto) mode.

Maps free-text user messages to a dashboard execution mode so newcomers
do not have to learn the Chat / Ask / Research / Do / Agent taxonomy.
"""
from __future__ import annotations

import re
from typing import Literal

Mode = Literal["chat", "ask", "research", "do", "agent"]

_URL_RE = re.compile(r"https?://\S+", re.I)
_RESEARCH_RE = re.compile(
    r"\b(research|look up|search (the )?(web|internet)|find (articles|sources)|"
    r"latest (news|on)|summarize (this|the) (url|page|link|article))\b",
    re.I,
)
_ASK_RE = re.compile(
    r"\b(according to (my|the) (kb|knowledge|notes|wiki|memory)|"
    r"from (my|the) (knowledge|notes|wiki|docs)|cite (sources|your sources)|"
    r"grounded|in (the )?knowledge base)\b",
    re.I,
)
_DO_RE = re.compile(
    r"\b(queue (this|a )?task|run this (as )?(a )?(background|autonomous)|"
    r"work on this (in the )?background|schedule this|add to (the )?board|"
    r"every (day|morning|evening|hour)|daily at|cron)\b",
    re.I,
)
_AGENT_RE = re.compile(
    r"\b(use tools|browse|open (the )?browser|click|fill (the )?form|"
    r"send (the )?email|delete|call (the )?agent|delegate)\b",
    re.I,
)


def detect_intent(text: str, *, explicit: Mode | None = None) -> Mode:
    """Return the mode to use for ``text``.

    Parameters
    ----------
    text:
        User message.
    explicit:
        If set (user forced a mode from the More menu), that mode wins.
        Pass ``None`` for Auto routing.
    """
    if explicit is not None:
        return explicit
    return detect_intent_auto(text)


def detect_intent_auto(text: str) -> Mode:
    """Pure auto routing from message text."""
    t = (text or "").strip()
    if not t:
        return "chat"
    if _DO_RE.search(t):
        return "do"
    if _ASK_RE.search(t):
        return "ask"
    if _URL_RE.search(t) or _RESEARCH_RE.search(t):
        return "research"
    if _AGENT_RE.search(t):
        return "agent"
    return "chat"
