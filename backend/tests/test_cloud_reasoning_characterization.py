"""Characterization gate for the hosted-catalog cloud-reasoning extraction.

This pins the EXACT reasoning shapes / adaptive flags / effort enums / temperature
resolution that the hardcoded family tables produced before they moved into the
hosted-catalog snapshot. The expected values were captured from the pre-change
code (origin/dev) and must not move: cloud reasoning behaviour is the gate.

Companion to ``test_reasoning_config.py`` / ``test_anthropic_thinking.py`` (which
test the same surface piecemeal); this is the single full-matrix snapshot.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

import pytest  # noqa: E402

from llm.reasoning_config import reasoning_config_for  # noqa: E402
from llm.providers.anthropic_thinking import (  # noqa: E402
    model_uses_adaptive_thinking,
    effort_enum_for,
)

# --- Ground truth captured from the pre-extraction hardcoded tables ----------

_EFFORT = "effort_levels"
_BUDGET = {"type": "token_budget", "min": 1024, "max": 32000, "default": 16000}

OPENAI_REASONING = {
    "o1": {"type": _EFFORT, "values": ["low", "medium", "high"], "default": "medium", "rejects_temperature": True},
    "o1-mini": {"type": _EFFORT, "values": ["low", "medium", "high"], "default": "medium", "rejects_temperature": True},
    "o3": {"type": _EFFORT, "values": ["low", "medium", "high"], "default": "medium", "rejects_temperature": True},
    "o3-mini": {"type": _EFFORT, "values": ["low", "medium", "high"], "default": "medium", "rejects_temperature": True},
    "gpt-5": {"type": _EFFORT, "values": ["minimal", "low", "medium", "high"], "default": "medium"},
    "gpt-5-mini": {"type": _EFFORT, "values": ["minimal", "low", "medium", "high"], "default": "medium"},
    "gpt-5-nano": {"type": _EFFORT, "values": ["minimal", "low", "medium", "high"], "default": "medium", "rejects_temperature": True},
    "gpt-5.4": {"type": _EFFORT, "values": ["minimal", "low", "medium", "high", "xhigh"], "default": "medium"},
    "gpt-5.5": {"type": _EFFORT, "values": ["minimal", "low", "medium", "high", "xhigh"], "default": "medium", "rejects_temperature": True},
    "gpt-4o": None,
    "gpt-3.5-turbo": None,
    "": None,
}

_OPUS_47 = {"type": _EFFORT, "values": ["low", "medium", "high", "xhigh", "max"], "default": "high", "rejects_temperature": True}
_EFFORT_46 = {"type": _EFFORT, "values": ["low", "medium", "high", "max"], "default": "high"}

ANTHROPIC_REASONING = {
    "claude-opus-4-7": _OPUS_47,
    "claude-opus-4.7": _OPUS_47,
    "claude-opus-4-7-20260205": _OPUS_47,
    "claude-opus-4-6": _EFFORT_46,
    "claude-opus-4.6": _EFFORT_46,
    "claude-sonnet-4-6": _EFFORT_46,
    "claude-sonnet-4.6": _EFFORT_46,
    "claude-sonnet-4-7": _BUDGET,
    "claude-sonnet-4.7": _BUDGET,
    "claude-haiku-4-5": _BUDGET,
    "claude-haiku-4.5": _BUDGET,
    "claude-haiku-4-6": _BUDGET,
    "claude-haiku-4.6": _BUDGET,
    "claude-3-5-sonnet-20241022": None,
    "": None,
}

# (uses_adaptive_thinking, sorted effort enum) per anthropic model
_ENUM_XHIGH = sorted(["low", "medium", "high", "xhigh", "max"])
_ENUM_DEFAULT = sorted(["low", "medium", "high", "max"])
ANTHROPIC_THINKING = {
    "claude-opus-4-7": (True, _ENUM_XHIGH),
    "claude-opus-4.7": (True, _ENUM_XHIGH),
    "claude-opus-4-7-20260205": (True, _ENUM_XHIGH),
    "claude-opus-4-6": (True, _ENUM_DEFAULT),
    "claude-opus-4.6": (True, _ENUM_DEFAULT),
    "claude-sonnet-4-6": (True, _ENUM_DEFAULT),
    "claude-sonnet-4.6": (True, _ENUM_DEFAULT),
    "claude-sonnet-4-7": (False, _ENUM_DEFAULT),
    "claude-haiku-4-5": (False, _ENUM_DEFAULT),
    "claude-haiku-4-6": (False, _ENUM_DEFAULT),
    "claude-3-5-sonnet-20241022": (False, _ENUM_DEFAULT),
    "": (False, _ENUM_DEFAULT),
}


@pytest.mark.parametrize("model,expected", list(OPENAI_REASONING.items()))
def test_openai_reasoning_shape_unchanged(model, expected):
    assert reasoning_config_for("openai", model) == expected


@pytest.mark.parametrize("model,expected", list(ANTHROPIC_REASONING.items()))
def test_anthropic_reasoning_shape_unchanged(model, expected):
    assert reasoning_config_for("anthropic", model) == expected


@pytest.mark.parametrize("model,expected", list(ANTHROPIC_THINKING.items()))
def test_anthropic_adaptive_and_enum_unchanged(model, expected):
    api_model = f"anthropic/{model}" if model else ""
    adaptive, enum = expected
    assert model_uses_adaptive_thinking(api_model) is adaptive
    assert sorted(effort_enum_for(api_model)) == enum


def test_db_override_layer_still_wins(monkeypatch):
    """The live DB override (HMC3) takes precedence over the snapshot — unchanged."""
    import llm.reasoning_config as rc
    override = {"type": "effort_levels", "values": ["custom"], "default": "custom"}
    monkeypatch.setattr(rc, "_catalog_reasoning_shape", lambda *a, **k: override)
    assert rc.reasoning_config_for("anthropic", "claude-opus-4-7") == override
    assert rc.reasoning_config_for("openai", "gpt-4o") == override


def test_returned_shape_does_not_alias_the_cached_snapshot():
    """Mutating a returned shape must not corrupt the next caller's result."""
    first = reasoning_config_for("openai", "gpt-5.5")
    assert first is not None
    first["values"].append("CORRUPT")
    first["default"] = "CORRUPT"
    second = reasoning_config_for("openai", "gpt-5.5")
    assert "CORRUPT" not in second["values"]
    assert second["default"] == "medium"


def test_local_reasoning_is_not_resolved_by_the_cloud_path():
    """Local models never pick up a cloud shape (the boundary this gate pins).

    Local reasoning was itself hardcoded by a model-name matcher when this gate
    was written; it now comes from what the oMLX server reported at discovery
    and is stored on the catalog row, so a bare model id resolves to None here.
    The boundary being characterized is unchanged: the cloud tables must never
    answer for a local model.
    """
    assert reasoning_config_for("omlx", "qwen3-synthetic-8b") is None
    assert reasoning_config_for("llama", "local-model-8b") is None
