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

from .errors import agent_error


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
            raise ValidationError(agent_error(
                what=f"{path}: expected integer, got {type(value).__name__}",
                why=f"the '{path}' argument must be an integer",
                fix=f"change the value to an integer, e.g. {int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0}",
            ))
        return
    if schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(agent_error(
                what=f"{path}: expected number, got {type(value).__name__}",
                why=f"the '{path}' argument must be a number",
                fix="change the value to a number, e.g. 1.0",
            ))
        return
    expected = _TYPE_CHECKS.get(schema_type)
    if expected is None:
        return                                   # unknown types: pass through
    if not isinstance(value, expected):
        raise ValidationError(agent_error(
            what=f"{path}: expected {schema_type}, got {type(value).__name__}",
            why=f"the '{path}' argument must be a {schema_type}",
            fix=f"change the value to a {schema_type}",
        ))


def validate(value: Any, schema: dict, path: str = "args") -> None:
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        if not any(_passes_type(value, t) for t in types):
            raise ValidationError(agent_error(
                what=f"{path}: expected one of {types}, got {type(value).__name__}",
                why=f"the '{path}' argument must be one of those types",
                fix=f"change the value to one of {types}",
            ))
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(agent_error(
            what=f"{path}: value {value!r} not in enum {schema['enum']}",
            why=f"the '{path}' argument must be one of the allowed values",
            fix=f"use one of {schema['enum']}",
        ))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValidationError(agent_error(
                what=f"{path}: string shorter than minLength={schema['minLength']}",
                why=f"the '{path}' string must be at least {schema['minLength']} chars",
                fix=f"provide a string with at least {schema['minLength']} characters",
            ))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValidationError(agent_error(
                what=f"{path}: string longer than maxLength={schema['maxLength']}",
                why=f"the '{path}' string must be at most {schema['maxLength']} chars",
                fix=f"shorten the string to at most {schema['maxLength']} characters",
            ))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(agent_error(
                what=f"{path}: value {value} below minimum {schema['minimum']}",
                why=f"the '{path}' value must be >= {schema['minimum']}",
                fix=f"use a value of at least {schema['minimum']}",
            ))
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(agent_error(
                what=f"{path}: value {value} above maximum {schema['maximum']}",
                why=f"the '{path}' value must be <= {schema['maximum']}",
                fix=f"use a value of at most {schema['maximum']}",
            ))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(agent_error(
                what=f"{path}: array shorter than minItems={schema['minItems']}",
                why=f"the '{path}' array must have at least {schema['minItems']} items",
                fix=f"add items until the array has at least {schema['minItems']} elements",
            ))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValidationError(agent_error(
                what=f"{path}: array longer than maxItems={schema['maxItems']}",
                why=f"the '{path}' array must have at most {schema['maxItems']} items",
                fix=f"remove items until the array has at most {schema['maxItems']} elements",
            ))
        if "items" in schema:
            for i, elem in enumerate(value):
                validate(elem, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                raise ValidationError(agent_error(
                    what=f"{path}: missing required property {key!r}",
                    why=f"the '{path}' object must include the '{key}' key",
                    fix=f"add the '{key}' key to the arguments",
                ))
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = [k for k in value if k not in props]
            if extras:
                raise ValidationError(agent_error(
                    what=f"{path}: unexpected properties {extras}",
                    why=f"the '{path}' object only accepts {list(props)}",
                    fix=f"remove the extra keys {extras} or add them to the schema",
                ))


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
