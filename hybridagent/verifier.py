"""Governed answer verification — an independent critic gate over a turn.

Reflexion (:mod:`hybridagent.reflexion`) retries turns that *dead-end*.
Verification is the complement: it scrutinises turns that produced a
confident-looking answer and catches the ones that are wrong or dishonest before
the user sees them — most importantly an answer that claims a consequential
action was completed when the broker actually **held** it for approval or
**denied** it. A failed verification injects the critique and triggers one
bounded revision.

The default checks are deterministic and offline-safe (no model). An optional
critic callable adds a genuine second-model review when one is configured — the
verification half of a multi-agent loop, still under the same governance spine.

Safety: a turn is only re-run when it executed **no** draft/send/destructive
side effect, so a revision can never duplicate a real-world action. A held or
denied action did not execute, so re-running to correct the *wording* is safe.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import config as cfg
from .chat_agent import AgentEvent, ChatEngine
from .reflexion import _Trajectory

# First-person / passive assertions that a consequential action is already done.
_CLAIM_DONE = re.compile(
    r"(?i)\b("
    r"i'?ve\s+(?:just\s+)?(?:sent|emailed|deleted|scheduled|updated|created|posted|booked|cancell?ed|completed)|"
    r"i\s+have\s+(?:sent|emailed|deleted|scheduled|updated|created|posted|booked|cancell?ed|completed)|"
    r"i\s+(?:sent|emailed|deleted|scheduled|posted|booked|cancell?ed)\b|"
    r"(?:the\s+)?(?:email|message|it|that)\s+(?:was|has\s+been|is)\s+"
    r"(?:sent|delivered|deleted|scheduled|posted|updated|created)|"
    r"successfully\s+(?:sent|deleted|scheduled|updated|created|posted|completed)|"
    r"all\s+set|taken\s+care\s+of|it'?s\s+done|that'?s\s+done"
    r")\b")

_EMPTY = {"", "(no response)"}

# (task, answer) -> "APPROVE" or "REVISE: <reason>"
CriticFn = Callable[[str, str], str]


@dataclass
class VerificationVerdict:
    approved: bool
    critique: str = ""
    checks: list[str] = field(default_factory=list)  # names of FAILED checks


class AnswerVerifier:
    """Deterministic honesty checks plus an optional second-model critic."""

    def __init__(self, critic: CriticFn | None = None) -> None:
        self.critic = critic

    def verify(self, task: str, answer: str, *, held: bool = False,
               action_denied: bool = False, claim_ledger=None,
               organization_id: str = "", workspace_id: str = "") -> VerificationVerdict:
        text = (answer or "").strip()
        failed: list[str] = []
        critiques: list[str] = []

        if claim_ledger is not None and not claim_ledger.release_ready(
                organization_id, workspace_id):
            failed.append("material_claims")
            critiques.append(
                "Material claims are not fully supported by workspace evidence; "
                "professional release is blocked.")

        # 1) Honesty: never claim completion of a held/denied consequential action.
        if (held or action_denied) and _CLAIM_DONE.search(text):
            failed.append("action_claim_consistency")
            state = "held for human approval" if held else "denied by policy"
            critiques.append(
                f"Your answer states the action was completed, but it was {state} "
                "and did NOT execute. Rewrite it to say the action is pending "
                "approval (or could not be completed) — do not imply success.")

        # 2) Non-evasive: a turn that ran must produce a usable answer.
        if text in _EMPTY:
            failed.append("non_evasive")
            critiques.append("You produced no usable answer; address the request "
                             "directly or explain what is blocking you.")

        if failed:
            return VerificationVerdict(False, "\n".join(critiques), failed)

        # 3) Optional independent critic model (a genuine second opinion).
        if self.critic is not None:
            try:
                verdict = (self.critic(task, text) or "").strip()
            except Exception:
                verdict = "APPROVE"
            if verdict.upper().startswith("REVISE"):
                reason = (verdict.split(":", 1)[1].strip()
                          if ":" in verdict else verdict)
                return VerificationVerdict(
                    False, f"A reviewer flagged: {reason}", ["critic"])
        return VerificationVerdict(True)


@dataclass
class VerificationConfig:
    enabled: bool = True
    max_revisions: int = 1
    critic: "CriticFn | None" = None  # optional LLM-verifier critic backend

    @classmethod
    def load(cls) -> "VerificationConfig":
        v = cfg.load_config().get("agents", {}).get("verification", {}) or {}
        enabled = bool(v.get("enabled", True))
        maxr = int(v.get("maxRevisions", 1) or 0)
        env = os.environ.get("PRAXIS_VERIFY", "").lower()
        if env in ("0", "false", "off"):
            enabled = False
        elif env in ("1", "true", "on"):
            enabled = True
        # Optional LLM-verifier critic backend (H05). Lazy-built so the core
        # stays dependency-free when the operator has not opted in. A missing
        # library surfaces here only when critic == "llm-verifier".
        critic: "CriticFn | None" = None
        if enabled and str(v.get("critic", "") or "").lower() in (
                "llm-verifier", "llm_verifier", "llmverifier"):
            try:
                from .verifier_llm import build_llm_verifier_critic
                critic = build_llm_verifier_critic(v)
            except Exception:  # noqa: BLE001 — never block the deterministic path
                # Missing library or backend: fall back to deterministic-only
                # rather than break the chat loop. The operator sees the error
                # in the daemon log on first call, and can install/configure.
                critic = None
        return cls(enabled=enabled, max_revisions=max(0, maxr), critic=critic)


class VerifiedChatAgent:
    """Drop-in wrapper that verifies a turn's answer and revises once if flawed.

    Matches the ``run(messages, system) -> Iterator[AgentEvent]`` engine
    signature, so it stacks over a :class:`GovernedChatAgent` (optionally already
    wrapped by Reflexion). Emits a ``verification`` event when it rejects an
    answer.
    """

    def __init__(self, inner: ChatEngine, *, verifier: AnswerVerifier | None = None,
                 max_revisions: int = 1, claim_ledger=None,
                 organization_id: str = "", workspace_id: str = "") -> None:
        if claim_ledger is not None and (not organization_id or not workspace_id):
            raise ValueError("claim verification requires organization and workspace scope")
        self.inner = inner
        self.verifier = verifier or AnswerVerifier()
        self.max_revisions = max(0, max_revisions)
        self.claim_ledger = claim_ledger
        self.organization_id = organization_id
        self.workspace_id = workspace_id

    def run(self, messages: list[dict],
            system: str | None = None) -> Iterator[AgentEvent]:
        task = next((str(m.get("content", "")) for m in reversed(messages)
                     if m.get("role") == "user"), "")
        critique: str | None = None
        attempts = self.max_revisions + 1
        for attempt in range(attempts):
            aug_system = system
            if critique:
                preface = "A reviewer rejected your previous answer:\n" + critique
                aug_system = ((system + "\n\n") if system else "") + preface
            traj = _Trajectory()
            terminal: AgentEvent | None = None
            for ev in self.inner.run(messages, system=aug_system):
                traj.observe(ev)
                if ev.type in ("final", "error"):
                    terminal = ev
                    break  # hold the terminal: verify before emitting
                yield ev

            # Only a clean final answer is verified (errors are Reflexion's domain).
            if terminal is None or terminal.type != "final":
                yield terminal if terminal is not None else AgentEvent(
                    "final", {"text": "(no response)"})
                return
            verdict = self.verifier.verify(
                task, str(terminal.data.get("text", "")),
                held=traj.held, action_denied=traj.consequential_denied,
                claim_ledger=self.claim_ledger,
                organization_id=self.organization_id,
                workspace_id=self.workspace_id)
            last = attempt >= attempts - 1
            # A revision is safe only if the turn executed no side effect AND held
            # nothing for approval. Re-running a *held* turn would re-propose the
            # action and mint a SECOND pending approval (a human could approve both
            # -> double execution), so a held turn is surfaced but never re-run —
            # mirroring ReflexiveChatAgent's retry_safe.
            if (not last) and (not verdict.approved) and (
                    not traj.side_effect) and (not traj.held):
                critique = verdict.critique
                yield AgentEvent("verification", {
                    "approved": False, "critique": critique,
                    "checks": verdict.checks, "attempt": attempt + 1})
                continue
            if not verdict.approved:
                # Surfaced for the operator even when we cannot safely revise.
                yield AgentEvent("verification", {
                    "approved": False, "critique": verdict.critique,
                    "checks": verdict.checks})
                # Unsupported material claims are a release barrier, not an
                # advisory quality signal. Never emit the rejected terminal text.
                if "material_claims" in verdict.checks:
                    return
            yield terminal
            return
