from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patched_anthropic_sdk() -> tuple[object, AsyncMock]:
    """Patch the Anthropic client so the exact create kwargs are observable."""
    create = AsyncMock(return_value=SimpleNamespace(
        model="claude-sonnet-4-6",
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    ))
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = create
    client.close = AsyncMock(return_value=None)
    return patch(
        "llm.providers.anthropic._get_client", return_value=client
    ), create


@pytest.mark.asyncio
async def test_anthropic_ordinary_chat_uses_model_capability_not_a_lower_literal():
    """The mandatory Anthropic ceiling follows the model, never a 4096 literal.

    The Messages API requires ``max_tokens``, so this adapter cannot omit it.
    It must still not silently truncate ordinary chat below the ceiling that
    applied before the global override was removed.
    """
    from llm.providers.anthropic import anthropic_completion

    patcher, create = _patched_anthropic_sdk()
    with patcher, patch(
        "llm.capabilities.get_model_max_output_tokens", return_value=16_000
    ):
        await anthropic_completion(
            [{"role": "user", "content": "ordinary anthropic turn"}],
            tools=None,
            model="anthropic/claude-sonnet-4-6",
        )

    assert create.call_args.kwargs["max_tokens"] == 16_000


@pytest.mark.asyncio
async def test_anthropic_still_honours_an_explicit_caller_budget():
    """An explicit per-operation budget stays exactly what the caller asked for."""
    from llm.providers.anthropic import anthropic_completion

    patcher, create = _patched_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            [{"role": "user", "content": "bounded checkpoint"}],
            tools=None,
            model="anthropic/claude-sonnet-4-6",
            max_tokens=8_192,
        )

    assert create.call_args.kwargs["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_native_ordinary_chat_omits_max_tokens(monkeypatch):
    """Native turns leave the provider output budget caller-owned."""
    from core import turn_provider_call

    captured: dict[str, object] = {}

    async def completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content="done",
            finish_reason="stop",
            has_tool_calls=False,
            usage={},
            ttft_ms=None,
        )

    async def on_step(_kind, _payload):
        return None

    monkeypatch.setattr(turn_provider_call, "chat_completion", completion)

    response, streamed_tools = await turn_provider_call.call_llm(
        SimpleNamespace(max_tokens=8192),
        [{"role": "user", "content": "hello"}],
        [],
        "hosted_vllm/example",
        {},
        "session-output-budget",
        asyncio.Event(),
        on_step,
        1,
    )

    assert response.content == "done"
    assert streamed_tools is False
    assert "max_tokens" not in captured
