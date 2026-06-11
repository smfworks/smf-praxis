"""PraxisAgent — the hybrid autonomous colleague.

Fuses OpenClaw's proactive action loop with Hermes' memory/judgment behind a
governance broker:

    perceive  (OpenClaw proactivity, injection-screened)
      -> plan (decompose into tool-bound steps)
      -> govern (broker: autonomous vs. approval-required)
      -> act/draft (execute reads/drafts; hold sends for approval)
      -> reflect (Hermes self-improvement)
      -> consolidate (summarize-not-hoard into durable memory + skills)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .broker import GovernanceBroker, GovernancePolicy, Verdict, RiskClass
from .llm import LLMClient
from .memory import Memory
from .perception import Perception, Signal
from .planner import Planner, Plan
from .reflection import Reflector, ReflectionResult
from .tools import ToolRegistry, default_registry


@dataclass
class CycleReport:
    goal: str
    actions: list[str] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    reflection: ReflectionResult | None = None

    def summary(self) -> str:
        return (
            f"goal='{self.goal}' | actions={len(self.actions)} "
            f"| pending_approvals={len(self.pending_approvals)} "
            f"| injection_flags={len(self.injection_flags)}"
        )


class PraxisAgent:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        llm: LLMClient | None = None,
        memory: Memory | None = None,
    ) -> None:
        self.registry = registry or default_registry()
        self.llm = llm or LLMClient()
        self.memory = memory or Memory()
        # Least privilege: allow exactly the registered tools; consequential
        # ones still route through approval regardless of being allowlisted.
        self.broker = GovernanceBroker(
            GovernancePolicy(allowed_tools=set(self.registry.names()))
        )
        self.perception = Perception(self.registry, self.broker, self.memory)
        self.planner = Planner(self.registry, self.llm)
        self.reflector = Reflector(self.memory, self.llm)

    # ----------------------------------------------------- durable seeding
    def learn(self, fact: str, kind: str = "preference", provenance: str = "user") -> None:
        self.memory.add_durable(fact, kind=kind, provenance=provenance)

    # --------------------------------------------------------- the main loop
    def handle(self, goal: str) -> CycleReport:
        report = CycleReport(goal=goal)

        # 1. PERCEIVE (proactive, injection-screened).
        signals: list[Signal] = self.perception.sense(
            goal, self.planner.read_tools_for(goal)
        )
        report.injection_flags = [s.source for s in signals if s.flagged_injection]
        # Reuse what perception already read so a read tool isn't executed twice
        # per cycle (which, against the M365 broker, would double real Graph calls).
        read_cache = {s.source: s.content for s in signals
                      if not s.source.startswith("memory:")}

        # 2. PLAN.
        plan: Plan = self.planner.plan(goal)

        # 3+4. GOVERN + ACT.
        for step in plan.steps:
            tool = self.registry.get(step.tool)
            if not tool:
                continue
            preview = f"{step.intent}: {step.tool}({step.args})"
            decision = self.broker.authorize(
                actor="praxis", tool=tool.name, risk=tool.risk,
                args=step.args, preview=preview, provenance="plan",
            )
            if decision.verdict is Verdict.ALLOW:
                if tool.risk is RiskClass.READ and tool.name in read_cache:
                    result = read_cache[tool.name]          # reuse perception's read
                else:
                    try:
                        result = tool.run(**step.args)
                    except Exception as exc:  # tool/broker failure shouldn't crash the cycle
                        report.actions.append(
                            f"[{tool.risk.value}] {step.intent} -> ERROR ({exc})")
                        continue
                self.memory.note_working(result, provenance=f"action:{tool.name}")
                report.actions.append(f"[{tool.risk.value}] {step.intent} -> {result}")
            elif decision.verdict is Verdict.NEEDS_APPROVAL:
                report.pending_approvals.append({
                    "approval_id": decision.approval_id,
                    "tool": tool.name,
                    "risk": tool.risk.value,
                    "preview": preview,
                })
                report.actions.append(
                    f"[{tool.risk.value}] {step.intent} -> HELD ({decision.approval_id})"
                )
            else:
                report.actions.append(f"[{tool.risk.value}] {step.intent} -> DENIED")

        # 5+6. REFLECT + CONSOLIDATE.
        report.reflection = self.reflector.reflect(goal, report.actions)
        return report

    # ------------------------------------------------- approval completion
    def approve(self, approval_id: str) -> str:
        pending = self.broker.approve(approval_id)
        if not pending:
            return f"no pending approval {approval_id}"
        tool = self.registry.get(pending.tool)
        if not tool:
            return f"tool {pending.tool} missing"
        try:
            result = tool.run(**pending.args)
        except Exception as exc:
            return f"approved action failed: {exc}"
        self.memory.add_episodic(f"approved+executed {pending.tool}: {result}",
                                 provenance="user-approval")
        return result

    # ---------------------------------------------------- proactive trigger
    def heartbeat(self, watch_goal: str = "scan for urgent follow-ups") -> CycleReport:
        """OpenClaw-style always-on tick: proactively run a watch goal."""
        return self.handle(watch_goal)
