"""Governance broker — the control plane both source guides converge on.

Eliminates permissionless local autonomy and prompt-injection
weaknesses, and supplies the broker the action loop assumes:

* tool allowlist + least privilege
* risk classification: read/draft are autonomous; send/destructive need approval
* draft-before-send: consequential actions are held in an approval queue
* prompt-injection boundary: retrieved content is data, never instruction
* audit trail (attributable) + redaction
* kill-switch that disables all consequential tools
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .data_policy import Classification, DataPolicy
from .errors import agent_error
from .logging_util import get_logger

if TYPE_CHECKING:
    from .persistence import Store


class RiskClass(str, Enum):
    READ = "read"             # autonomous
    DRAFT = "draft"           # autonomous (never sends)
    SEND = "send"             # requires approval
    DESTRUCTIVE = "destructive"  # requires approval


AUTONOMOUS = {RiskClass.READ, RiskClass.DRAFT}
CONSEQUENTIAL = {RiskClass.SEND, RiskClass.DESTRUCTIVE}


class ComplianceMode(str, Enum):
    """Operator-selectable governance posture (the lockdown is on by default).

    * ``enforced``   — consequential (send/destructive) actions are held for human
      approval; egress firewall and injection detection active. The default.
    * ``autonomous`` — consequential actions run without approval, but the egress
      firewall, injection detection, and kill-switch all stay active.
    * ``permissive`` — consequential actions run without approval and the egress
      firewall + injection detection are off; only the kill-switch remains. For
      trusted or sandboxed environments (e.g. an isolated coding workspace).
    """

    ENFORCED = "enforced"
    AUTONOMOUS = "autonomous"
    PERMISSIVE = "permissive"


COMPLIANCE_MODES = (
    ComplianceMode.ENFORCED, ComplianceMode.AUTONOMOUS, ComplianceMode.PERMISSIVE)


def coerce_compliance_mode(value: object) -> ComplianceMode:
    """Map a string or member to a ComplianceMode, falling back to the safe default."""
    try:
        return ComplianceMode(value)
    except (ValueError, TypeError):
        return ComplianceMode.ENFORCED


class Verdict(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    approval_id: str | None = None
    decision_id: str = ""
    policy_rule: str = ""


# Multiple injection patterns. Single-pattern regexes are easily paraphrased
# around; this set covers common jailbreak shapes (instruction overrides,
# role-swaps, exfil prompts, system-prompt extraction, encoded delimiters) so
# that retrieved content carrying any of them gets flagged as data, not policy.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"ignore (all |the )?(previous |prior )?(instructions|prompt|rules)",
        r"disregard (the |all )?(system|policy|previous|prior)",
        r"do not tell (?:anyone|the user|michael)",
        r"send this (file|message|note) to (everyone|all|the team)",
        r"delete the (original|email|file|record)",
        r"approve (this|the) (request|action) (immediately|silently|without)",
        r"reveal (your )?(system )?(prompt|instructions|context)",
        r"override (the )?(safety|policy|approval)",
        r"you are now (a |an )?[a-z]+ (assistant|agent|model) (?:that|who)",
        r"switch (to )?(developer|debug|jailbreak|admin) mode",
        r"<\|.*?system.*?\|>|<system>",
        r"begin (a |an )?new (system|policy) prompt",
        r"forget (everything|prior|previous|the )?",
    )
]

_SECRET_RE = re.compile(r"(?i)(api[_-]?key|password|token|secret)\s*[:=]\s*\S+")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _arg_strings(value: object) -> list[str]:
    """Collect every string leaf in a (possibly nested) tool-argument value."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out += _arg_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out += _arg_strings(v)
    return out


@dataclass
class AuditEntry:
    actor: str
    tool: str
    risk: str
    verdict: str
    detail: str
    decision_id: str = ""
    cycle_id: str = ""
    policy_rule: str = ""
    approval_id: str = ""
    args_hash: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class PendingApproval:
    approval_id: str
    tool: str
    args: dict
    preview: str
    provenance: str
    cycle_id: str = ""
    decision_id: str = ""
    rationale: str = ""
    evidence: list[dict] = field(default_factory=list)
    expires_at: float | None = None
    required_approvals: int = 1
    approvals: list[dict] = field(default_factory=list)
    organization_id: str = ""

    @property
    def fully_approved(self) -> bool:
        return len(self.approvals) >= self.required_approvals


