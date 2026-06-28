"""Narrow, explicit tools with risk classification (per both guides).

Read/draft tools are safe for autonomous use; send/destructive tools are
consequential and routed through the broker's approval queue. Tools here are
mock M365-style stand-ins; replace the callables with real broker/Graph calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .broker import RiskClass
from .real_tools import fetch_url, list_dir, read_file, search_web, write_file


@dataclass
class Tool:
    name: str
    risk: RiskClass
    description: str
    run: Callable[..., str]
    parameters: dict | None = None  # optional JSON-schema arg spec (function-calling)


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

    def catalog(self) -> list[Tool]:
        return list(self._tools.values())


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


# JSON schemas for the default mock tools so the LLM planner can emit valid args.
# Keep these minimal: the real M365 broker tools reuse these same arg names.
_LIST_EVENTS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_SEARCH_MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query for recent mail"},
    },
    "additionalProperties": False,
}

_GET_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "File or document name/path"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "array", "items": {"type": "string"},
                 "description": "Recipient email addresses"},
        "subject": {"type": "string", "description": "Email subject"},
        "body": {"type": "string", "description": "Email body text"},
    },
    "required": ["to", "subject", "body"],
    "additionalProperties": False,
}

_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Note text to save"},
    },
    "required": ["text"],
    "additionalProperties": False,
}

_SEND_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_id": {"type": "string", "description": "Identifier for the draft to send"},
    },
    "required": ["draft_id"],
    "additionalProperties": False,
}

_DELETE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "File or document name/path to delete"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Relative path under PRAXIS_WORK_DIR"},
    },
    "required": ["name"],
    "additionalProperties": False,
}

_WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Relative path under PRAXIS_WORK_DIR"},
        "content": {"type": "string", "description": "Text content to write"},
    },
    "required": ["name", "content"],
    "additionalProperties": False,
}

_LIST_DIR_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative directory under PRAXIS_WORK_DIR"},
    },
    "additionalProperties": False,
}

_FETCH_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "HTTP/HTTPS URL to fetch"},
    },
    "required": ["url"],
    "additionalProperties": False,
}

_SEARCH_WEB_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Web search query"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("list_today_events", RiskClass.READ, "List today's calendar",
                      _list_today_events, parameters=_LIST_EVENTS_SCHEMA))
    reg.register(Tool("search_mail", RiskClass.READ, "Search recent mail",
                      _search_mail, parameters=_SEARCH_MAIL_SCHEMA))
    reg.register(Tool("get_file_text", RiskClass.READ, "Read a file's text",
                      _get_file_text, parameters=_GET_FILE_SCHEMA))
    reg.register(Tool("create_email_draft", RiskClass.DRAFT, "Draft an email (never sends)",
                      _create_email_draft, parameters=_DRAFT_SCHEMA))
    reg.register(Tool("save_private_note", RiskClass.DRAFT, "Save a local private note",
                      _save_private_note, parameters=_NOTE_SCHEMA))
    reg.register(Tool("send_email", RiskClass.SEND, "Send an approved email",
                      _send_email, parameters=_SEND_SCHEMA))
    reg.register(Tool("delete_file", RiskClass.DESTRUCTIVE, "Delete a file",
                      _delete_file, parameters=_DELETE_SCHEMA))
    reg.register(Tool("read_file", RiskClass.READ, "Read a local file under PRAXIS_WORK_DIR",
                      read_file, parameters=_READ_FILE_SCHEMA))
    reg.register(Tool("write_file", RiskClass.DRAFT, "Write a local file under PRAXIS_WORK_DIR",
                      write_file, parameters=_WRITE_FILE_SCHEMA))
    reg.register(Tool("list_dir", RiskClass.READ, "List a local directory under PRAXIS_WORK_DIR",
                      list_dir, parameters=_LIST_DIR_SCHEMA))
    reg.register(Tool("fetch_url", RiskClass.READ, "Fetch the text content of a URL",
                      fetch_url, parameters=_FETCH_URL_SCHEMA))
    reg.register(Tool("search_web", RiskClass.READ,
                      "Search the web (Tavily/Brave/SerpAPI when configured)",
                      search_web, parameters=_SEARCH_WEB_SCHEMA))
    from .browser import browser_tools
    for tool in browser_tools():
        reg.register(tool)
    return reg
