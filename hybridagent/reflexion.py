"""Bounded Reflexion-style self-correction over the governed tool loop.

The governed ReAct loop (:class:`~hybridagent.chat_agent.GovernedChatAgent`)
already corrects *within* a turn: a denied or failing tool call is fed back so
the model can adapt on the next step. Reflexion adds an **outer** loop for the
turns that still end badly — a step-budget exhaustion, an empty answer, or a
provider error. When a turn ends in such a dead-end *and retrying is side-effect
safe*, this wrapper distils a short verbal self-reflection from the failure
signals, injects it, and re-runs the turn once (bounded).

Governance is unchanged: the inner agent's broker authorises every tool call on
every attempt. Two safety rules keep the retry honest:

* **No retry after a held action.** A turn that held a consequential action for
  approval is the *correct* governed outcome, not a failure — never retried.
* **No retry after a side effect.** If the failed turn already executed a
  draft/send/destructive tool, it is accepted as-is so a retry can never
  duplicate a real-world action. Only read-only or no-op turns are retried.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import config as cfg
from .chat_agent import AgentEvent, GovernedChatAgent

_CONSEQUENTIAL = {"draft", "send", "destructive"}
_EMPTY_FINALS = {"", "(no response)"}
_STEP_LIMIT_MARK = "tool-step limit"

ReflectFn = Callable[[list[dict], "_Trajectory", "str | None"], str]


@dataclass
class _Trajectory:
    """Failure and side-effect signals observed across one governed attempt."""

    denials: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    side_effect: bool = False
    terminal: AgentEvent | None = None

    def observe(self, ev: AgentEvent) -> None:
        if ev.type == "denied":
            self.denials.append(
                f"{ev.data.get('tool', '?')}: {ev.data.get('reason', 'denied')}")
        elif ev.type == "tool_call":
            if (ev.data.get("verdict") == "allow"
                    and ev.data.get("risk") in _CONSEQUENTIAL):
                self.side_effect = True
        elif ev.type == "tool_result":
            preview = str(ev.data.get("preview", ""))
            if preview.startswith("ERROR:"):
                self.tool_errors.append(f"{ev.data.get('tool', '?')}: {preview}")
        elif ev.type in ("final", "error"):
            self.terminal = ev

    # ----------------------------------------------------------- assessment
    @property
    def held(self) -> bool:
        t = self.terminal
        return bool(t and t.type == "final" and t.data.get("held"))

    @property
    def failed(self) -> bool:
        """A *terminal* dead-end — not a mid-turn bump the model recovered from."""
        t = self.terminal
        if t is None or self.held:
            return False
        if t.type == "error":
            return True
        text = str(t.data.get("text", "")).strip()
        return _STEP_LIMIT_MARK in text or text in _EMPTY_FINALS

    @property
    def retry_safe(self) -> bool:
        return not self.held and not self.side_effect

    def reasons(self) -> list[str]:
        out: list[str] = []
        t = self.terminal
        if t is not None and t.type == "error":
            out.append(
                f"the model/provider call failed ({t.data.get('error', 'error')})")
        elif t is not None and _STEP_LIMIT_MARK in str(t.data.get("text", "")):
            out.append("you exhausted the tool-step budget without finishing")
        elif t is not None and str(t.data.get("text", "")).strip() in _EMPTY_FINALS:
            out.append("you produced no usable answer")
        out += [f"a tool was rejected ({d})" for d in self.denials]
        out += [f"a tool errored ({e})" for e in self.tool_errors]
        return out or ["the previous attempt did not succeed"]


def _default_reflect(messages: list[dict], traj: "_Trajectory",
                     system: str | None) -> str:
    """Deterministic, offline self-reflection grounded in the failure signals."""
    lines = ["Self-reflection on your previous failed attempt:"]
    lines += [f"- {r}" for r in traj.reasons()]
    lines.append(
        "Revise your approach: call only tools that exist and are allowed, "
        "satisfy each tool's argument schema before calling it, and do not "
        "repeat a tool that was denied. If a required action was denied or needs "
        "approval, say so plainly instead of retrying it. If no tool applies, "
        "answer the user directly and concisely.")
    return "\n".join(lines)


@dataclass
class ReflexionConfig:
    enabled: bool = True
    max_reflections: int = 1

    @classmethod
    def load(cls) -> "ReflexionConfig":
        r = cfg.load_config().get("agents", {}).get("reflection", {}) or {}
        enabled = bool(r.get("enabled", True))
        maxr = int(r.get("maxReflections", 1) or 0)
        env = os.environ.get("PRAXIS_REFLECT", "").lower()
        if env in ("0", "false", "off"):
            enabled = False
        elif env in ("1", "true", "on"):
            enabled = True
        return cls(enabled=enabled, max_reflections=max(0, maxr))


class ReflexiveChatAgent:
    """Drop-in wrapper around :class:`GovernedChatAgent` adding bounded retries.

    Matches the inner agent's ``run(messages, system) -> Iterator[AgentEvent]``
    signature, so the daemon can swap it in transparently. A ``reflection`` event
    is emitted between attempts for UI/audit visibility.
    """

    def __init__(self, inner: GovernedChatAgent, *, max_reflections: int = 1,
                 reflect: ReflectFn | None = None) -> None:
        self.inner = inner
        self.max_reflections = max(0, max_reflections)
        self._reflect: ReflectFn = reflect or _default_reflect

    def run(self, messages: list[dict],
            system: str | None = None) -> Iterator[AgentEvent]:
        reflection: str | None = None
        attempts = self.max_reflections + 1
        for attempt in range(attempts):
            aug_system = system
            if reflection:
                aug_system = ((system + "\n\n") if system else "") + reflection
            traj = _Trajectory()
            terminal: AgentEvent | None = None
            for ev in self.inner.run(messages, system=aug_system):
                traj.observe(ev)
                if ev.type in ("final", "error"):
                    terminal = ev
                    break  # hold the terminal event: decide retry vs. emit
                yield ev
            last = attempt >= attempts - 1
            if (not last) and traj.failed and traj.retry_safe:
                reflection = self._reflect(messages, traj, system)
                yield AgentEvent("reflection",
                                 {"text": reflection, "attempt": attempt + 1})
                continue
            yield terminal if terminal is not None else AgentEvent(
                "final", {"text": "(no response)"})
            return
