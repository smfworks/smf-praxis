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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .agent import PraxisAgent
from .router_model import RouterModel, train_from_runs
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
    """Goal -> role routing. Learns from governed outcome history when a model
    has been trained, and otherwise falls back to a keyword heuristic.

    The learned model (:class:`~hybridagent.router_model.RouterModel`) is fit
    from the ``subagent_runs`` the governance spine already records, so routing
    improves as real work accumulates — without any new data collection. Two
    invariants hold regardless of what the model says:

    * **Injection pin.** A goal whose source is flagged as injection (or any
      untrusted content) is always routed to the most-restrictive role, so an
      attacker who controls retrieved text cannot steer consequential work into
      the drafter/predictor pools by keyword *or* by poisoning the model.
    * **Known-role validation.** A learned label is only honoured if it is still
      a real role, so a stale model can never route to a role that was removed.
    """

    KNOWN_ROLES = frozenset(ROLE_TO_TOOLS)

    def __init__(self, model: RouterModel | None = None,
                 threshold: float = 0.60) -> None:
        self.model = model
        self.threshold = threshold

    @classmethod
    def from_store(cls, store, *, threshold: float = 0.60,
                   name: str = "predictive_router") -> "PredictiveRouter":
        """Load a persisted model if one exists; else a heuristic-only router."""
        model: RouterModel | None = None
        try:
            rec = store.load_router_model(name)
            if rec:
                model = RouterModel.from_json(rec["model_json"])
        except Exception:  # older store / corrupt blob -> heuristic fallback
            model = None
        return cls(model=model, threshold=threshold)

    def route(self, goal: str, *, injection_flagged: bool = False) -> str:
        """Assign a role to ``goal``.

        Untrusted (injection-flagged) goals are pinned to ``researcher`` before
        any model or keyword is consulted. Otherwise a confident learned
        prediction wins, falling back to the keyword heuristic.
        """
        if injection_flagged:
            return "researcher"
        if self.model is not None:
            learned = self.model.confident(goal, self.threshold)
            if learned in self.KNOWN_ROLES:
                return learned
        return self._heuristic(goal)

    @staticmethod
    def _heuristic(goal: str) -> str:
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
    # Hard cap on subagent nesting depth. The orchestrator can call itself
    # (e.g. compliance reviewer spawning a researcher), but unbounded recursion
    # would be both a runaway-resource risk and a governance-bypass surface.
    MAX_DEPTH = 3

    def __init__(self, store, router: PredictiveRouter | None = None,
                 pool: AgentPool | None = None) -> None:
        self.store = store
        # Auto-load a learned router from outcome history when present; the
        # classmethod falls back to a heuristic-only router otherwise.
        self.router = router or PredictiveRouter.from_store(store)
        self.pool = pool or AgentPool(store)

    def train_router(self, *, limit: int = 1000, min_samples: int = 8,
                     min_classes: int = 2) -> RouterModel | None:
        """Train the goal->role router from persisted subagent-run outcomes,
        persist it, and swap it in. Returns the model, or ``None`` when there is
        not yet enough successful history to learn from (heuristic stays)."""
        runs = self.store.list_subagent_runs(limit=limit)
        model = train_from_runs(runs, min_samples=min_samples,
                                min_classes=min_classes)
        if model is not None:
            self.store.save_router_model(model.to_json(), n_samples=model.n_samples)
            self.router = PredictiveRouter(model=model,
                                           threshold=self.router.threshold)
        return model

    def run(self, goal: str, role: str | None = None, *,
            injection_flagged: bool = False, parent_run_id: str = "",
            depth: int = 0) -> SubagentRun:
        if depth >= self.MAX_DEPTH:
            run_id = f"run-{uuid.uuid4().hex[:10]}"
            chosen = role or "researcher"
            self.store.add_subagent_run(run_id, f"agent-{chosen}", chosen, goal,
                                        status="failed")
            self.store.update_subagent_run(
                run_id, status="failed",
                error=f"recursion depth exceeded (depth={depth} >= {self.MAX_DEPTH})")
            self.store.add_compliance_event("", "subagent_recursion_blocked", {
                "run_id": run_id, "parent_run_id": parent_run_id,
                "depth": depth, "max_depth": self.MAX_DEPTH,
            }, ref_id=run_id)
            return SubagentRun(run_id, f"agent-{chosen}", chosen, goal, "failed")
        chosen = role or self.router.route(goal, injection_flagged=injection_flagged)
        spec, agent = self.pool.build_agent(chosen)
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        self.store.add_subagent_run(run_id, spec.agent_id, spec.role, goal)
        self.store.add_compliance_event("", "subagent_started", {
            "run_id": run_id, "agent_id": spec.agent_id,
            "role": spec.role, "goal": goal, "tools": spec.tools,
            "parent_run_id": parent_run_id, "depth": depth,
        }, ref_id=run_id)
        # Mark the agent instance live for liveness monitoring.
        self.store.upsert_agent_instance(spec.agent_id, spec.role,
                                         tools=spec.tools, status="running",
                                         load=1)
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
                "status": status, "parent_run_id": parent_run_id, "depth": depth,
            }, ref_id=run_id)
            self.store.upsert_agent_instance(spec.agent_id, spec.role,
                                             tools=spec.tools, status="idle",
                                             load=0)
            return SubagentRun(run_id, spec.agent_id, spec.role, goal, status,
                               cycle_id=report.cycle_id)
        except Exception as exc:
            self.store.update_subagent_run(run_id, status="failed", error=str(exc))
            self.store.add_compliance_event("", "subagent_error", {
                "run_id": run_id, "agent_id": spec.agent_id,
                "role": spec.role, "error": str(exc),
                "parent_run_id": parent_run_id, "depth": depth,
            }, ref_id=run_id)
            self.store.upsert_agent_instance(spec.agent_id, spec.role,
                                             tools=spec.tools, status="error",
                                             load=0)
            return SubagentRun(run_id, spec.agent_id, spec.role, goal, "failed")

    def run_many(self, goals: "list[str | tuple[str, str | None]]", *,
                 max_workers: int = 4, injection_flagged: bool = False,
                 parent_run_id: str = "", depth: int = 0) -> list[SubagentRun]:
        """Execute several subagent goals **concurrently** over the shared store.

        Each entry is a goal string or a ``(goal, role)`` tuple; the role is
        predicted from the goal when omitted. Results preserve input order. The
        store is lock-guarded (WAL + busy_timeout), so writes serialize safely
        while the agents' reasoning and tool work run in parallel — every
        decision, approval, and compliance event still flows through the same
        governance spine.
        """
        items = list(goals)
        if not items:
            return []
        workers = max(1, min(max_workers, len(items)))

        def _one(item: "str | tuple[str, str | None]") -> SubagentRun:
            goal, role = item if isinstance(item, tuple) else (item, None)
            return self.run(goal, role, injection_flagged=injection_flagged,
                            parent_run_id=parent_run_id, depth=depth)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_one, items))

    def list_runs(self, limit: int = 100) -> list[dict]:
        return self.store.list_subagent_runs(limit)

    def liveness(self, max_idle_seconds: float = 3600.0) -> list[dict]:
        """Return agent instances whose last_heartbeat_ts is older than
        ``max_idle_seconds``. Stale agents are also marked 'stale' in the store."""
        import time as _time
        now = _time.time()
        stale: list[dict] = []
        for inst in self.store.list_agent_instances():
            hb = inst.get("last_heartbeat_ts")
            if hb is None or (now - hb) <= max_idle_seconds:
                continue
            stale.append(inst)
            self.store.upsert_agent_instance(
                inst["agent_id"], inst["role"], tools=inst.get("tools", []),
                status="stale", load=inst.get("load", 0))
        return stale
