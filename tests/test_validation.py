"""Tests for the lightweight JSON-schema validator used by the planner."""
from hybridagent.broker import RiskClass
from hybridagent.tools import Tool, default_registry
from hybridagent.validation import ValidationError, validate_tool_args


def test_no_schema_is_permissive():
    tool = Tool("open", RiskClass.READ, "open", lambda **_: "ok")
    validate_tool_args(tool, {"anything": 123})


def test_required_property():
    reg = default_registry()
    with pytest_raises(ValidationError):
        validate_tool_args(reg.get("delete_file"), {})
    validate_tool_args(reg.get("delete_file"), {"name": "x.txt"})


def test_additional_properties_forbidden():
    reg = default_registry()
    with pytest_raises(ValidationError):
        validate_tool_args(reg.get("search_mail"), {"query": "x", "extra": True})
    validate_tool_args(reg.get("search_mail"), {"query": "x"})


def test_type_checking():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["count"],
        "additionalProperties": False,
    }
    tool = Tool("demo", RiskClass.READ, "demo", lambda **_: "ok", parameters=schema)
    validate_tool_args(tool, {"count": 1, "ratio": 1.5, "flag": True, "tags": ["a"]})
    with pytest_raises(ValidationError):
        validate_tool_args(tool, {"count": "1"})
    with pytest_raises(ValidationError):
        validate_tool_args(tool, {"count": 1, "tags": [1]})


def pytest_raises(exc):
    import pytest
    return pytest.raises(exc)
