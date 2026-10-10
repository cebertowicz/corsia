"""Characterization gate for hosted-catalog connector capabilities.

Pins connector capability dicts consumed by model selection. Exact context
windows are discovered per model into the canonical catalog and deliberately
do not live in connector-family descriptors.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa: E402

import pytest  # noqa: E402

from llm.hosted_catalog.connector_models import capabilities_for  # noqa: E402
from llm.hosted_catalog.loader import _reset_cache_for_tests  # noqa: E402
from llm.model_status import _required_harness_capabilities  # noqa: E402

# --- Expected hosted-catalog capability descriptors --------------------------

_CC_CAPS = {
    "supports_vision": True,
    "supports_tools": True,
    "supports_reasoning": True,
    "reasoning_config": {
        "type": "effort_levels",
        "values": ["low", "medium", "high", "xhigh", "max"],
        "default": "high",
    },
    "default_temperature": None,
    "default_max_tokens": None,
}

_CODEX_CAPS = {
    "supports_vision": True,
    "supports_tools": True,
    "supports_reasoning": True,
    "reasoning_config": {
        "type": "effort_levels",
        "values": ["none", "minimal", "low", "medium", "high", "xhigh"],
        "default": "high",
    },
    "default_temperature": None,
    "default_max_tokens": None,
}


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


def test_cc_capabilities_byte_identical():
    """Claude Code capabilities must match the approved catalog descriptor."""
    assert capabilities_for("claude-code") == _CC_CAPS


def test_codex_capabilities_byte_identical():
    """capabilities_for('codex') must match the pre-extraction hardcoded dict exactly."""
    assert capabilities_for("codex") == _CODEX_CAPS


def test_capabilities_returns_deep_copy():
    """Mutating the returned dict must not affect subsequent calls."""
    caps = capabilities_for("claude-code")
    caps["reasoning_config"]["values"].clear()
    caps2 = capabilities_for("claude-code")
    assert caps2["reasoning_config"]["values"] == _CC_CAPS["reasoning_config"]["values"]


def test_unknown_connector_returns_none():
    assert capabilities_for("does-not-exist") is None


def test_no_capabilities_block_returns_none():
    """Connectors without a capabilities block (e.g. omlx) return None."""
    result = capabilities_for("omlx")
    assert result is None


@pytest.mark.parametrize("connector_id,expected", [
    ("claude-code", _CC_CAPS),
    ("codex", _CODEX_CAPS),
])
def test_connector_capability_matrix(connector_id, expected):
    assert capabilities_for(connector_id) == expected


def test_required_capabilities_raises_loud_on_unknown():
    """Required harness capabilities fail loud when the snapshot drifts."""
    with pytest.raises(RuntimeError, match="missing capabilities for 'does-not-exist'"):
        _required_harness_capabilities("does-not-exist")


def test_required_capabilities_passes_for_cc():
    assert _required_harness_capabilities("claude-code") == _CC_CAPS


def test_required_capabilities_passes_for_codex():
    assert _required_harness_capabilities("codex") == _CODEX_CAPS
