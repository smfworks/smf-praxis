"""Scoped subagent orchestration and predictive routing foundation.

Subagents are ordinary ``PraxisAgent`` instances with narrowed tool registries
and roles. They share the same persistent store, so all subagent decisions,
approvals, memories, and compliance events remain under the same governance
spine. This foundation is synchronous and deterministic; a future worker pool can
execute the same run records concurrently.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from .agent import PraxisAgent
from .tools import ToolRegistry, default_registry


ROLE_TO_TOOLS = {
    "researcher": {"list_today_events", "search_mail", "get_file_text", "save_private_note"},
    "drafter": {
        "list_today_events", "search_mail", "create_email_draft",
        "send_email", "save_private_note",
    },
    "compliance": {"list_today_events", "search_mail", "get_file_text", "save_private_note"},
    "predictor": {"list_today_events", "search_mail", "save_private_note"},
}


@dataclass
class AgentSpec:
    agent_id: str
    role: str
    tools: list[str] = field(default_factory=list)


@dataclass
class SubagentRun:
    run_id: str
    agent_id: str
    role: str
    goal: str
    status: str
    cycle_id: str = ""


class AgentSpecializer:
    @staticmethod
    def registry_for(role: str, base: ToolRegistry | None = None) -> ToolRegistry:
        base = base or default_registry()
        allowed = ROLE_TO_TOOLS.get(role, set(base.names()))
        reg = ToolRegistry()
        for name in base.names():
            tool = base.get(name)
            if tool and name in allowed:
                reg.register(tool)
        return reg


class PredictiveRouter:
    """Lightweight goal -> role routing heuristic, ready to be replaced by
    learned outcome statistics as subagent run data accumulates."""

    def route(self, goal: str) -> str:
        g = goal.lower()
        if any(k in g for k in ("risk", "compliance", "audit", "policy", "hipaa")):
            return "compliance"
        if any(k in g for k in ("draft", "reply", "email", "follow up", "follow-up")):
            return "drafter"
        if any(k in g for k in ("predict", "forecast", "likely", "trend")):
            return "predictor"
        return "researcher"


class AgentPool:
    def __init__(self, store, base_registry: ToolRegistry | None = None) -> None:
        self.store = store
        self.base_registry = base_registry or default_registry()

    def ensure(self, role: str) -> AgentSpec:
        agent_id = f"agent-{role}"
        registry = AgentSpecializer.registry_for(role, self.base_registry)
        tools = registry.names()
        self.store.upsert_agent_instance(agent_id, role, tools=tools)
        return AgentSpec(agent_id=agent_id, role=role, tools=tools)

    def build_agent(self, role: str) -> tuple[AgentSpec, PraxisAgent]:
        spec = self.ensure(role)
        registry = AgentSpecializer.registry_for(role, self.base_registry)
        return spec, PraxisAgent(registry=registry, store=self.store)

    def list(self) -> list[AgentSpec]:
        return [
            AgentSpec(agent_id=r["agent_id"], role=r["role"], tools=r["tools"])
            for r in self.store.list_agent_instances()
        ]


class Orchestrator:
    def __init__(self, store, router: PredictiveRouter | None = None,
                 pool: AgentPool | None = None) -> None:
        self.store = store
        self.router = router or PredictiveRouter()
        self.pool = pool or AgentPool(store)

    def run(self, goal: str, role: str | None = None) -> SubagentRun:
        chosen = role or self.router.route(goal)
        spec, agent = self.pool.build_agent(chosen)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        self.store.add_subagent_run(run_id, spec.agent_id, spec.role, goal)
        self.store.add_compliance_event("", "subagent_started", {
            "run_id": run_id, "agent_id": spec.agent_id,
            "role": spec.role, "goal": goal, "tools": spec.tools,
        }, ref_id=run_id)
        try:
            report = agent.handle(goal)
            result = {"summary": report.summary(), "actions": report.actions,
                      "pending_approvals": report.pending_approvals}
            status = "waiting_approval" if report.pending_approvals else "completed"
            self.store.update_subagent_run(
                run_id, status=status, cycle_id=report.cycle_id,
                result_json=json.dumps(result), error="")
            self.store.add_compliance_event(report.cycle_id, "subagent_finished", {
                "run_id": run_id, "agent_id": spec.agent_id, "role": spec.role,
                "status": status,
            }, ref_id=run_id)
            return SubagentRun(run_id, spec.agent_id, spec.role, goal, status,
                               cycle_id=report.cycle_id)
        except Exception as exc:
            self.store.update_subagent_run(run_id, status="failed", error=str(exc))
            self.store.add_compliance_event("", "subagent_error", {
                "run_id": run_id, "agent_id": spec.agent_id,
                "role": spec.role, "error": str(exc),
            }, ref_id=run_id)
            return SubagentRun(run_id, spec.agent_id, spec.role, goal, "failed")

    def list_runs(self, limit: int = 100) -> list[dict]:
        return self.store.list_subagent_runs(limit)
