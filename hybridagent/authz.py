"""Professional RBAC/ABAC and purpose-of-use authorization policy."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .persistence import Store


@dataclass(frozen=True)
class AccessContext:
    user_id: str
    organization_id: str
    roles: frozenset[str]
    purpose_of_use: str


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    break_glass: bool = False
    audit_event: dict = field(default_factory=dict)


_ROLE_ACTIONS = {
    "organization_admin": frozenset({
        "read", "write", "execute_tool", "manage_members", "approve_content",
        "approve_decision", "attest_evidence",
    }),
    "workspace_admin": frozenset({
        "read", "write", "execute_tool", "approve_content", "approve_decision",
        "attest_evidence",
    }),
    "professional": frozenset({
        "read", "write", "execute_tool", "approve_content", "approve_decision",
        "attest_evidence",
    }),
    "reviewer": frozenset({"read", "comment", "approve_content"}),
    "member": frozenset({"read", "write"}),
    "external_collaborator": frozenset({"read", "comment"}),
    "auditor": frozenset({"read", "audit"}),
}

_CLASSIFICATION_PURPOSES = {
    "phi": frozenset({"treatment", "payment", "healthcare_operations"}),
    "education_record": frozenset({"education_delivery", "education_administration"}),
    "privileged": frozenset({"legal_service", "legal_review"}),
    "evidence": frozenset({"investigation", "professional_review", "service_delivery"}),
}


class AuthorizationPolicy:
    """Deny-by-default policy independent of transport and UI."""

    def __init__(self, store: "Store | None" = None) -> None:
        self.store = store

    def authorize(
        self, context: AccessContext, action: str, *,
        resource_organization_id: str, classification: str = "internal",
        break_glass: bool = False, break_glass_reason: str = "",
    ) -> AuthorizationDecision:
        if resource_organization_id != context.organization_id:
            role_actions: set[str] = set()
            for role in context.roles:
                role_actions.update(_ROLE_ACTIONS.get(role, ()))
            purposes = _CLASSIFICATION_PURPOSES.get(classification)
            if (action != "read" or "read" not in role_actions
                    or (purposes is not None
                        and context.purpose_of_use not in purposes)):
                return AuthorizationDecision(False, "break_glass_policy_denied")
            if not break_glass or not break_glass_reason.strip():
                return AuthorizationDecision(False, "organization_scope_denied")
            if action != "read":
                return AuthorizationDecision(False, "break_glass_read_only")
            audit_event = {
                "event_type": "break_glass_access", "actor": context.user_id,
                "source_organization_id": context.organization_id,
                "resource_organization_id": resource_organization_id,
                "action": action, "reason": break_glass_reason.strip(),
                "ts": time.time()}
            if self.store is not None:
                self.store.add_compliance_event(
                    "professional-access", "break_glass_access", audit_event,
                    ref_id=context.user_id)
            return AuthorizationDecision(
                True, "break_glass_allowed", True,
                audit_event,
            )

        purposes = _CLASSIFICATION_PURPOSES.get(classification)
        if purposes is not None and context.purpose_of_use not in purposes:
            return AuthorizationDecision(False, "purpose_of_use_denied")

        allowed_actions: set[str] = set()
        for role in context.roles:
            allowed_actions.update(_ROLE_ACTIONS.get(role, ()))
        if action not in allowed_actions:
            return AuthorizationDecision(False, "role_action_denied")
        return AuthorizationDecision(True, "role_action_allowed")
