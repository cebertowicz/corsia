"""A workflow's declared result (``corsia.workflow.result``) and the one check of a worker's
final message against it.

The declared schema is a small typed subset, enough for a findings list without a
schema library:

- every node has ``type``: ``object``, ``array``, ``string``, ``integer``, ``number`` or
  ``boolean``, and may have ``description`` and ``default`` (used when an object's
  optional property is missing; a missing required one is invalid, default or not);
- ``object`` has ``properties`` (name to node), ``required`` (names among them),
  and may set ``additionalProperties: false``;
- ``array`` has ``items`` (one node) and may declare ``maxItems``;
- ``string`` may declare ``maxLength``, ``enum`` and ``unknown``, an enum value a string outside the enum
  becomes instead of failing;
- ``integer`` and ``number`` may have ``coerce: true``, which reads a numeric string.

:func:`schema_errors` checks a declaration, for the skill validator; :func:`check_result`
checks a final message: its payload is the whole message as JSON, else its last fenced
block that parses (never a bracket found in prose, which may be an example); it applies
the declared defaults and coercions, and only what fails after that is invalid.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TYPES = {"object", "array", "string", "integer", "number", "boolean"}
_KEYS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "maxItems"},
    "string": {"enum", "unknown", "maxLength"},
    "integer": {"coerce"},
    "number": {"coerce"},
    "boolean": set(),
}
_COMMON = {"type", "description", "default"}
_MAX_DEPTH = 8
#: A final message larger than this is not read for a payload.
MAX_MESSAGE = 256 * 1024
_FENCE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\n(.*?)```", re.DOTALL)


class ResultInvalid(ValueError):
    """A final message with no payload, or one that fails its schema; the text says where."""


def schema_errors(schema: Any, path: str = "corsia.workflow.result", depth: int = 0) -> list[str]:
    """What is wrong with a declared result schema; empty when it is usable."""
    if depth > _MAX_DEPTH:
        return [f"{path} nests deeper than {_MAX_DEPTH} levels"]
    if not isinstance(schema, dict):
        return [f"{path} must be a mapping"]
    kind = schema.get("type")
    if kind not in _TYPES:
        return [f"{path}.type must be one of {', '.join(sorted(_TYPES))}"]
    errors = [f"{path}.{key} is an unknown key for type {kind}"
              for key in schema if key not in _COMMON | _KEYS[kind]]
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
            return [*errors, f"{path}.properties must map names to schemas"]
        for name, node in properties.items():
            errors += schema_errors(node, f"{path}.properties.{name}", depth + 1)
        if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            errors.append(f"{path}.additionalProperties must be true or false")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(name not in properties for name in required):
            errors.append(f"{path}.required must list names from properties")
    elif kind == "array":
        if "items" not in schema:
            errors.append(f"{path}.items is required for an array")
        else:
            errors += schema_errors(schema["items"], f"{path}.items", depth + 1)
    elif kind == "string":
        enum = schema.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum
                                 or not all(isinstance(v, str) for v in enum)):
            errors.append(f"{path}.enum must be a list of strings")
        elif "unknown" in schema and schema["unknown"] not in (enum or []):
            errors.append(f"{path}.unknown must be one of its enum")
    elif "coerce" in schema and not isinstance(schema["coerce"], bool):
        errors.append(f"{path}.coerce must be true or false")
    limit_key = {"array": "maxItems", "string": "maxLength"}.get(kind)
    if limit_key and limit_key in schema:
        limit = schema[limit_key]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            errors.append(f"{path}.{limit_key} must be a non-negative integer")
    if not errors and "default" in schema:
        try:
            conform(schema["default"], schema, f"{path}.default")
        except ResultInvalid as exc:
            errors.append(str(exc))
    return errors


def conform(value: Any, schema: dict[str, Any], path: str = "result") -> Any:
    """``value`` with the schema's defaults and coercions applied, or :class:`ResultInvalid`."""
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ResultInvalid(f"{path} must be an object")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ResultInvalid(f"{path} has unexpected properties")
        out = dict(value)
        for name, node in properties.items():
            if name in out:
                out[name] = conform(out[name], node, f"{path}.{name}")
            elif name in schema.get("required", []):
                raise ResultInvalid(f"{path}.{name} is required")
            elif "default" in node:
                out[name] = node["default"]
        return out
    if kind == "array":
        if not isinstance(value, list):
            raise ResultInvalid(f"{path} must be an array")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ResultInvalid(f"{path} must have at most {schema['maxItems']} items")
        return [conform(item, schema["items"], f"{path}[{index}]") for index, item in enumerate(value)]
    if kind == "string":
        if not isinstance(value, str):
            raise ResultInvalid(f"{path} must be a string")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ResultInvalid(f"{path} must have at most {schema['maxLength']} characters")
        enum = schema.get("enum")
        if enum and value not in enum:
            if "unknown" not in schema:
                raise ResultInvalid(f"{path} must be one of {', '.join(enum)}")
            return schema["unknown"]
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ResultInvalid(f"{path} must be true or false")
        return value
    return _number(value, schema, path)


def _number(value: Any, schema: dict[str, Any], path: str) -> int | float:
    wanted = "an integer" if schema["type"] == "integer" else "a number"
    if isinstance(value, str) and schema.get("coerce"):
        try:
            value = float(value.strip()) if schema["type"] == "number" else int(value.strip())
        except ValueError:
            raise ResultInvalid(f"{path} must be {wanted}") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultInvalid(f"{path} must be {wanted}")
    if schema["type"] == "integer" and not float(value).is_integer():
        raise ResultInvalid(f"{path} must be {wanted}")
    return int(value) if schema["type"] == "integer" else value


def extract_payload(text: str) -> Any:
    """The JSON object or array a final message carries: the whole message, else its last
    fenced block that parses. Brackets in prose are never read: they may be an example."""
    if len(text) > MAX_MESSAGE:
        raise ResultInvalid(f"the final message is over {MAX_MESSAGE} bytes")
    blocks = [block.strip() for block in _FENCE.findall(text)]
    for candidate in (text.strip(), *reversed(blocks)):
        try:
            found = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(found, (dict, list)):
            return found
    raise ResultInvalid("the final message is not JSON and has no fenced JSON block")


def check_result(text: str | None, schema: dict[str, Any]) -> Any:
    """The worker's payload, conformed to its workflow's declared result, or :class:`ResultInvalid`."""
    if not text or not text.strip():
        raise ResultInvalid("the worker returned no final message")
    return conform(extract_payload(text), schema)
