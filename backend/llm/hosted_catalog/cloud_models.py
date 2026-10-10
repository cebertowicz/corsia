"""Cloud-model reasoning shapes, sourced from the hosted-catalog snapshot.

Replaces the hardcoded per-family reasoning tables that used to be duplicated
across ``reasoning_config.py`` and ``anthropic_thinking.py`` (the "3 lists that
must stay in sync" smell — ``PROVIDER-STRATEGY-MANAGED-VS-EXTERNAL.md`` §8 #4).
The data now lives in ``snapshot/cloud-models/<provider>.json`` and is read
through the general :mod:`llm.hosted_catalog.loader`.

Matching is **first-prefix-wins** over the file's ordered ``families`` array, so
the JSON order encodes precedence (e.g. ``gpt-5-nano`` before ``gpt-5``).
"""

from __future__ import annotations

import copy
from typing import Any

from llm.hosted_catalog.loader import load_section

_SECTION = "cloud-models"


def _match(provider: str, model_id: str) -> dict[str, Any] | None:
    """Return the first family whose prefixes match ``model_id``, or None."""
    if not model_id:
        return None
    section = load_section(_SECTION)
    data = section.get(provider)
    if not data:
        return None
    for family in data.get("families", []):
        if any(model_id.startswith(p) for p in family.get("match_prefixes", [])):
            return family
    return None


def reasoning_shape_for(provider: str, model_id: str) -> dict[str, Any] | None:
    """Return the verbatim reasoning-capability descriptor for a cloud model.

    None means the model has no extended-reasoning shape (a normal, non-error
    outcome for a non-reasoning model). The returned dict is a deep copy (incl.
    the nested ``values`` list), so callers cannot mutate the cached snapshot —
    matching the old behaviour of returning a fresh ``list(...)``.
    """
    family = _match(provider, model_id)
    if family is None:
        return None
    return copy.deepcopy(family["config"])


def uses_adaptive_thinking(model_id: str) -> bool:
    """True if this Anthropic model routes through output_config.effort adaptive thinking."""
    family = _match("anthropic", model_id)
    return bool(family and family.get("uses_adaptive_thinking", False))


def effort_enum_values(model_id: str) -> frozenset[str] | None:
    """Effort enum for an Anthropic adaptive-thinking model, or None to use the default.

    None means "not an adaptive-thinking effort model" — the caller falls back to
    its default enum. A matching family returns its exact ``config.values`` set
    (e.g. the xhigh-bearing set for opus-4-7).
    """
    family = _match("anthropic", model_id)
    if not family or not family.get("uses_adaptive_thinking"):
        return None
    config = family.get("config", {})
    if config.get("type") != "effort_levels":
        return None
    return frozenset(config.get("values", []))
