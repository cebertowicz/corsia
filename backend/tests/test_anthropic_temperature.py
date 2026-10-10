
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.providers.anthropic import (  # noqa: E402
    _thinking_active,
    anthropic_completion,
)




class TestThinkingActive:
    def test_low(self):
        assert _thinking_active("low") is True

    def test_medium(self):
        assert _thinking_active("medium") is True

    def test_high(self):
        assert _thinking_active("high") is True

    def test_auto_is_off(self):
        assert _thinking_active("auto") is False

    def test_none_is_off(self):
        assert _thinking_active(None) is False

    def test_empty_is_off(self):
        assert _thinking_active("") is False


def _fake_anthropic_message() -> object:
    text_block = SimpleNamespace(type="text", text="hi")
    usage = SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        usage=usage,
    )


def _patch_anthropic_sdk() -> tuple[Any, AsyncMock]:
    create_mock = AsyncMock(return_value=_fake_anthropic_message())
    client_instance = MagicMock()
    client_instance.messages = MagicMock()
    client_instance.messages.create = create_mock
    client_instance.close = AsyncMock(return_value=None)
    patcher = patch(
        "llm.providers.anthropic._get_client",
        return_value=client_instance,
    )
    return patcher, create_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "effort"), [
    ("anthropic/claude-opus-4-7", None),
    ("anthropic/claude-sonnet-4-6", "high"),
    ("anthropic/claude-sonnet-4-6", None),
])
async def test_anthropic_sends_no_temperature(model, effort):
    """Sampling is the provider's setting: no temperature on any request."""
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            [{"role": "user", "content": "hi"}], tools=None,
            model=model, max_tokens=100, reasoning_effort=effort,
        )
    kwargs = create_mock.call_args.kwargs
    assert "temperature" not in kwargs
    assert kwargs["max_tokens"] == 100
