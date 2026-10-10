
from __future__ import annotations

import os
import sys

import litellm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.base import filter_kwargs, _ACOMPLETION_KWARGS  # noqa: E402
from llm.providers.openai import _ARESPONSES_KWARGS  # noqa: E402


def test_litellm_drop_params_is_off():
    import llm  # noqa: F401  (side-effect import: sets the flag)
    assert litellm.drop_params is False


def test_filter_keeps_allowed_and_drops_others(caplog):
    allowed = frozenset({"a", "b"})
    out = filter_kwargs({"a": 1, "b": 2, "c": 3, "d": 4}, allowed, where="test")
    assert out == {"a": 1, "b": 2}


def test_filter_logs_dropped_keys(caplog):
    import loguru
    messages: list[str] = []
    sink_id = loguru.logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        filter_kwargs({"a": 1, "bogus": 2}, frozenset({"a"}), where="unit test")
    finally:
        loguru.logger.remove(sink_id)
    assert any("bogus" in m and "unit test" in m for m in messages)


def test_filter_noop_when_all_allowed():
    allowed = frozenset({"a", "b"})
    assert filter_kwargs({"a": 1}, allowed, where="t") == {"a": 1}


def test_acompletion_kwargs_includes_core_transport():
    assert "temperature" not in _ACOMPLETION_KWARGS
    required = {"model", "messages", "max_tokens",
                "tools", "tool_choice", "api_key", "api_base",
                "reasoning_effort", "extra_body", "stream"}
    assert required <= _ACOMPLETION_KWARGS


def test_aresponses_kwargs_differs_from_acompletion():
    assert "input" in _ARESPONSES_KWARGS
    assert "max_output_tokens" in _ARESPONSES_KWARGS
    assert "messages" not in _ARESPONSES_KWARGS
    assert "max_tokens" not in _ARESPONSES_KWARGS
