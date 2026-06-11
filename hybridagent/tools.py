"""Narrow, explicit tools with risk classification (per both guides).

Read/draft tools are safe for autonomous use; send/destructive tools are
consequential and routed through the broker's approval queue. Tools here are
mock M365-style stand-ins; replace the callables with real broker/Graph calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .broker import RiskClass


@dataclass
class Tool:
    name: str
    risk: RiskClass
    description: str
    run: Callable[..., str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def of_risk(self, risk: RiskClass) -> list[str]:
        return [t.name for t in self._tools.values() if t.risk == risk]


# ----------------------------------------------------------------- mock tools
def _list_today_events(**_) -> str:
    return "3 events: 9:00 standup; 13:00 customer sync (AdventHealth); 16:00 1:1"


def _search_mail(query: str = "", **_) -> str:
    return f"4 messages matching '{query}': 1 high-priority from a customer re: follow-up"


def _get_file_text(name: str = "", **_) -> str:
    return f"[contents of {name or 'document'}]: project goals, milestones, owners"


def _create_email_draft(to=None, subject="", body="", **_) -> str:
    return f"DRAFT created -> to={to} subj='{subject}' ({len(body)} chars). Not sent."


def _save_private_note(text: str = "", **_) -> str:
    return f"private note saved ({len(text)} chars)"


def _send_email(draft_id: str = "", **_) -> str:
    return f"email SENT (draft {draft_id})"


def _delete_file(name: str = "", **_) -> str:
    return f"file '{name}' deleted"


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("list_today_events", RiskClass.READ, "List today's calendar", _list_today_events))
    reg.register(Tool("search_mail", RiskClass.READ, "Search recent mail", _search_mail))
    reg.register(Tool("get_file_text", RiskClass.READ, "Read a file's text", _get_file_text))
    reg.register(Tool("create_email_draft", RiskClass.DRAFT, "Draft an email (never sends)", _create_email_draft))
    reg.register(Tool("save_private_note", RiskClass.DRAFT, "Save a local private note", _save_private_note))
    reg.register(Tool("send_email", RiskClass.SEND, "Send an approved email", _send_email))
    reg.register(Tool("delete_file", RiskClass.DESTRUCTIVE, "Delete a file", _delete_file))
    return reg
