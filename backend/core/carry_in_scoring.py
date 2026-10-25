"""Score one C1 sample from its canonical tool-call records; no model judge.

A case's actions are ``lesson_case``'s validated shape: ordered
``required_actions``, ``forbidden_actions`` and the ``decisive_action`` (one of
the required ones). An action matches a call when the tool names agree and the
action's params are a subset of the call's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def _params(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def canonical_calls(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``{tool, params}`` per recorded call, unwrapping the generic ``tool_call`` dispatcher."""
    normalized = []
    for call in calls:
        name, params = call.get("name"), _params(call.get("arguments"))
        if not isinstance(name, str) or params is None:
            continue
        if name == "tool_call":
            name, params = params.get("name"), _params(params.get("arguments"))
            if not isinstance(name, str) or params is None:
                continue
        normalized.append({"tool": name, "params": params})
    return normalized


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(value, actual[key]) for key, value in expected.items()
        )
    return expected == actual


def matches(call: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    return call["tool"] == action["tool"] and _subset(action["params"], call["params"])


def sample_passes(actions: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> bool:
    """True when the decisive action comes after every earlier required one, in order.

    A forbidden call loses at once. The decisive call passes only if each
    required action listed before it already matched, in that order.
    """
    required = list(actions["required_actions"])
    decisive = actions["decisive_action"]
    before = required[:required.index(decisive)]
    matched = 0
    for call in canonical_calls(calls):
        if any(matches(call, action) for action in actions["forbidden_actions"]):
            return False
        if matched == len(before) and matches(call, decisive):
            return True
        if matched < len(before) and matches(call, before[matched]):
            matched += 1
    return False
