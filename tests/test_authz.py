"""RBAC/ABAC and purpose-of-use authorization contracts."""
from hybridagent.authz import AccessContext, AuthorizationPolicy


def context(*roles, organization_id="org-a", purpose="service_delivery"):
    return AccessContext("usr-1", organization_id, frozenset(roles), purpose)


def test_cross_organization_access_is_denied():
    decision = AuthorizationPolicy().authorize(
        context("professional"), "read", resource_organization_id="org-b")
    assert not decision.allowed
    assert decision.reason == "organization_scope_denied"


def test_reviewer_cannot_mutate_or_execute_tools():
    policy = AuthorizationPolicy()
    assert policy.authorize(context("reviewer"), "read",
                            resource_organization_id="org-a").allowed
    assert not policy.authorize(context("reviewer"), "write",
                                resource_organization_id="org-a").allowed
    assert not policy.authorize(context("reviewer"), "execute_tool",
                                resource_organization_id="org-a").allowed


def test_admin_and_professional_permissions_are_explicit():
    policy = AuthorizationPolicy()
    assert policy.authorize(context("organization_admin"), "manage_members",
                            resource_organization_id="org-a").allowed
    assert policy.authorize(context("professional"), "write",
                            resource_organization_id="org-a").allowed
    assert not policy.authorize(context("member"), "approve_decision",
                                resource_organization_id="org-a").allowed


def test_sensitive_classification_requires_valid_purpose():
    policy = AuthorizationPolicy()
    denied = policy.authorize(
        context("professional", purpose="marketing"), "read",
        resource_organization_id="org-a", classification="phi")
    assert not denied.allowed and denied.reason == "purpose_of_use_denied"
    allowed = policy.authorize(
        context("professional", purpose="treatment"), "read",
        resource_organization_id="org-a", classification="phi")
    assert allowed.allowed


def test_break_glass_requires_reason_and_is_auditable(tmp_path):
    from hybridagent.persistence import Store

    store = Store(tmp_path / "praxis.db")
    policy = AuthorizationPolicy(store)
    denied = policy.authorize(
        context("professional", organization_id="org-a"), "read",
        resource_organization_id="org-b", break_glass=True)
    assert not denied.allowed
    allowed = policy.authorize(
        context("professional", organization_id="org-a"), "read",
        resource_organization_id="org-b", break_glass=True,
        break_glass_reason="Emergency continuity of care")
    assert allowed.allowed and allowed.break_glass
    assert allowed.audit_event["reason"] == "Emergency continuity of care"
    events = store.list_compliance_events(cycle_id="professional-access")
    assert events[0]["event_type"] == "break_glass_access"
    assert events[0]["payload"]["actor"] == "usr-1"
    role_denied = policy.authorize(
        context("unknown", organization_id="org-a"), "read",
        resource_organization_id="org-b", break_glass=True,
        break_glass_reason="No entitlement")
    assert not role_denied.allowed


def test_approval_signature_records_actor_and_role(tmp_path):
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
    from hybridagent.persistence import Store

    store = Store(tmp_path / "praxis.db")
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"send_email"}), store=store)
    decision = broker.authorize("agent", "send_email", RiskClass.SEND, {})
    assert decision.approval_id is not None
    released = broker.approve(
        decision.approval_id, approved_by="usr-1", approved_role="professional")
    assert released is not None
    row = store.get_approval(decision.approval_id)
    assert row is not None
    assert row["signatures"][0]["approved_by"] == "usr-1"
    assert row["signatures"][0]["role"] == "professional"


def test_approval_ownership_is_persisted_and_distinct(tmp_path):
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
    from hybridagent.persistence import Store

    store = Store(tmp_path / "praxis.db")
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"send_email"}), store=store)
    decision = broker.authorize(
        "agent", "send_email", RiskClass.SEND,
        {"classification": "public", "connector": "public_web",
         "redacted": True}, organization_id="org-a")
    assert decision.approval_id is not None
    row = store.get_approval(decision.approval_id)
    assert row is not None and row["organization_id"] == "org-a"


def test_identical_actions_are_deduplicated_only_within_tenant(tmp_path):
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
    from hybridagent.persistence import Store

    store = Store(tmp_path / "praxis.db")
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"send_email"}), store=store)
    args = {"classification": "public", "connector": "public_web",
            "redacted": True}
    first = broker.authorize(
        "agent", "send_email", RiskClass.SEND, args, organization_id="org-a")
    same_tenant = broker.authorize(
        "agent", "send_email", RiskClass.SEND, args, organization_id="org-a")
    other_tenant = broker.authorize(
        "agent", "send_email", RiskClass.SEND, args, organization_id="org-b")
    assert first.approval_id is not None
    assert same_tenant.approval_id == first.approval_id
    assert other_tenant.approval_id is not None
    assert other_tenant.approval_id != first.approval_id
    first_row = store.get_approval(first.approval_id)
    other_row = store.get_approval(other_tenant.approval_id)
    assert first_row is not None and first_row["organization_id"] == "org-a"
    assert other_row is not None and other_row["organization_id"] == "org-b"


def test_professional_egress_fails_closed_without_metadata():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict

    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    decision = broker.authorize(
        "agent", "send_email", RiskClass.SEND, {}, organization_id="org-a")
    assert decision.verdict is Verdict.DENY
    assert decision.policy_rule == "classification_required"
