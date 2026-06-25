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

import hashlib
import uuid
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
    cycle_id: str = field(default_factory=lambda: f"cyc-{uuid.uuid4().hex[:12]}")
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
        store=None,
    ) -> None:
        self.store = store
        self.registry = registry or default_registry()
        self.llm = llm or LLMClient()
        self.memory = memory or Memory(store=store)
        # Least privilege: allow exactly the registered tools; consequential
        # ones still route through approval regardless of being allowlisted.
        self.broker = GovernanceBroker(
            GovernancePolicy(allowed_tools=set(self.registry.names())),
            store=store,
        )
        # RAG is available when there's a store to back the vector table.
        self.rag = None
        self.skills = None
        if store is not None:
            from .rag import Rag
            from .embeddings import EmbeddingClient
            from .skills import SkillLibrary
            embedder = EmbeddingClient()
            self.rag = Rag(store, embedder)
            self.skills = SkillLibrary(store=store, embedder=embedder)
        self.perception = Perception(self.registry, self.broker, self.memory,
                                     rag=self.rag, skills=self.skills)
        self.planner = Planner(self.registry, self.llm)
        self.reflector = Reflector(self.memory, self.llm)

    @classmethod
    def persistent(cls, registry: ToolRegistry | None = None,
                   llm: LLMClient | None = None) -> "PraxisAgent":
        """Build an agent backed by the on-disk store (~/.praxis/praxis.db)."""
        from .persistence import Store
        return cls(registry=registry, llm=llm, store=Store.open())

    # ----------------------------------------------------- durable seeding
    def learn(self, fact: str, kind: str = "preference", provenance: str = "user") -> None:
        self.memory.add_durable(fact, kind=kind, provenance=provenance)

    # --------------------------------------------------------- the main loop
    def handle(self, goal: str) -> CycleReport:
        report = CycleReport(goal=goal)
        self._event(report.cycle_id, "cycle_start", {"goal": goal})

        # 1. PERCEIVE (proactive, injection-screened).
        signals: list[Signal] = self.perception.sense(
            goal, self.planner.read_tools_for(goal)
        )
        report.injection_flags = [s.source for s in signals if s.flagged_injection]
        evidence = self._evidence_from_signals(signals)
        self._event(report.cycle_id, "signals", {
            "count": len(signals),
            "injection_flags": report.injection_flags,
            "evidence": evidence,
        })
        # Reuse what perception already read so a read tool isn't executed twice
        # per cycle (which, against the M365 broker, would double real Graph
        # calls). Only cache signals that came from an actual registered tool.
        # NOTE: keyed on tool name only — valid because perception and the
        # heuristic Planner both read at goal granularity. Before wiring an
        # LLM planner that emits arbitrary read args, key this on (tool, args).
        read_cache = {s.source: s.content for s in signals
                      if self.registry.get(s.source) is not None}

        # 2. PLAN.
        plan: Plan = self.planner.plan(goal)
        self._event(report.cycle_id, "plan", {
            "steps": [
                {"intent": s.intent, "tool": s.tool, "args_hash": self._hash_text(str(s.args))}
                for s in plan.steps
            ]
        })

        # 3+4. GOVERN + ACT.
        for step in plan.steps:
            tool = self.registry.get(step.tool)
            if not tool:
                continue
            preview = f"{step.intent}: {step.tool}({step.args})"
            decision = self.broker.authorize(
                actor="praxis", tool=tool.name, risk=tool.risk,
                args=step.args, preview=preview, provenance="plan",
                cycle_id=report.cycle_id, evidence=evidence,
                rationale=f"Plan step '{step.intent}' requested {tool.risk.value} tool '{tool.name}'.",
            )
            self._event(report.cycle_id, "decision", {
                "decision_id": decision.decision_id,
                "tool": tool.name,
                "risk": tool.risk.value,
                "verdict": decision.verdict.value,
                "policy_rule": decision.policy_rule,
                "reason": self.broker.redact(decision.reason),
                "approval_id": decision.approval_id,
            }, ref_id=decision.decision_id)
            if decision.verdict is Verdict.ALLOW:
                if tool.risk is RiskClass.READ and tool.name in read_cache:
                    result = read_cache[tool.name]          # reuse perception's read
                else:
                    try:
                        result = tool.run(**step.args)
                    except Exception as exc:  # tool/broker failure shouldn't crash the cycle
                        report.actions.append(
                            f"[{tool.risk.value}] {step.intent} -> ERROR ({exc})")
                        self._event(report.cycle_id, "action_error", {
                            "decision_id": decision.decision_id,
                            "tool": tool.name,
                            "error": str(exc),
                        }, ref_id=decision.decision_id)
                        continue
                self.memory.note_working(result, provenance=f"action:{tool.name}")
                report.actions.append(f"[{tool.risk.value}] {step.intent} -> {result}")
                self._event(report.cycle_id, "action_result", {
                    "decision_id": decision.decision_id,
                    "tool": tool.name,
                    "risk": tool.risk.value,
                    "result_hash": self._hash_text(result),
                    "result_preview": self.broker.redact(result)[:240],
                }, ref_id=decision.decision_id)
            elif decision.verdict is Verdict.NEEDS_APPROVAL:
                report.pending_approvals.append({
                    "approval_id": decision.approval_id,
                    "decision_id": decision.decision_id,
                    "cycle_id": report.cycle_id,
                    "tool": tool.name,
                    "risk": tool.risk.value,
                    "preview": preview,
                    "rationale": f"{tool.risk.value} tool '{tool.name}' requires human approval.",
                    "evidence": evidence,
                })
                report.actions.append(
                    f"[{tool.risk.value}] {step.intent} -> HELD ({decision.approval_id})"
                )
            else:
                report.actions.append(f"[{tool.risk.value}] {step.intent} -> DENIED")

        # 5+6. REFLECT + CONSOLIDATE.
        report.reflection = self.reflector.reflect(goal, report.actions)
        self._event(report.cycle_id, "cycle_end", {
            "actions": len(report.actions),
            "pending_approvals": len(report.pending_approvals),
            "injection_flags": report.injection_flags,
            "reflection": str(report.reflection),
        })
        return report

    # ------------------------------------------------- approval completion
    def approve(self, approval_id: str, approved_by: str = "user",
                approval_notes: str = "") -> str:
        pending = self.broker.approve(
            approval_id, approved_by=approved_by, approval_notes=approval_notes)
        if not pending:
            return f"no pending approval {approval_id}"
        tool = self.registry.get(pending.tool)
        if not tool:
            return f"tool {pending.tool} missing"
        try:
            result = tool.run(**pending.args)
        except Exception as exc:
            self._event(pending.cycle_id, "approval_execution_error", {
                "approval_id": approval_id,
                "decision_id": pending.decision_id,
                "tool": pending.tool,
                "error": str(exc),
            }, ref_id=approval_id)
            return f"approved action failed: {exc}"
        self.memory.add_episodic(f"approved+executed {pending.tool}: {result}",
                                 provenance="user-approval")
        self._event(pending.cycle_id, "approval_executed", {
            "approval_id": approval_id,
            "decision_id": pending.decision_id,
            "tool": pending.tool,
            "approved_by": approved_by,
            "result_hash": self._hash_text(result),
            "result_preview": self.broker.redact(result)[:240],
        }, ref_id=approval_id)
        return result

    # ------------------------------------------------------ compliance helpers
    def _event(self, cycle_id: str, event_type: str, payload: dict,
               ref_id: str = "") -> None:
        if self.store is not None:
            self.store.add_compliance_event(cycle_id, event_type, payload, ref_id)

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256((text or "").encode()).hexdigest()

    def _evidence_from_signals(self, signals: list[Signal]) -> list[dict]:
        out = []
        for sig in signals:
            redacted = self.broker.redact(sig.content or "")
            out.append({
                "source": sig.source,
                "content_hash": self._hash_text(sig.content or ""),
                "snippet": redacted[:240],
                "flagged_injection": sig.flagged_injection,
            })
        return out

    # ---------------------------------------------------- proactive trigger
    def heartbeat(self, watch_goal: str = "scan for urgent follow-ups") -> CycleReport:
        """OpenClaw-style always-on tick: proactively run a watch goal."""
        return self.handle(watch_goal)

    # ------------------------------------------------- grounded Q&A (no hallucination)
    def ask(self, question: str, k: int = 5):
        """Answer a question grounded in retrieved sources; abstain if unsupported."""
        from .grounding import GroundedResponder
        from .rag import RetrievedChunk
        sources: list = []
        if self.rag is not None:
            sources.extend(self.rag.retrieve(question, k=k))
        for item in self.memory.recall(question, k=3):
            sources.append(RetrievedChunk(
                text=item.text, source=f"memory:{item.kind}", score=1.0,
                kind="memory", provenance=item.provenance))
        return GroundedResponder(self.llm).answer(question, sources)

    # ------------------------------------------------------- skill distillation
    def learn_skill(self, goal: str, name: str | None = None):
        """Distill a reusable skill draft from the goal's plan (no side effects).

        Returns a Skill draft; persisting it is a governed step the caller must
        approve (see `praxis learn`)."""
        from .skills import distill_skill
        plan = self.planner.plan(goal)
        trace = [f"{s.intent} -> {s.tool}" for s in plan.steps]
        return distill_skill(self.llm, goal, trace, name=name)
