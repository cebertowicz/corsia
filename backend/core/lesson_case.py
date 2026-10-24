"""Canonical schema and validation for persisted placement case metadata."""

from __future__ import annotations

from typing import Any

CORRECTION_CAUSES = (
    "wrong_rule", "no_foundation", "rule_gap", "process_gap", "not_delivered",
    "not_followed", "cant_be_done",
)
NEW_EVIDENCE = ("target_changed", "case_differs")
_ACTION_FIELDS = frozenset({"tool", "params"})
_ACTIONS_FIELDS = frozenset({"required_actions", "forbidden_actions", "decisive_action"})

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tool", "params"],
    "properties": {"tool": {"type": "string"}, "params": {"type": "object"}},
}

CASE_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["what_happened", "right_call", "does_not_apply", "correction_subject"],
    "properties": {
        "what_happened": {"type": "string"},
        "right_call": {"type": "string"},
        "does_not_apply": {"type": "string"},
        "correction_subject": {"type": "string"},
        "actions": {
            "type": "object",
            "additionalProperties": False,
            "required": ["required_actions", "forbidden_actions", "decisive_action"],
            "properties": {
                "required_actions": {"type": "array", "minItems": 1, "items": ACTION_SCHEMA},
                "forbidden_actions": {"type": "array", "items": ACTION_SCHEMA},
                "decisive_action": ACTION_SCHEMA,
            },
        },
    },
}


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _action(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ACTION_FIELDS:
        raise ValueError(f"case.actions.{field} actions must be {{tool, params}}")
    tool, params = value.get("tool"), value.get("params")
    if not isinstance(tool, str) or not tool.strip() or not isinstance(params, dict) or not _json_value(params):
        raise ValueError(f"case.actions.{field} actions need a tool and JSON-object params")
    return {"tool": tool.strip(), "params": params}


def validate_actions(value: object) -> dict[str, Any]:
    """Validate ordered actions whose params later match canonical calls by subset."""
    if not isinstance(value, dict) or set(value) != _ACTIONS_FIELDS:
        raise ValueError(
            "case.actions must name required_actions, forbidden_actions and decisive_action"
        )
    required_raw, forbidden_raw = value.get("required_actions"), value.get("forbidden_actions")
    if not isinstance(required_raw, list) or not required_raw or not isinstance(forbidden_raw, list):
        raise ValueError("case.actions requires non-empty required_actions and list forbidden_actions")
    required = [_action(item, field="required_actions") for item in required_raw]
    forbidden = [_action(item, field="forbidden_actions") for item in forbidden_raw]
    decisive = _action(value.get("decisive_action"), field="decisive_action")
    if decisive not in required:
        raise ValueError("case.actions.decisive_action must be one declared required action")
    return {
        "required_actions": required,
        "forbidden_actions": forbidden,
        "decisive_action": decisive,
    }


def validate_case(value: object) -> dict[str, Any] | None:
    """Validate optional persisted case metadata without introducing an evaluator."""
    if value is None:
        return None
    keys = ("what_happened", "right_call", "does_not_apply", "correction_subject")
    allowed = {*keys, "actions"}
    if not isinstance(value, dict) or set(value) - allowed or not set(keys) <= set(value):
        raise ValueError(
            "case must name what_happened, right_call, does_not_apply and correction_subject"
        )
    cleaned = {key: _clean(value.get(key)) for key in keys}
    if not all(cleaned.values()):
        raise ValueError(
            "case must name what_happened, right_call, does_not_apply and correction_subject"
        )
    case: dict[str, Any] = {key: text for key, text in cleaned.items() if text is not None}
    if "actions" in value:
        case["actions"] = validate_actions(value["actions"])
    return case
