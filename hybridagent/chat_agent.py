"""Governed tool-calling chat loop (ReAct), wrapped by the GovernanceBroker.

This is the capability layer on top of the governance spine: in conversation the
model may answer directly or request tools. Every requested tool call is
schema-validated and routed through the broker, so the same guarantees the task
pipeline enforces apply to live chat:

* read / draft tools execute autonomously;
* send / destructive tools are **held for human approval** (surfaced as an
  approval the dashboard already renders) — the model is told it was held and
  must not claim success;
* disallowed or kill-switched tools are **denied**;
* malformed arguments are rejected against the tool's JSON schema before the
  broker ever sees them.

Tool results feed back into the model until it produces a final answer or the
per-turn step budget is exhausted. ``run`` is a generator of :class:`AgentEvent`
so the HTTP layer can stream tool-call cards and the final answer to the UI.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from .broker import GovernanceBroker, Verdict
from .content_guard import guard_tool_result
from .context import compact_tool_messages
from .tools import ToolRegistry
from .validation import ValidationError, validate_tool_args


@dataclass
class AgentEvent:
    """A single step in the governed loop, streamed to the UI.

    ``type`` is one of ``tool_call`` / ``tool_result`` / ``approval`` /
    ``denied`` / ``final`` / ``error`` (wrappers may add ``reflection`` /
    ``verification``).
    """
    type: str
    data: dict = field(default_factory=dict)


class ChatEngine(Protocol):
    """Structural type shared by the governed agent and its wrappers, so the
    Reflexion and verification wrappers can stack in any order."""

    def run(self, messages: list[dict],
            system: str | None = None) -> Iterator[AgentEvent]:
        ...


class GovernedChatAgent:
    MAX_STEPS = 6

    def __init__(self, llm, registry: ToolRegistry, broker: GovernanceBroker,
                 memory=None, *, actor: str = "praxis-chat",
                 max_steps: int | None = None,
                 max_context_chars: int | None = None) -> None:
        self.llm = llm
        self.registry = registry
        self.broker = broker
        self.memory = memory
        self.actor = actor
        self.max_steps = max_steps or self.MAX_STEPS
        # When set, the running tool history is compacted to this char budget
        # before each model call (pairing-aware), so long multi-tool turns stay
        # within the context window. None/0 disables it.
        self.max_context_chars = max_context_chars

    def _tool_specs(self) -> list[dict]:
        specs: list[dict] = []
        for tool in self.registry.catalog():
            specs.append({
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
                "risk": tool.risk.value,
            })
        return specs

    def run(self, messages: list[dict],
            system: str | None = None) -> Iterator[AgentEvent]:
        history: list[dict] = [dict(m) for m in (messages or [])]
        specs = self._tool_specs()

        for _ in range(self.max_steps):
            if self.max_context_chars:
                history = compact_tool_messages(
                    history, max_chars=self.max_context_chars)
            try:
                turn = self.llm.chat_tools(history, tools=specs, system=system)
            except Exception as exc:  # provider/connection failure
                yield AgentEvent("error", {"error": str(exc)})
                return

            text = (turn or {}).get("text") or ""
            calls = (turn or {}).get("tool_calls") or []
            if not calls:
                yield AgentEvent("final", {"text": text or "(no response)"})
                return

            # Record the assistant's tool-request turn so the next model call has
            # the full context (and the matching tool results below).
            history.append({"role": "assistant", "content": text, "tool_calls": calls})

            held = False
            for call in calls:
                name = str(call.get("name", ""))
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                cid = call.get("id") or f"call_{name}"
                tool = self.registry.get(name)

                if tool is None:
                    yield AgentEvent("denied", {"tool": name, "reason": "unknown tool"})
                    history.append(_tool_result(cid, name,
                                                f"ERROR: unknown tool '{name}'"))
                    continue

                try:
                    validate_tool_args(tool, args)
                except ValidationError as exc:
                    yield AgentEvent("denied", {"tool": name, "risk": tool.risk.value,
                                                "reason": f"schema: {exc}"})
                    history.append(_tool_result(cid, name, f"SCHEMA-DENIED: {exc}"))
                    continue

                preview = f"{name}({args})"
                decision = self.broker.authorize(
                    actor=self.actor, tool=name, risk=tool.risk, args=args,
                    preview=preview, provenance="chat",
                    rationale=f"Chat requested {tool.risk.value} tool '{name}'.",
                )
                yield AgentEvent("tool_call", {
                    "tool": name, "risk": tool.risk.value, "args": args,
                    "verdict": decision.verdict.value,
                })

                if decision.verdict is Verdict.ALLOW:
                    try:
                        result = str(tool.run(**args))
                    except Exception as exc:  # a tool failure must not crash chat
                        result = f"ERROR: {exc}"
                    safe = self.broker.redact(result)
                    # Tool output is untrusted external content: if it carries an
                    # injection, quarantine it before it re-enters the model and
                    # taint it so the egress firewall won't relay it back out.
                    flagged = self.broker.is_injection(result)
                    if flagged:
                        self.broker.mark_tainted(result)
                    guarded = guard_tool_result(safe, flagged=flagged)
                    if self.memory is not None:
                        try:
                            self.memory.note_working(safe, provenance=f"chat-action:{name}")
                        except Exception:
                            pass
                    yield AgentEvent("tool_result", {
                        "tool": name, "preview": safe[:240],
                        "injection_flagged": flagged})
                    history.append(_tool_result(cid, name, guarded.content))
                elif decision.verdict is Verdict.NEEDS_APPROVAL:
                    held = True
                    yield AgentEvent("approval", {
                        "tool": name, "risk": tool.risk.value,
                        "approval_id": decision.approval_id, "preview": preview,
                    })
                    history.append(_tool_result(
                        cid, name,
                        f"HELD for human approval (id={decision.approval_id}); "
                        "it has NOT executed."))
                else:
                    reason = self.broker.redact(decision.reason)
                    yield AgentEvent("denied", {"tool": name, "risk": tool.risk.value,
                                                "reason": reason})
                    history.append(_tool_result(cid, name, f"DENIED: {reason}"))

            if held:
                # A consequential action is queued; we can't fabricate its result,
                # so end the turn and let the human approve from the Approvals panel.
                yield AgentEvent("final", {
                    "text": text or (
                        "I've prepared a consequential action and held it for your "
                        "approval — see the **Approvals** panel."),
                    "held": True,
                })
                return

        yield AgentEvent("final", {
            "text": "I reached the tool-step limit for this turn. Ask me to continue "
                    "if you'd like me to keep going.",
        })


def _tool_result(call_id: str, name: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
