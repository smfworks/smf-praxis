"""Planner — decompose a goal into ordered, tool-bound steps.

Each step is bound to a tool and therefore inherits that tool's risk class, so
the broker can classify autonomous vs. approval-required actions at plan time.

* :class:`Planner` is the deterministic heuristic baseline (offline-safe).
* :class:`LLMPlanner` asks the configured LLM to emit JSON steps, validates each
  step's arguments against the tool's declared schema, drops hallucinated tools,
  and falls back to the heuristic planner whenever no valid steps survive or the
  agent is in mock/offline mode.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .escalation import AdaptiveCascade
from .llm import LLMClient
from .structured import generate_json
from .tools import ToolRegistry
from .validation import ValidationError, validate_tool_args


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


def _tool_catalog(registry: ToolRegistry) -> str:
    """Render registered tools and their JSON schemas for the planner prompt."""
    parts: list[str] = []
    for tool in registry.catalog():
        params = tool.parameters or {}
        parts.append(
            f"- {tool.name} ({tool.risk.value}): {tool.description}\n"
            f"  parameters: {json.dumps(params)}"
        )
    return "\n".join(parts)


def _validate_step(registry: ToolRegistry, step: dict) -> Step | None:
    """Convert a raw LLM step dict into a :class:`Step`, or return None if the
    tool is unknown or the arguments violate the tool's declared schema."""
    if not isinstance(step, dict):
        return None
    tool_name = step.get("tool")
    if not isinstance(tool_name, str):
        return None
    tool = registry.get(tool_name)
    if tool is None:
        return None
    args = step.get("args")
    args = args if isinstance(args, dict) else {}
    try:
        validate_tool_args(tool, args)
    except ValidationError:
        return None
    intent = str(step.get("intent", "step")).strip() or "step"
    return Step(intent=intent, tool=tool_name, args=args)


class LLMPlanner(Planner):
    """LLM-driven planner with schema-validated, registry-bound steps.

    In mock/offline mode it immediately falls back to the deterministic
    :class:`Planner`. In real mode it asks the LLM for a JSON plan, drops any
    step that references an unknown tool or has invalid args, and falls back to
    the heuristic planner if nothing usable survives.

    The constructor accepts an optional ``fallback`` factory so callers can swap
    the fallback planner (e.g. for tests or for a domain-specific planner like
    :class:`~hybridagent.m365_tools.M365Planner`).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm: LLMClient | None = None,
        fallback: Callable[[ToolRegistry, LLMClient | None], Planner] | None = None,
        can_escalate: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(registry, llm)
        self.fallback_factory = fallback or Planner
        self._can_escalate = can_escalate

    def plan(self, goal: str) -> Plan:
        if self.llm._effective_mode() != "real":
            return super().plan(goal)
        prompt = (
            f"Goal: {goal}\n\n"
            "Available tools (use ONLY these tool names; every step must use a "
            "declared tool and its arguments must match the parameters schema):\n"
            f"{_tool_catalog(self.registry)}\n\n"
            "Return a single JSON object with this exact shape:\n"
            '{"steps": [{"intent": "concise human-readable intent", '
            '"tool": "tool_name", "args": {}}]}'
        )

        def solve(difficulty: str | None) -> list[Step]:
            try:
                obj = generate_json(self.llm, prompt, ["steps"], role="planner",
                                    difficulty=difficulty)
            except Exception:
                return []
            steps: list[Step] = []
            for raw in obj.get("steps", []):
                parsed = _validate_step(self.registry, raw)
                if parsed is not None:
                    steps.append(parsed)
            return steps

        # Cheap-first: plan at the routed tier; if it yields no valid steps,
        # escalate to the strongest model before falling back to the heuristic.
        # The escalation respects the spend budget (``can_escalate``) so a run
        # can't jump to the costly tier once the cap is reached.
        result = AdaptiveCascade[list[Step]](
            can_escalate=self._can_escalate).run(solve, accept=bool)
        if result.escalated and hasattr(self.llm, "note_escalation"):
            self.llm.note_escalation()
        if result.answer:
            return Plan(goal=goal, steps=result.answer)
        return self.fallback_factory(self.registry, self.llm).plan(goal)

    def read_tools_for(self, goal: str) -> list[str]:
        """Use the LLM to pick read-only tools, but fall back to the baseline."""
        if self.llm._effective_mode() != "real":
            return super().read_tools_for(goal)
        # Build a clean catalog of read tools and ask the model which matter.
        read_names = [t.name for t in self.registry.catalog()
                      if t.risk == "read"]
        if not read_names:
            return super().read_tools_for(goal)
        prompt = (
            f"Goal: {goal}\n\nRead-only tools available: {json.dumps(read_names)}\n\n"
            "Return JSON: {\"read_tools\": [\"tool_name\", ...]} — the subset most "
            "likely to provide useful context for the goal. Omit tools that are "
            "clearly irrelevant."
        )
        try:
            obj = generate_json(self.llm, prompt, ["read_tools"], role="planner")
            chosen = [str(t) for t in obj.get("read_tools", [])
                      if isinstance(t, str) and t in read_names]
            if chosen:
                return chosen
        except Exception:
            pass
        return super().read_tools_for(goal)