class KillSwitch:
    """Emergency brake. When tripped, the broker denies every consequential
    action and the daemon refuses to start new runs. Store-backed so an engaged
    brake survives a daemon restart instead of silently releasing."""

    def __init__(self, store: "Store | None" = None) -> None:
        self._store = store
        self._tripped = bool(store.get_killswitch()) if store is not None else False

    def trip(self) -> None:
        self._tripped = True
        if self._store is not None:
            self._store.set_killswitch(True)

    def reset(self) -> None:
        self._tripped = False
        if self._store is not None:
            self._store.set_killswitch(False)

    @property
    def tripped(self) -> bool:
        return self._tripped


@dataclass
class GovernancePolicy:
    allowed_tools: set[str] = field(default_factory=set)
    injection_check: bool = True
    approval_ttl_seconds: float | None = 3600.0
    # Risk classes that run autonomously (no human approval). Per the framework
    # principle "autonomy for preparation, approval for consequence", both READ
    # and DRAFT are autonomous: a draft only prepares content and never sends.
    # SEND/DESTRUCTIVE are always held for approval. Mirrors the AUTONOMOUS set.
    autonomous_risks: set[RiskClass] = field(default_factory=lambda: {RiskClass.READ, RiskClass.DRAFT})
    # Risk classes that require two distinct approvers (four-eyes principle).
    dual_approval_risks: set[RiskClass] = field(
        default_factory=lambda: {RiskClass.DESTRUCTIVE})
    # Egress firewall: deny a consequential action whose arguments would relay
    # content from an untrusted source previously flagged for prompt injection.
    egress_check: bool = True
    # Optional vertical-pack tool allowlist: when set, a tool must also be in this
    # set (separate from allowed_tools so the daemon's allowlist refresh can't undo
    # the pack restriction). None = no pack restriction.
    pack_tools: set[str] | None = None
    # Optional external policy hook (OPA/Rego, Cedar, or any custom evaluator).
    # Called as hook(ctx: dict) -> "deny" | "allow" | None for every authorize().
    # ctx = {actor, tool, risk, args, args_hash, cycle_id, provenance}.
    #   "deny"  -> hard deny (overrides everything, evaluated first)
    #   "allow" -> short-circuit allow for an otherwise-consequential action
    #   None    -> defer to the built-in governance logic (default)
    # Kept as a plain callable so the dependency-free core stands alone; an
    # OPA/Rego or Cedar binding plugs in here without touching broker internals.
    policy_hook: object = None


