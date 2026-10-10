"""Anthropic adaptive-thinking decision logic."""

from __future__ import annotations

from typing import Any


def strip_anthropic_prefix(model: str) -> str:
    """Strip the ``anthropic/`` provider prefix."""
    return model.removeprefix("anthropic/") if model else ""


def thinking_active(reasoning_effort: str | None) -> bool:
    """Return True if extended thinking is active for this turn."""
    if not reasoning_effort:
        return False
    return reasoning_effort.strip().lower() not in ("auto", "none", "")


# Which models use adaptive thinking, and their effort enums, come from the
# hosted-catalog snapshot (cloud_models) — formerly hardcoded family tables that
# duplicated reasoning_config.py. This default enum applies to any model without
# its own catalog effort set (non-adaptive / budget models).
_EFFORT_ENUM_DEFAULT = frozenset({"low", "medium", "high", "max"})


def model_uses_adaptive_thinking(model: str) -> bool:
    """True if this model uses output_config.effort adaptive thinking."""
    from llm.hosted_catalog.cloud_models import uses_adaptive_thinking
    return uses_adaptive_thinking(strip_anthropic_prefix(model))


def effort_enum_for(model: str) -> frozenset[str]:
    """Valid effort values for a given model."""
    from llm.hosted_catalog.cloud_models import effort_enum_values
    enum = effort_enum_values(strip_anthropic_prefix(model))
    return enum if enum is not None else _EFFORT_ENUM_DEFAULT


def effort_level_from(reasoning_effort: str, model: str) -> str | None:
    """Map ``reasoning_effort`` to a model-valid effort enum."""
    valid = effort_enum_for(model)
    s = reasoning_effort.strip().lower()
    if s in valid:
        return s
    try:
        budget = int(s)
    except ValueError:
        return None
    if budget <= 4000:
        return "low"
    if budget <= 16000:
        return "medium"
    if budget <= 32000:
        return "high"
    return "xhigh" if "xhigh" in valid else "high"


def adaptive_thinking_kwargs(
    model: str, reasoning_effort: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build ``(thinking, output_config)`` kwargs, or ``(None, None)``."""
    if not model_uses_adaptive_thinking(model):
        return None, None
    if not thinking_active(reasoning_effort):
        return None, None
    effort = effort_level_from(reasoning_effort, model)
    if effort is None:
        return None, None
    return {"type": "adaptive"}, {"effort": effort}


def legacy_thinking_budget(reasoning_effort: str | None) -> int | None:
    """Translate non-adaptive ``reasoning_effort`` into ``budget_tokens``."""
    if not reasoning_effort:
        return None
    s = reasoning_effort.strip().lower()
    bucket = {
        "low": 1024,
        "medium": 8000,
        "high": 16000,
        "max": 32000,
        "xhigh": 32000,
    }.get(s)
    if bucket is not None:
        return bucket
    try:
        n = int(s)
    except ValueError:
        return None
    return max(1024, n)
