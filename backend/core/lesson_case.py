"""Optional case metadata a placement answer carries and its lesson keeps.

The one definition of these fields: their allowed values, their answer schema
and their validation, shared by the placement answer and the ``corsia.lesson``
block. Nothing reads them yet beyond that round trip; this module adds no
evaluator, writer or lifecycle. The corrections a lesson came from stay the
system-derived ``originating_subjects``, never a field here.
"""

from __future__ import annotations

from typing import Any

#: The seven correction causes of the ``memory-placement`` skill.
CORRECTION_CAUSES = (
    "wrong_rule", "no_foundation", "rule_gap", "process_gap", "not_delivered",
    "not_followed", "cant_be_done",
)
NEW_EVIDENCE = ("target_changed", "case_differs")

_TEXT_FIELDS = ("target_file", "new_target_path", "target_section")
_CHOICES = {"cause": CORRECTION_CAUSES, "new_evidence": NEW_EVIDENCE}
_CASE_FIELDS = ("what_happened", "right_call", "does_not_apply")
_ACTION_FIELDS = ("required_actions", "forbidden_actions", "decisive_action")
FIELDS = (*_TEXT_FIELDS, "cause", "case", "new_evidence")

#: The placement guide's lines for ``FIELDS``.
GUIDE = f"""- With a trial, optionally describe its case; leave out what does not apply,
  and a revision keeps what it leaves out:
  - case: what_happened, right_call and does_not_apply, one line each. Add
    actions when the right call is a tool call: the ordered required_actions,
    any forbidden_actions, and the decisive_action, one of the required ones;
    each is a tool and the params the call must carry.
  - cause: why the correction happened, one of {", ".join(CORRECTION_CAUSES)}.
  - target_file and target_section: the existing file and section the rule
    amends; new_target_path instead when the rule needs a new file.
  - new_evidence, only when revising a trial: {" or ".join(NEW_EVIDENCE)}."""

_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tool", "params"],
    "properties": {"tool": {"type": "string"}, "params": {"type": "object"}},
}

#: The answer-schema properties for ``FIELDS``, merged into the placement answer.
SCHEMA_PROPERTIES: dict[str, Any] = {
    **{key: {"type": ["string", "null"]} for key in _TEXT_FIELDS},
    **{key: {"type": ["string", "null"], "enum": [*values, None]} for key, values in _CHOICES.items()},
    "case": {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": list(_CASE_FIELDS),
        "properties": {
            **{key: {"type": "string"} for key in _CASE_FIELDS},
            "actions": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_ACTION_FIELDS),
                "properties": {
                    "required_actions": {"type": "array", "minItems": 1, "items": _ACTION_SCHEMA},
                    "forbidden_actions": {"type": "array", "items": _ACTION_SCHEMA},
                    "decisive_action": _ACTION_SCHEMA,
                },
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
    if not isinstance(value, dict) or set(value) != {"tool", "params"}:
        raise ValueError(f"case.actions.{field} actions must be {{tool, params}}")
    tool, params = value.get("tool"), value.get("params")
    if not isinstance(tool, str) or not tool.strip() or not isinstance(params, dict) or not _json_value(params):
        raise ValueError(f"case.actions.{field} actions need a tool and JSON-object params")
    return {"tool": tool.strip(), "params": params}


def _actions(value: object) -> dict[str, Any]:
    """Ordered actions whose params later match canonical calls by subset."""
    if not isinstance(value, dict) or set(value) != set(_ACTION_FIELDS):
        raise ValueError("case.actions must name required_actions, forbidden_actions and decisive_action")
    required_raw, forbidden_raw = value.get("required_actions"), value.get("forbidden_actions")
    if not isinstance(required_raw, list) or not required_raw or not isinstance(forbidden_raw, list):
        raise ValueError("case.actions requires non-empty required_actions and list forbidden_actions")
    required = [_action(item, field="required_actions") for item in required_raw]
    decisive = _action(value.get("decisive_action"), field="decisive_action")
    if decisive not in required:
        raise ValueError("case.actions.decisive_action must be one declared required action")
    return {
        "required_actions": required,
        "forbidden_actions": [_action(item, field="forbidden_actions") for item in forbidden_raw],
        "decisive_action": decisive,
    }


def _case(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {*_CASE_FIELDS, "actions"}:
        raise ValueError("case may name only what_happened, right_call, does_not_apply and actions")
    case: dict[str, Any] = {key: _clean(value.get(key)) for key in _CASE_FIELDS}
    if not all(case.values()):
        raise ValueError("case must name what_happened, right_call and does_not_apply")
    if "actions" in value:
        case["actions"] = _actions(value["actions"])
    return case


def validate_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """The supplied ``FIELDS`` of ``raw``, checked; ``ValueError`` says what to correct.

    A field that is absent or null is left out, so a revision that omits it
    keeps the value its lesson already holds.
    """
    metadata: dict[str, Any] = {}
    for key in FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if key == "case":
            metadata[key] = _case(value)
        elif key in _CHOICES:
            if value not in _CHOICES[key]:
                raise ValueError(f"{key} must be one of {', '.join(_CHOICES[key])}")
            metadata[key] = value
        elif not isinstance(value, str):
            raise ValueError(f"{key} must be a string or null")
        elif text := _clean(value):
            metadata[key] = text
    return metadata