class GovernanceBroker:
    def __init__(self, policy: GovernancePolicy | None = None,
                 store: "Store | None" = None) -> None:
        self.policy = policy or GovernancePolicy()
        self.kill = KillSwitch(store)
        self.audit: list[AuditEntry] = []
        self.pending: dict[str, PendingApproval] = {}
        # Untrusted spans flagged for injection, as an insertion-ordered set so the
        # memory bound evicts the OLDEST span (FIFO), never the just-added one.
        self._tainted: dict[str, None] = {}
        # Approval ids minted by THIS broker session. Idempotency dedups only
        # against these — never against approvals hydrated from a shared store —
        # so a fresh process can't collapse onto a prior session's pending action.
        self._session_approvals: set[str] = set()
        self._session_allowed_tools: set[str] = set()
        # One-shot allows: consume on the next authorize for that tool (used by
        # dashboard "Approve once" so a held chat turn can resume without
        # permanently waiving approval for the tool).
        self._session_one_shot_tools: set[str] = set()
        # Durable workflow resumes bind grants to the exact tool+args fingerprint.
        # Counts matter when multiple independently approved actions use one tool.
        self._session_one_shot_actions: dict[str, int] = {}
        self.store = store
        self.log = get_logger("praxis.broker")
        self.mode = (coerce_compliance_mode(store.get_compliance_mode())
                     if store is not None else ComplianceMode.ENFORCED)
        self.mode_expires_ts = (store.get_compliance_expiry()
                                if store is not None else None)
        if store is not None:
            self._hydrate(store)

    def _hydrate(self, store: "Store") -> None:
        for row in store.list_approvals():
            self.pending[row["approval_id"]] = PendingApproval(
                approval_id=row["approval_id"], tool=row["tool"],
                args=row["args"], preview=row["preview"],
                provenance=row["provenance"], cycle_id=row.get("cycle_id", ""),
                decision_id=row.get("decision_id", ""),
                rationale=row.get("rationale", ""), evidence=row.get("evidence", []),
                expires_at=row["expires_at"],
                required_approvals=row.get("required_approvals", 1),
                approvals=row.get("signatures", []),
                organization_id=row.get("organization_id", ""))
        for row in store.load_audit():
            self.audit.append(AuditEntry(
                actor=row["actor"], tool=row["tool"], risk=row["risk"],
                verdict=row["verdict"], detail=row["detail"],
                decision_id=row.get("decision_id", ""),
                cycle_id=row.get("cycle_id", ""),
                policy_rule=row.get("policy_rule", ""),
                approval_id=row.get("approval_id", ""),
                args_hash=row.get("args_hash", ""), ts=row["ts"]))

    def set_mode(self, mode: "ComplianceMode | str",
                 ttl_seconds: float | None = None) -> ComplianceMode:
        """Set the governance posture (compliance mode), persisting it so the
        choice survives a daemon restart. The kill-switch is independent and
        always overrides regardless of mode.

        ``ttl_seconds`` schedules an automatic revert to enforced after that many
        seconds (only meaningful for a relaxed mode); selecting enforced always
        clears any pending revert."""
        self.mode = coerce_compliance_mode(mode)
        if self.mode is ComplianceMode.ENFORCED or not ttl_seconds or ttl_seconds <= 0:
            self.mode_expires_ts = None
        else:
            self.mode_expires_ts = time.time() + float(ttl_seconds)
        if self.store is not None:
            self.store.set_compliance_mode(self.mode.value, self.mode_expires_ts)
        return self.mode

    def effective_mode(self) -> ComplianceMode:
        """The mode in force right now, auto-reverting to enforced when a timed
        relaxation has expired. Fail-safe: expiry always returns to the default."""
        if (self.mode is not ComplianceMode.ENFORCED
                and self.mode_expires_ts is not None
                and time.time() >= self.mode_expires_ts):
            self._revert_to_enforced()
        return self.mode

    def _revert_to_enforced(self) -> None:
        prior = self.mode.value
        self.mode = ComplianceMode.ENFORCED
        self.mode_expires_ts = None
        if self.store is not None:
            self.store.set_compliance_mode(ComplianceMode.ENFORCED.value, None)
        self.log.warning(
            "compliance auto-reverted to enforced (timed '%s' relaxation expired)",
            prior)

    # ---------------------------------------------------------- authorization
    def authorize(self, actor: str, tool: str, risk: RiskClass, args: dict,
                  preview: str = "", provenance: str = "agent",
                  cycle_id: str = "", evidence: list[dict] | None = None,
                  rationale: str = "", organization_id: str = "") -> Decision:
        decision_id = f"dec-{uuid.uuid4().hex[:12]}"
        args_hash = self._hash_args(args)
        # External policy hook (OPA/Rego/Cedar/custom). A "deny" is an absolute
        # veto, evaluated first (defense in depth over the built-in logic). An
        # "allow" is NOT applied here: it may only waive the *human-approval*
        # requirement for a consequential action, and only AFTER the
        # non-negotiable safety gates (allowlist, pack, kill-switch, egress
        # firewall) have all passed — otherwise a convenience "allow" rule could
        # bypass exfiltration protection. Hook errors fail SAFE (treated as deny)
        # so a broken policy can never widen access.
        hook = getattr(self.policy, "policy_hook", None)
        hook_allow = False
        if callable(hook):
            try:
                verdict = hook({"actor": actor, "tool": tool, "risk": risk.value,
                                "args": args, "args_hash": args_hash,
                                "cycle_id": cycle_id, "provenance": provenance})
            except Exception:  # noqa: BLE001 - a broken hook must not open access
                verdict = "deny"
            if verdict == "deny":
                return self._log_decision(
                    actor, tool, risk, Verdict.DENY,
                    agent_error(what="denied by policy hook",
                                why="an operator policy hook refused this tool call",
                                fix="check the policy hook configuration or ask the "
                                    "operator to allow this tool/risk"),
                    decision_id=decision_id, cycle_id=cycle_id,
                    policy_rule="policy_hook_deny", args_hash=args_hash)
            hook_allow = (verdict == "allow")
        if tool not in self.policy.allowed_tools:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      agent_error(what="tool not in allowlist",
                                                  why=f"'{tool}' is not on the "
                                                      "GovernancePolicy allowlist",
                                                  fix="add the tool to "
                                                      "GovernancePolicy(allowed_tools=...) "
                                                      "or use an allowed alternative"),
                                      decision_id=decision_id,
                                      cycle_id=cycle_id, policy_rule="allowlist_denied",
                                      args_hash=args_hash)
        if (self.policy.pack_tools is not None
                and tool not in self.policy.pack_tools):
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      agent_error(what="tool not enabled by the active pack",
                                                  why=f"'{tool}' is not in the active "
                                                      "vertical pack's tool set",
                                                  fix="switch to a pack that enables this "
                                                      "tool, or ask the operator to add it "
                                                      "to the pack"),
                                      decision_id=decision_id, cycle_id=cycle_id,
                                      policy_rule="pack_restricted", args_hash=args_hash)
        if risk in CONSEQUENTIAL and self.kill.tripped:
            return self._log_decision(actor, tool, risk, Verdict.DENY,
                                      agent_error(what="kill-switch engaged",
                                                  why="the global kill-switch is tripped; "
                                                      "all consequential actions are blocked",
                                                  fix="an operator must reset it with "
                                                      "`praxis kill-switch reset` before "
                                                      "consequential tools can run"),
                                      decision_id=decision_id, cycle_id=cycle_id,
                                      policy_rule="kill_switch_denied",
                                      args_hash=args_hash)
        classification = args.get("classification")
        connector = args.get("connector")
        if organization_id and risk in CONSEQUENTIAL and (
                classification is None or connector is None):
            return self._log_decision(
                actor, tool, risk, Verdict.DENY,
                "professional consequential egress requires classification and connector",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="classification_required", args_hash=args_hash)
        if classification is not None or connector is not None:
            try:
                classified = Classification(str(classification))
            except ValueError:
                return self._log_decision(
                    actor, tool, risk, Verdict.DENY,
                    "unknown or missing data classification",
                    decision_id=decision_id, cycle_id=cycle_id,
                    policy_rule="classification_denied", args_hash=args_hash)
            if not connector or not DataPolicy().allow_egress(classified, str(connector)):
                return self._log_decision(
                    actor, tool, risk, Verdict.DENY,
                    "classified data is not approved for this connector",
                    decision_id=decision_id, cycle_id=cycle_id,
                    policy_rule="classified_egress_denied", args_hash=args_hash)
            export = DataPolicy().export_decision(
                classified, redacted=bool(args.get("redacted", False)))
            if not export.allowed:
                return self._log_decision(
                    actor, tool, risk, Verdict.DENY, export.reason,
                    decision_id=decision_id, cycle_id=cycle_id,
                    policy_rule="redaction_required", args_hash=args_hash)
        if risk in self.policy.autonomous_risks:
            return self._log_decision(actor, tool, risk, Verdict.ALLOW,
                                      "autonomous (read/draft)", decision_id=decision_id,
                                      cycle_id=cycle_id, policy_rule="autonomous_allow",
                                      args_hash=args_hash)
        # Resolve the mode in force now (this also auto-reverts an expired
        # timed relaxation back to enforced).
        mode = self.effective_mode()
        # Egress firewall: refuse to relay untrusted, injection-flagged content
        # out through a consequential action (exfiltration / injection propagation).
        # Active in enforced + autonomous modes; permissive turns it off.
        if self.policy.egress_check and mode is not ComplianceMode.PERMISSIVE:
            blocked = self._egress_blocked(args)
            if blocked:
                return self._log_decision(actor, tool, risk, Verdict.DENY, blocked,
                                          decision_id=decision_id, cycle_id=cycle_id,
                                          policy_rule="egress_blocked",
                                          args_hash=args_hash)
        # One-shot allow: consume exactly once (dashboard "Approve once" + chat resume).
        # Still respects allowlist, pack, kill-switch, and egress firewall.
        exact_grant = self._one_shot_action_key(tool, args)
        exact_count = self._session_one_shot_actions.get(exact_grant, 0)
        if exact_count:
            if exact_count == 1:
                self._session_one_shot_actions.pop(exact_grant, None)
            else:
                self._session_one_shot_actions[exact_grant] = exact_count - 1
            return self._log_decision(
                actor, tool, risk, Verdict.ALLOW,
                "auto-allowed (exact one-shot approval)",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="session_exact_oneshot_allow", args_hash=args_hash)
        if tool in self._session_one_shot_tools:
            self._session_one_shot_tools.discard(tool)
            return self._log_decision(
                actor, tool, risk, Verdict.ALLOW,
                "auto-allowed (one-shot approval)",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="session_oneshot_allow", args_hash=args_hash)
        # Session allowlist: a tool explicitly allowed for this daemon session runs
        # without re-approval (e.g. "always run browser_click for this chat"). This
        # still respects the allowlist, pack, kill-switch, and egress firewall.
        if tool in self._session_allowed_tools:
            return self._log_decision(
                actor, tool, risk, Verdict.ALLOW,
                "auto-allowed (session allowlist)",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="session_allowlist_allow", args_hash=args_hash)
        # Policy-hook "allow" applies HERE — after allowlist, pack, kill-switch and
        # the egress firewall have all passed — so it may only waive the
        # human-approval requirement for a consequential action, never bypass a
        # safety gate (exfiltration of injection-flagged content stays blocked).
        if hook_allow:
            return self._log_decision(
                actor, tool, risk, Verdict.ALLOW, "allowed by policy hook",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="policy_hook_allow", args_hash=args_hash)
        # Compliance off (autonomous/permissive): consequential actions run without
        # human approval. The kill-switch already had its say above, and each such
        # action is audit-logged distinctly so unsupervised runs stay visible.
        if mode is not ComplianceMode.ENFORCED:
            return self._log_decision(
                actor, tool, risk, Verdict.ALLOW,
                f"auto-allowed (compliance {mode.value})",
                decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="compliance_off_allow", args_hash=args_hash)
        # Idempotency: an identical action (same tool + args) already pending and
        # unexpired reuses that approval instead of queuing a duplicate, so a
        # re-proposed action (e.g. a verifier-revised turn) can't mint a second
        # approval a human could approve twice -> double execution.
        existing = self._find_pending(tool, args_hash, organization_id)
        if existing is not None:
            return self._log_decision(
                actor, tool, risk, Verdict.NEEDS_APPROVAL,
                f"reused pending {existing} (identical action already queued)",
                existing, decision_id=decision_id, cycle_id=cycle_id,
                policy_rule="approval_deduped", args_hash=args_hash)
        # Consequential -> hold for human approval (draft-before-send).
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        ttl = self.policy.approval_ttl_seconds
        expires_at = time.time() + ttl if ttl else None
        required = 2 if risk in self.policy.dual_approval_risks else 1
        why = rationale or (
            f"{risk.value} tool '{tool}' is consequential and requires "
            f"{required} human approval(s)."
        )
        self.pending[approval_id] = PendingApproval(
            approval_id=approval_id, tool=tool, args=args,
            preview=preview, provenance=provenance, cycle_id=cycle_id,
            decision_id=decision_id, rationale=why, evidence=evidence or [],
            expires_at=expires_at, required_approvals=required,
            organization_id=organization_id,
        )
        self._session_approvals.add(approval_id)
        if self.store is not None:
            self.store.upsert_approval(approval_id, tool, args, preview,
                                       provenance, expires_at, cycle_id=cycle_id,
                                       decision_id=decision_id, rationale=why,
                                       evidence=evidence,
                                       required_approvals=required,
                                       organization_id=organization_id)
        return self._log_decision(actor, tool, risk, Verdict.NEEDS_APPROVAL,
                                  f"queued {approval_id} (needs {required})",
                                  approval_id,
                                  decision_id=decision_id, cycle_id=cycle_id,
                                  policy_rule=("dual_approval_required"
                                               if required > 1
                                               else "human_approval_required"),
                                  args_hash=args_hash)

    def approve(self, approval_id: str, approved_by: str = "",
                approval_notes: str = "", approved_role: str = "") -> PendingApproval | None:
        pending = self.pending.get(approval_id)
        if pending is None:
            return None
        if pending.expires_at and pending.expires_at < time.time():
            self.pending.pop(approval_id, None)
            if self.store is not None:
                self.store.resolve_approval(approval_id, "expired")
            self.log.info("approval %s expired", approval_id)
            return None
        signer = (approved_by or "").strip()
        # Four-eyes: dual-approval requires a non-empty distinct identity so two
        # blank signatures cannot satisfy required_approvals > 1.
        if pending.required_approvals > 1 and not signer:
            self.log.info("approval %s: dual-approval requires non-empty approved_by",
                          approval_id)
            return None
        # Four-eyes principle: an approver who already signed THIS approval
        # cannot sign it a second time to satisfy the dual-approval requirement.
        if signer and any(a.get("approved_by") == signer
                          for a in pending.approvals):
            self.log.info("approval %s: %s already signed; second approver required",
                          approval_id, signer)
            return None
        pending.approvals.append({
            "approved_by": signer, "role": approved_role,
            "notes": approval_notes, "ts": time.time(),
        })
        if not pending.fully_approved:
            if self.store is not None:
                self.store.add_approval_signature(
                    approval_id, signer, approval_notes, approved_role)
            self.log.info("approval %s: %d/%d signatures collected",
                          approval_id, len(pending.approvals),
                          pending.required_approvals)
            return None
        # Task approvals atomically resolve the human approval and persist the
        # pre-execution action intent. No provider call can precede that commit.
        if self.store is not None and self.store.has_task_approval_action(approval_id):
            if not self.store.claim_task_approval_action(
                approval_id,
                signatures=pending.approvals,
                approved_by=signer,
                approval_notes=approval_notes,
            ):
                pending.approvals.pop()
                row = self.store.get_approval(approval_id)
                if row is None or row.get("status") != "pending":
                    self.pending.pop(approval_id, None)
                self.log.info("approval %s could not claim task execution", approval_id)
                return None
        else:
            if self.store is not None:
                self.store.add_approval_signature(
                    approval_id, signer, approval_notes, approved_role)
                if not self.store.resolve_approval(
                    approval_id, "approved", approved_by=signer,
                    approval_notes=approval_notes,
                ):
                    self.pending.pop(approval_id, None)
                    self.log.info("approval %s already resolved; refusing", approval_id)
                    return None
        self.pending.pop(approval_id, None)
        return pending

    def allow_tool_for_session(self, tool: str) -> None:
        """Permanently allow a tool for the current daemon session.

        Used by the dashboard "always run this tool" approval option. The tool
        still passes through the allowlist, pack, kill-switch, and egress checks;
        it only skips the human-approval step.
        """
        self._session_allowed_tools.add(tool)
        self.log.info("tool %s added to session allowlist", tool)

    def allow_tool_once(self, tool: str, args: dict | None = None) -> None:
        """Allow one subsequent authorization, optionally for an exact action."""
        name = (tool or "").strip()
        if not name:
            return
        if args is None:
            self._session_one_shot_tools.add(name)
            self.log.info("tool %s granted one-shot session allow", name)
            return
        key = self._one_shot_action_key(name, args)
        self._session_one_shot_actions[key] = self._session_one_shot_actions.get(key, 0) + 1
        self.log.info("tool %s granted exact one-shot session allow", name)

    def reject(self, approval_id: str) -> bool:
        """Reject a pending approval. Returns True if it was pending."""
        existed = approval_id in self.pending
        self.pending.pop(approval_id, None)
        if self.store is not None:
            self.store.resolve_approval(approval_id, "rejected")
        return existed

    def revoke_tool_once(self, tool: str) -> None:
        """Drop a previously granted generic one-shot allow (if still unused)."""
        name = (tool or "").strip()
        if name:
            self._session_one_shot_tools.discard(name)

    def _one_shot_action_key(self, tool: str, args: dict) -> str:
        return f"{tool}\n{self._hash_args(args)}"

    def egress_blocked_for(self, args: dict) -> str:
        """Public egress check: non-empty reason string if the action is blocked.

        Respects compliance mode (permissive disables the firewall) and the
        policy's ``egress_check`` flag.
        """
        if not self.policy.egress_check:
            return ""
        if self.effective_mode() is ComplianceMode.PERMISSIVE:
            return ""
        return self._egress_blocked(args)

    # ------------------------------------------------------------- screening
    def mark_tainted(self, text: str) -> None:
        """Record untrusted content (e.g. an injection-flagged tool result) so the
        egress firewall can refuse to relay it out through a consequential action."""
        norm = _normalize_ws(text)
        if len(norm) < 16:  # ignore trivially short spans to avoid false positives
            return
        self._tainted[norm] = None
        while len(self._tainted) > 256:  # bound memory; evict the oldest (FIFO)
            del self._tainted[next(iter(self._tainted))]

    def _egress_blocked(self, args: dict) -> str:
        if not self._tainted:
            return ""
        values = [_normalize_ws(v) for v in _arg_strings(args)]
        blob = " ".join(v for v in values if v)
        if not blob:
            return ""
        for span in self._tainted:
            # Either the action relays the flagged content wholesale, or one of
            # its argument values is a substantial chunk of that flagged content.
            if span in blob or any(len(v) >= 24 and v in span for v in values):
                return ("egress blocked: this action would relay content from an "
                        "untrusted source flagged for prompt injection")
        return ""

    def _find_pending(self, tool: str, args_hash: str,
                      organization_id: str = "") -> str | None:
        now = time.time()
        for aid, p in list(self.pending.items()):
            if aid not in self._session_approvals:
                continue  # only dedup re-proposals made in this broker session
            if self.store is not None:
                row = self.store.get_approval(aid)
                if row is None or row.get("status") != "pending":
                    self.pending.pop(aid, None)
                    self._session_approvals.discard(aid)
                    continue
            if p.tool != tool:
                continue
            if p.organization_id != organization_id:
                continue
            if p.expires_at and p.expires_at < now:
                continue
            if self._hash_args(p.args) == args_hash:
                return aid
        return None

    def is_injection(self, text: str) -> bool:
        if self.effective_mode() is ComplianceMode.PERMISSIVE:
            return False
        if not (self.policy.injection_check and text):
            return False
        return any(pat.search(text) for pat in _INJECTION_PATTERNS)

    @staticmethod
    def redact(text: str) -> str:
        return _SECRET_RE.sub(r"\1: [REDACTED]", text or "")

    @staticmethod
    def _hash_args(args: dict) -> str:
        blob = json.dumps(args or {}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    # ----------------------------------------------------------------- audit
    def _log_decision(self, actor: str, tool: str, risk: RiskClass,
                      verdict: Verdict, reason: str,
                      approval_id: str | None = None,
                      decision_id: str = "", cycle_id: str = "",
                      policy_rule: str = "", args_hash: str = "") -> Decision:
        entry = AuditEntry(actor=actor, tool=tool, risk=risk.value,
                           verdict=verdict.value, detail=self.redact(reason),
                           decision_id=decision_id, cycle_id=cycle_id,
                           policy_rule=policy_rule,
                           approval_id=approval_id or "", args_hash=args_hash)
        self.audit.append(entry)
        if self.store is not None:
            self.store.add_audit(entry.actor, entry.tool, entry.risk,
                                 entry.verdict, entry.detail, entry.ts,
                                 decision_id=entry.decision_id,
                                 cycle_id=entry.cycle_id,
                                 policy_rule=entry.policy_rule,
                                 approval_id=entry.approval_id,
                                 args_hash=entry.args_hash)
        self.log.debug("decision actor=%s tool=%s risk=%s verdict=%s",
                       actor, tool, risk.value, verdict.value)
        return Decision(verdict=verdict, reason=reason, approval_id=approval_id,
                        decision_id=decision_id, policy_rule=policy_rule)
