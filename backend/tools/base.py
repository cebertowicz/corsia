"""Base class for agent tools."""

from abc import ABC, abstractmethod
import json
import math
from pathlib import Path
from typing import Any


def _container_type(schema: Any) -> str | None:
    """The object or array type a property declares, directly or by every oneOf branch.

    Any other schema shape (a boolean schema, a list type, mixed branches) has
    none, so arguments from external tool schemas are left as they arrive.
    """
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    branches = schema.get("oneOf")
    if declared is None and isinstance(branches, list) and branches:
        kinds = [branch.get("type") if isinstance(branch, dict) else None for branch in branches]
        declared = kinds[0] if all(kind == kinds[0] for kind in kinds) else None
    return declared if isinstance(declared, str) and declared in ("object", "array") else None


_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}
_SCALAR_TYPES = ("integer", "number", "boolean")


def _scalar_text(value: str, declared: str) -> Any:
    """The integer, number or boolean that text spells exactly, else the text unchanged.

    A boolean is only ``true`` or ``false``; ``1`` or ``yes`` stays text, as
    do ``5.0`` for an integer and a non-finite number.
    """
    if value != value.strip():
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if declared == "boolean":
        return parsed if isinstance(parsed, bool) else value
    if isinstance(parsed, bool) or not isinstance(parsed, _JSON_TYPES[declared]):
        return value
    return parsed if math.isfinite(parsed) else value


class Tool(ABC):
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

    #: Whether this tool's own errors already state what to do next. The
    #: registry appends a generic "try a different approach" hint to every
    #: error; on a tool whose refusal names the exact fix, that hint reads as
    #: advice to abandon a request that only needed one field corrected. A
    #: tool sets this to True only when it authors that remedy itself.
    self_describing_errors: bool = False
    #: True for a tool the user configured (an MCP server's tool). It has no
    #: catalog entry, yet ``tools_load`` lists it and ``tool_call`` reaches it.
    user_configured: bool = False
    #: Operator opt-in to advertise a configured tool's schema without discovery.
    always_advertised: bool = False

    _TYPE_MAP: dict[str, Any] = _JSON_TYPES

    @property
    def workspace(self) -> Path | None:
        """This tool's own configured resolution workspace, or None.

        The authoritative value for a filesystem tool: it is set once at
        construction (``self._workspace``) and never duplicated onto a
        registry or agent-level field a caller could read out of sync with
        the instance that actually executes.
        """
        return getattr(self, "_workspace", None)

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters.

        Returns:
            String result of the tool execution.
        """
        pass

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def decode_string_arguments(self, params: dict[str, Any]) -> dict[str, Any]:
        """Decode a top-level argument a model sent as text instead of its declared type.

        Some models serialize a nested argument as a string (#1653, #2453), and
        a local model calling a deferred tool by name, with no schema at its
        parser, sends every argument as text. Text that parses exactly to the
        declared object, array, integer, number or boolean is that argument;
        anything else is left for validation to refuse. This is the one place
        that decodes it, for every tool.
        """
        properties = (self.parameters or {}).get("properties", {})
        decoded = dict(params)
        for key, value in params.items():
            schema = properties.get(key, {})
            if not isinstance(value, str):
                continue
            declared = schema.get("type") if isinstance(schema, dict) else None
            if isinstance(declared, str) and declared in _SCALAR_TYPES:
                decoded[key] = _scalar_text(value, declared)
                continue
            expected = _container_type(schema)
            if expected is None:
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict if expected == "object" else list):
                decoded[key] = parsed
        return decoded

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        if "allOf" in schema:
            rules = schema["allOf"]
            base_schema = {key: value for key, value in schema.items() if key != "allOf"}
            errors = self._validate(val, base_schema, path)
            for rule in rules:
                condition = rule.get("if")
                if condition is not None and self._validate(val, condition, path):
                    continue
                consequence = rule.get("then")
                if consequence is not None:
                    errors.extend(self._validate(val, consequence, path))
            # The base schema and an operation's rule can refuse the same field.
            return list(dict.fromkeys(errors))

        # Exact operation contracts use the conditional allOf path above.

        t, label = schema.get("type") or _container_type(schema), path or "parameter"
        if isinstance(t, list):
            if "null" in t and val is None:
                return []
            non_null = [x for x in t if x != "null"]
            if non_null and not any(isinstance(val, self._TYPE_MAP.get(x, type(None))) for x in non_null):
                return [f"{label} should be one of {non_null}"]
            # Continue with the member the value matches, so a union such as
            # ["string", "array"] still checks an array's items.
            t = next(
                (x for x in non_null if isinstance(val, self._TYPE_MAP.get(x, type(None)))),
                None,
            )
        if t in self._TYPE_MAP and not isinstance(val, self._TYPE_MAP[t]):
            if t in ("object", "array") and isinstance(val, str):
                return [f"{label} should be {t}, not text: pass the {t} itself"]
            return [f"{label} should be {t}"]

        errors = []
        if "const" in schema and val != schema["const"]:
            errors.append(f"{label} must equal {schema['const']!r}")
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        if t == "object":
            if "minProperties" in schema and len(val) < schema["minProperties"]:
                errors.append(
                    f"{label} must have at least {schema['minProperties']} field(s)"
                )
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {path + '.' + k if path else k}")
            for k, v in val.items():
                key_path = path + "." + k if path else k
                if k in props:
                    errors.extend(self._validate(v, props[k], key_path))
                elif schema.get("additionalProperties") is False:
                    valid = ", ".join(sorted(props))
                    errors.append(
                        f"unexpected {key_path}"
                        + (f" (valid keys: {valid})" if valid else "")
                    )
        if t == "array" and "items" in schema:
            if "minItems" in schema and len(val) < schema["minItems"]:
                errors.append(f"{label} must have at least {schema['minItems']} item(s)")
            if "maxItems" in schema and len(val) > schema["maxItems"]:
                errors.append(f"{label} must have at most {schema['maxItems']} item(s)")
            for i, item in enumerate(val):
                errors.extend(self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]"))
        return errors

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }
