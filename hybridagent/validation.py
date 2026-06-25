"""Lightweight, dependency-free JSON-schema-style validation for tool arguments.

In a regulated environment, tools should reject malformed arguments **before**
they execute, not in the middle of a Graph call. This validator supports the
subset of JSON Schema we actually use to describe tool parameters:

* ``type``: ``object`` / ``string`` / ``integer`` / ``number`` / ``boolean`` /
  ``array`` / ``null``;
* ``required``: list of required keys (for objects);
* ``properties``: nested per-property schemas;
* ``items``: schema applied to every element of an array;
* ``enum``: an allowed value list;
* ``minLength`` / ``maxLength`` / ``minimum`` / ``maximum`` / ``minItems`` /
  ``maxItems``;
* ``additionalProperties`` defaults to true; pass ``False`` to forbid extras.

It is intentionally permissive when a tool has *no* ``parameters`` schema — we
don't want to break existing tools that haven't been annotated yet. Use it
opt-in by calling :func:`validate_tool_args`.
"""
from __future__ import annotations

from typing import Any


class ValidationError(ValueError):
    """Raised when tool arguments fail the declared schema."""


_TYPE_CHECKS = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _check_type(value: Any, schema_type: str, path: str) -> None:
    if schema_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{path}: expected integer, got {type(value).__name__}")
        return
    if schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{path}: expected number, got {type(value).__name__}")
        return
    expected = _TYPE_CHECKS.get(schema_type)
    if expected is None:
        return                                   # unknown types: pass through
    if not isinstance(value, expected):
        raise ValidationError(
            f"{path}: expected {schema_type}, got {type(value).__name__}")


def validate(value: Any, schema: dict, path: str = "args") -> None:
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        if not any(_passes_type(value, t) for t in types):
            raise ValidationError(
                f"{path}: expected one of {types}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(
            f"{path}: value {value!r} not in enum {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError(
                f"{path}: string shorter than minLength={schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError(
                f"{path}: string longer than maxLength={schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(
                f"{path}: value {value} below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(
                f"{path}: value {value} above maximum {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(
                f"{path}: array shorter than minItems={schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(
                f"{path}: array longer than maxItems={schema['maxItems']}")
        if "items" in schema:
            for i, elem in enumerate(value):
                validate(elem, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                raise ValidationError(f"{path}: missing required property {key!r}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = [k for k in value if k not in props]
            if extras:
                raise ValidationError(
                    f"{path}: unexpected properties {extras}")


def _passes_type(value: Any, schema_type: str) -> bool:
    try:
        _check_type(value, schema_type, "")
    except ValidationError:
        return False
    return True


def validate_tool_args(tool, args: dict) -> None:
    """Validate ``args`` against ``tool.parameters`` if present; no-op otherwise."""
    schema = getattr(tool, "parameters", None)
    if not schema:
        return
    validate(args or {}, schema, path=f"{tool.name}.args")
