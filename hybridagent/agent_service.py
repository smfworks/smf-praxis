"""Expose Praxis as a governed, callable agent (A2A-style).

The complement to the MCP work: where ``mcp_client`` lets Praxis *consume* other
agents' tools, this lets other agents/systems *invoke Praxis* as a node. A caller
posts a goal; Praxis plans and executes it under the governance broker
(read/draft run, send/destructive are held for approval) and returns a
JSON-serialisable result. :meth:`AgentService.card` advertises the agent's
capabilities and tools (with risk classes) for discovery.
"""
from __future__ import annotations

from typing import Any

from .plan_execute import PlanExecutor

PROTOCOL_VERSION = "praxis-a2a/0.1"
AGENT_VERSION = "0.1.0"


class AgentService:
    """Wrap a governed :class:`~hybridagent.agent.PraxisAgent` as a callable agent."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def run(self, goal: str, *, max_replans: int = 1) -> dict:
        """Plan and execute ``goal`` under governance; return a JSON-able result."""
        goal = (goal or "").strip()
        if not goal:
            return {"goal": "", "status": "failed", "summary": "no goal provided",
                    "replans": 0, "steps": [], "held_approvals": []}
        # Plan steps are broker-authorized; ensure the registry's tools are allowed.
        self.agent.broker.policy.allowed_tools.update(self.agent.registry.names())
        report = PlanExecutor(
            self.agent.registry, self.agent.broker,
            store=getattr(self.agent, "store", None),
            max_replans=max_replans).execute(goal)
        return {
            "goal": report.goal,
            "status": report.status,
            "summary": report.summary(),
            "replans": report.replans,
            "steps": [{"id": s.id, "intent": s.intent, "tool": s.tool,
                       "status": s.status} for s in report.steps],
            "held_approvals": report.held_approvals(),
        }

    def card(self) -> dict:
        """An agent card describing capabilities + tools (with risk) for discovery."""
        tools = [{"name": t.name, "risk": t.risk.value, "description": t.description}
                 for t in self.agent.registry.catalog()]
        return {
            "protocol": PROTOCOL_VERSION,
            "name": "praxis",
            "description": ("A governed, self-improving autonomous AI colleague. "
                            "Plans and executes goals under a governance broker."),
            "version": AGENT_VERSION,
            "skills": ["plan_execute", "deep_think", "debate", "grounded_qa",
                       "governed_tool_use"],
            "governance": {
                "model": "broker",
                "autonomous": ["read", "draft"],
                "held_for_approval": ["send", "destructive"],
            },
            "tools": tools,
        }
