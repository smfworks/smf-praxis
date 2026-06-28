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
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from .broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict
from .llm import LLMClient
from .memory import Memory
from .perception import Perception, Signal
from .planner import LLMPlanner, Plan
from .reflection import ReflectionResult, Reflector
from .tools import ToolRegistry, default_registry


@dataclass
class CycleReport:
    goal: str
    cycle_id: str = field(default_factory=lambda: f"cyc-{uuid.uuid4().hex[:12]}")
    actions: list[str] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    reflection: ReflectionResult | None = None
    plan: Any | None = None

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
        mcp_servers: list[dict] | None = None,
        planner=None,
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
        # A vertical pack (if one is active) tailors the governance posture and
        # tool allowlist for a domain; the persona is composed at the chat surface.
        from . import pack as _pack
        _pack.apply_active_to_broker(self.broker)
        # RAG is available when there's a store to back the vector table.
        self.rag = None
        self.skills = None
        if store is not None:
            from .embeddings import EmbeddingClient
            from .rag import Rag
            from .skills import SkillLibrary
            embedder = EmbeddingClient()
            self.rag = Rag(store, embedder)
            self.skills = SkillLibrary(store=store, embedder=embedder)
        self.perception = Perception(self.registry, self.broker, self.memory,
                                     rag=self.rag, skills=self.skills)
        self.planner = planner if planner is not None else LLMPlanner(
            self.registry, self.llm, can_escalate=self._under_budget)
        self.reflector = Reflector(self.memory, self.llm)
        # Optionally import tools from external MCP servers.
        self.mcp_servers = mcp_servers or []
        if self.mcp_servers:
            import asyncio

            from .mcp_adapter import MCPClient
            async def _load() -> None:
                client = MCPClient()
                for cfg in self.mcp_servers:
                    await client.load_server(
                        self.registry,
                        command=cfg["command"],
                        args=cfg.get("args", []),
                        env=cfg.get("env"),
                        server_name=cfg.get("name", "external"),
                    )
            asyncio.run(_load())
            # Refresh broker allowlist with discovered tools.
            self.broker.policy.allowed_tools.update(self.registry.names())

    @classmethod
    def persistent(cls, registry: ToolRegistry | None = None,
                   llm: LLMClient | None = None,
                   mcp_servers: list[dict] | None = None,
                   work_dir: str | None = None) -> "PraxisAgent":
        """Build an agent backed by the on-disk store (~/.praxis/praxis.db)."""
        from .persistence import Store
        if work_dir:
            os.environ.setdefault("PRAXIS_WORK_DIR", work_dir)
        return cls(registry=registry, llm=llm, store=Store.open(),
                   mcp_servers=mcp_servers)

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
        report.plan = plan
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
            # Pre-flight: validate step args against the tool's declared schema
            # so malformed plans fail at plan time (with a clean audit entry)
            # rather than mid-execution against a real M365 endpoint.
            try:
                from .validation import ValidationError, validate_tool_args
                validate_tool_args(tool, step.args)
            except ValidationError as exc:
                report.actions.append(
                    f"[{tool.risk.value}] {step.intent} -> SCHEMA-DENIED ({exc})")
                self._event(report.cycle_id, "schema_denied", {
                    "tool": tool.name, "error": str(exc),
                })
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
        self._record_skill_outcomes(goal, signals, report)
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

    def _record_skill_outcomes(self, goal: str, signals: list[Signal],
                               report: "CycleReport") -> None:
        """Score skills that fired during this cycle so the evaluator can
        quarantine low-quality ones over time. Without this, Phase 9's metrics
        never accumulate from real usage."""
        if self.skills is None or self.skills.rag is None:
            return
        applied = [s.source.split(":", 1)[1] for s in signals
                   if s.source.startswith("skill:")]
        if not applied:
            return
        # A cycle is a "success" when actions ran without errors AND nothing was
        # denied. Held approvals are neutral (the human will decide).
        has_error = any("-> ERROR" in a for a in report.actions)
        has_denied = any("-> DENIED" in a for a in report.actions)
        if has_error or has_denied:
            outcome = "failure"
        elif report.pending_approvals:
            outcome = "partial"
        else:
            outcome = "success"
        for name in applied:
            try:
                self.skills.record_outcome(
                    name, goal, outcome, cycle_id=report.cycle_id,
                    notes=f"auto-recorded; actions={len(report.actions)}")
            except Exception:
                continue
        self._event(report.cycle_id, "skill_outcomes_recorded", {
            "skills": applied, "outcome": outcome,
        })

    # ---------------------------------------------------- proactive trigger
    def heartbeat(self, watch_goal: str = "scan for urgent follow-ups",
                  refresh_wiki: bool = True) -> CycleReport:
        """OpenClaw-style always-on tick: proactively refresh KB sources due
        for revalidation, then run a watch goal. Setting refresh_wiki=False
        skips the refresh (useful in tests)."""
        if refresh_wiki and self.store is not None:
            try:
                from .wiki import KBSourceManager
                KBSourceManager(self.store).refresh_due(rag=self.rag)
            except Exception as exc:
                self._event("", "heartbeat_wiki_error", {"error": str(exc)})
        return self.handle(watch_goal)

    # ------------------------------------------------- grounded Q&A (no hallucination)
    def _under_budget(self) -> bool:
        """True while the spend budget has room (no cap set, or under the cap).
        Gates adaptive-cascade escalation — a low-confidence cheap answer is kept
        rather than escalating to the costly tier once the cap is hit, so cost
        control wins over a marginal quality bump. Shared by the planner and the
        grounded responder."""
        if self.store is None:
            return True
        b = self.store.get_budget()
        return not (b["limit_usd"] > 0 and b["spent_usd"] >= b["limit_usd"])

    def ask(self, question: str, k: int = 5, *,
            refresh_wiki: bool = True):
        """Answer a question grounded in retrieved sources; abstain if unsupported.

        Before retrieval, refresh any registered wiki sources that are due so
        the answer reflects current KB. Contradictions across retrieved chunks
        are surfaced on the returned answer (caller can inspect
        ``answer.contradictions``) and emitted to the compliance event log."""
        from .grounding import GroundedResponder
        from .rag import RetrievedChunk
        if refresh_wiki and self.store is not None:
            try:
                from .wiki import KBSourceManager
                KBSourceManager(self.store).refresh_due(rag=self.rag)
            except Exception as exc:
                self._event("", "ask_wiki_refresh_error", {"error": str(exc)})
        sources: list = []
        if self.rag is not None:
            sources.extend(self.rag.retrieve(question, k=k))
        try:
            from . import pack as _pack
            sources.extend(_pack.knowledge_chunks(question, self.store, k=k))
        except Exception:
            pass
        for item in self.memory.recall(question, k=3):
            sources.append(RetrievedChunk(
                text=item.text, source=f"memory:{item.kind}", score=1.0,
                kind="memory", provenance=item.provenance))
        answer = GroundedResponder(
            self.llm, can_escalate=self._under_budget).answer(question, sources)
        # Annotate with contradiction findings — caller / CLI can show these.
        try:
            from .contradiction import detect
            answer.contradictions = detect(sources)
            if answer.contradictions and self.store is not None:
                self._event("", "ask_contradictions_detected", {
                    "question": question,
                    "count": len(answer.contradictions),
                    "pairs": [
                        {"a": c.a_source, "b": c.b_source,
                         "score": c.score, "why": c.explanation}
                        for c in answer.contradictions
                    ],
                })
        except Exception:
            answer.contradictions = []
        return answer

    # ------------------------------------------------------- skill distillation
    def learn_skill(self, goal: str, name: str | None = None):
        """Distill a reusable skill draft from the goal's plan (no side effects).

        Returns a Skill draft; persisting it is a governed step the caller must
        approve (see `praxis learn`)."""
        from .skills import distill_skill
        plan = self.planner.plan(goal)
        trace = [f"{s.intent} -> {s.tool}" for s in plan.steps]
        return distill_skill(self.llm, goal, trace, name=name)
