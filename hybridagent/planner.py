"""Planner — decompose a goal into ordered, tool-bound steps.

Each step is bound to a tool and therefore inherits that tool's risk class, so
the broker can classify autonomous vs. approval-required actions at plan time.
Heuristic in mock mode; swap :meth:`plan` for an LLM planning call.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import LLMClient
from .tools import ToolRegistry


@dataclass
class Step:
    intent: str
    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class Plan:
    goal: str
    steps: list[Step]


class Planner:
    def __init__(self, registry: ToolRegistry, llm: LLMClient | None = None) -> None:
        self.registry = registry
        self.llm = llm or LLMClient()

    def plan(self, goal: str) -> Plan:
        g = goal.lower()
        steps: list[Step] = [
            Step("gather calendar context", "list_today_events"),
            Step("search related mail", "search_mail", {"query": goal}),
        ]
        if any(k in g for k in ("file", "doc", "report", "project")):
            steps.append(Step("read source document", "get_file_text", {"name": goal}))

        if any(k in g for k in ("follow up", "follow-up", "reply", "email", "respond")):
            steps.append(Step(
                "draft follow-up email", "create_email_draft",
                {"to": ["customer@example.com"], "subject": f"Re: {goal}",
                 "body": "Draft body grounded in gathered context."},
            ))
            # Consequential: held for approval by the broker.
            steps.append(Step("send the drafted email", "send_email", {"draft_id": "DRAFT-1"}))
        else:
            steps.append(Step("save a private brief", "save_private_note",
                              {"text": f"Brief for: {goal}"}))

        if "delete" in g or "clean up" in g or "remove" in g:
            steps.append(Step("delete obsolete file", "delete_file", {"name": "obsolete.txt"}))
        return Plan(goal=goal, steps=steps)

    def read_tools_for(self, goal: str) -> list[str]:
        """Which read-only tools to perceive with before planning."""
        tools = ["list_today_events", "search_mail"]
        if any(k in goal.lower() for k in ("file", "doc", "report", "project")):
            tools.append("get_file_text")
        return tools
