"""Check decisive actions against an existing tool registry."""

from __future__ import annotations

from typing import Any

from loguru import logger


UNPARAMETERIZED = "decisive_unparameterized"


def unparameterized(actions: object, registry: Any) -> bool:
    """True only when an empty decisive action has known required parameters."""
    if not isinstance(actions, dict) or registry is None:
        return False
    action = actions.get("decisive_action")
    if not isinstance(action, dict) or action.get("params") != {} or not isinstance(action.get("tool"), str):
        return False
    try:
        tool = registry.get(action["tool"])
        return bool(tool and not tool.user_configured and tool.parameters.get("required"))
    except Exception:
        logger.opt(exception=True).warning("carry-in tool schema lookup failed; treating it as unknown")
        return False  # an unresolvable schema is allowed


def validate_placement(placement: object, registry: Any) -> None:
    """Refuse an empty decisive action on a known tool with required params."""
    case = placement.get("case") if isinstance(placement, dict) else None
    actions = case.get("actions") if isinstance(case, dict) else None
    if unparameterized(actions, registry):
        raise ValueError(
            'The decisive action must name the param that separates the right call from the miss. '
            'Use an exact value, or {"$contains": "text"} to match part of a string such as a path.'
        )
