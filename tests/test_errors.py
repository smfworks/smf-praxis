"""Tests for agent-oriented error messages (H07).

Verifies the what + why + how-to-fix convention is in use in the governance
modules and that the helper formats it correctly.
"""
from __future__ import annotations

from hybridagent.errors import agent_error
from hybridagent.validation import ValidationError, validate


def test_agent_error_format_three_parts():
    msg = agent_error(what="absolute paths not allowed",
                      why="sandbox to PRAXIS_WORK_DIR",
                      fix="use a relative path")
    assert "absolute paths not allowed" in msg
    assert "sandbox to PRAXIS_WORK_DIR" in msg
    assert "use a relative path" in msg
    assert " -- " in msg


def test_agent_error_omits_empty_parts():
    msg = agent_error(what="denied", why="", fix="")
    assert msg == "denied"


def test_agent_error_two_parts():
    msg = agent_error(what="denied", why="policy hook", fix="")
    assert msg == "denied -- policy hook"


def test_validation_error_includes_fix():
    """A missing-required-property error must include how-to-fix guidance."""
    schema = {"type": "object", "required": ["email"],
              "properties": {"email": {"type": "string"}}}
    try:
        validate({}, schema, path="send_email.args")
        raise AssertionError("expected ValidationError")
    except ValidationError as exc:
        msg = str(exc)
        assert "missing required property" in msg
        assert "add the 'email' key" in msg, f"missing fix in: {msg}"


def test_validation_type_error_includes_fix():
    """A type-mismatch error must include how-to-fix guidance."""
    schema = {"type": "integer"}
    try:
        validate("not a number", schema, path="tool.args.count")
        raise AssertionError("expected ValidationError")
    except ValidationError as exc:
        msg = str(exc)
        assert "expected" in msg and "integer" in msg
        assert "change the value" in msg, f"missing fix: {msg}"


def test_validation_enum_error_includes_fix():
    """An enum error must include the allowed values as fix guidance."""
    schema = {"enum": ["a", "b", "c"]}
    try:
        validate("z", schema, path="tool.args.choice")
        raise AssertionError("expected ValidationError")
    except ValidationError as exc:
        msg = str(exc)
        assert "not in enum" in msg
        assert "use one of" in msg and "['a', 'b', 'c']" in msg, f"missing fix: {msg}"


def test_broker_denial_uses_agent_error():
    """Broker denials must carry why + fix (H07 convention)."""
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set()))
    decision = broker.authorize(actor="test", tool="send_email",
                                 risk=__import__("hybridagent.broker",
                                                 fromlist=["RiskClass"]).RiskClass.SEND,
                                 args={}, preview="send_email()",
                                 provenance="test")
    assert not decision.verdict.value == "allow"
    msg = decision.reason
    assert "not in allowlist" in msg
    assert "add the tool to" in msg, f"broker denial missing fix: {msg}"