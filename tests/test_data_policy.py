"""Classification, retention, legal hold, and egress policy contracts."""
import time

import pytest

from hybridagent.data_policy import (
    Classification,
    DataPolicy,
    DataPolicyError,
    RetentionRule,
)


def test_classification_vocabulary_is_closed():
    assert Classification.PHI.value == "phi"
    assert Classification.EDUCATION_RECORD.value == "education_record"
    with pytest.raises(ValueError):
        Classification("secret-ish")


def test_retention_decision_is_deterministic():
    policy = DataPolicy({
        Classification.INTERNAL: RetentionRule(days=30),
        Classification.EVIDENCE: RetentionRule(days=365),
    })
    now = 2_000_000.0
    assert policy.disposition(Classification.INTERNAL, created_ts=now - 31 * 86400,
                              now=now) == "delete"
    assert policy.disposition(Classification.EVIDENCE, created_ts=now - 31 * 86400,
                              now=now) == "retain"


def test_legal_hold_overrides_expiry_and_delete():
    policy = DataPolicy({Classification.PRIVILEGED: RetentionRule(days=1)})
    old = time.time() - 100 * 86400
    assert policy.disposition(Classification.PRIVILEGED, created_ts=old,
                              legal_hold=True) == "hold"
    with pytest.raises(DataPolicyError, match="legal hold"):
        policy.authorize_delete(Classification.PRIVILEGED, created_ts=old,
                                legal_hold=True)


def test_egress_restrictions_default_deny_sensitive_connectors():
    policy = DataPolicy()
    assert policy.allow_egress(Classification.PUBLIC, "public_web")
    assert not policy.allow_egress(Classification.PHI, "public_web")
    assert not policy.allow_egress(Classification.PRIVILEGED, "public_web")
    assert policy.allow_egress(Classification.PHI, "approved_healthcare_system")


def test_export_requires_redaction_for_sensitive_data():
    policy = DataPolicy()
    denied = policy.export_decision(Classification.PHI, redacted=False)
    assert not denied.allowed and denied.reason == "redaction_required"
    allowed = policy.export_decision(Classification.PHI, redacted=True)
    assert allowed.allowed


def test_governance_broker_enforces_classified_connector_egress():
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict

    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_record"}))
    denied = broker.authorize(
        "agent", "send_record", RiskClass.SEND,
        {"classification": "phi", "connector": "public_web"})
    assert denied.verdict is Verdict.DENY
    assert denied.policy_rule == "classified_egress_denied"
    allowed_connector = broker.authorize(
        "agent", "send_record", RiskClass.SEND,
        {"classification": "phi", "connector": "approved_healthcare_system",
         "redacted": True})
    assert allowed_connector.verdict is Verdict.NEEDS_APPROVAL
