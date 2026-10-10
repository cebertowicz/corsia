
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.providers.anthropic import (  # noqa: E402
    _adaptive_thinking_kwargs,
    _effort_enum_for,
    _effort_level_from,
    _model_uses_adaptive_thinking,
    _parse_response,
    _streaming_completion,
    _thinking_active,
    anthropic_completion,
)




class TestModelUsesAdaptiveThinking:
    def test_opus_47(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-opus-4-7") is True

    def test_opus_47_dot_variant(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-opus-4.7") is True

    def test_opus_47_dated_snapshot(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-opus-4-7-20260205") is True

    def test_opus_46_now_uses_adaptive(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-opus-4-6") is True

    def test_sonnet_46_now_uses_adaptive(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-sonnet-4-6") is True

    def test_sonnet_47_does_not(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-sonnet-4-7") is False

    def test_haiku_does_not(self):
        assert _model_uses_adaptive_thinking("anthropic/claude-haiku-4-5") is False
        assert _model_uses_adaptive_thinking("anthropic/claude-haiku-4-6") is False

    def test_empty_does_not(self):
        assert _model_uses_adaptive_thinking("") is False


class TestEffortEnumFor:
    def test_opus_47_includes_xhigh_and_max(self):
        enum = _effort_enum_for("anthropic/claude-opus-4-7")
        assert "xhigh" in enum
        assert "max" in enum

    def test_opus_46_excludes_xhigh_includes_max(self):
        enum = _effort_enum_for("anthropic/claude-opus-4-6")
        assert "xhigh" not in enum
        assert "max" in enum

    def test_sonnet_46_excludes_xhigh_includes_max(self):
        enum = _effort_enum_for("anthropic/claude-sonnet-4-6")
        assert "xhigh" not in enum
        assert "max" in enum


class TestThinkingActive:
    def test_low_medium_high_max_active(self):
        for v in ("low", "medium", "high", "xhigh", "max"):
            assert _thinking_active(v) is True, v

    def test_auto_inactive(self):
        assert _thinking_active("auto") is False

    def test_none_string_inactive(self):
        assert _thinking_active("none") is False

    def test_null_inactive(self):
        assert _thinking_active(None) is False
        assert _thinking_active("") is False


class TestEffortLevelFrom:
    def test_low_string_opus_47(self):
        assert _effort_level_from("low", "anthropic/claude-opus-4-7") == "low"

    def test_max_string_opus_46(self):
        assert _effort_level_from("max", "anthropic/claude-opus-4-6") == "max"

    def test_xhigh_invalid_for_opus_46(self):
        assert _effort_level_from("xhigh", "anthropic/claude-opus-4-6") is None

    def test_xhigh_string_opus_47(self):
        assert _effort_level_from("xhigh", "anthropic/claude-opus-4-7") == "xhigh"

    def test_uppercase_normalised(self):
        assert _effort_level_from("MAX", "anthropic/claude-sonnet-4-6") == "max"

    def test_small_budget_buckets_to_low(self):
        assert _effort_level_from("2000", "anthropic/claude-opus-4-7") == "low"
        assert _effort_level_from("2000", "anthropic/claude-opus-4-6") == "low"

    def test_mid_budget_buckets_to_medium(self):
        assert _effort_level_from("10000", "anthropic/claude-opus-4-6") == "medium"

    def test_large_budget_buckets_to_high(self):
        assert _effort_level_from("24000", "anthropic/claude-opus-4-6") == "high"

    def test_huge_budget_for_opus_47_is_xhigh(self):
        assert _effort_level_from("64000", "anthropic/claude-opus-4-7") == "xhigh"

    def test_huge_budget_for_opus_46_caps_at_high(self):
        assert _effort_level_from("64000", "anthropic/claude-opus-4-6") == "high"
        assert _effort_level_from("100000", "anthropic/claude-sonnet-4-6") == "high"

    def test_unparseable_returns_none(self):
        assert _effort_level_from("auto", "anthropic/claude-opus-4-7") is None
        assert _effort_level_from("none", "anthropic/claude-opus-4-7") is None


class TestAdaptiveThinkingKwargs:
    def test_opus_47_high_returns_both(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-opus-4-7", "high")
        assert thinking == {"type": "adaptive"}
        assert oc == {"effort": "high"}

    def test_opus_47_max(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-opus-4-7", "max")
        assert oc == {"effort": "max"}

    def test_opus_46_max_unlock(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-opus-4-6", "max")
        assert thinking == {"type": "adaptive"}
        assert oc == {"effort": "max"}

    def test_sonnet_46_max_unlock(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-sonnet-4-6", "max")
        assert thinking == {"type": "adaptive"}
        assert oc == {"effort": "max"}

    def test_opus_46_xhigh_falls_back_to_none(self):
        assert _adaptive_thinking_kwargs("anthropic/claude-opus-4-6", "xhigh") == (None, None)

    def test_opus_47_with_legacy_numeric_budget_buckets(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-opus-4-7", "16000")
        assert oc == {"effort": "medium"}

    def test_opus_46_with_legacy_numeric_budget_buckets(self):
        thinking, oc = _adaptive_thinking_kwargs("anthropic/claude-opus-4-6", "16000")
        assert oc == {"effort": "medium"}

    def test_auto_returns_nones(self):
        for model in (
            "anthropic/claude-opus-4-7",
            "anthropic/claude-opus-4-6",
            "anthropic/claude-sonnet-4-6",
        ):
            assert _adaptive_thinking_kwargs(model, "auto") == (None, None), model

    def test_none_string_returns_nones(self):
        for model in (
            "anthropic/claude-opus-4-7",
            "anthropic/claude-opus-4-6",
            "anthropic/claude-sonnet-4-6",
        ):
            assert _adaptive_thinking_kwargs(model, "none") == (None, None), model

    def test_null_returns_nones(self):
        assert _adaptive_thinking_kwargs("anthropic/claude-opus-4-7", None) == (None, None)

    def test_non_adaptive_models_return_nones(self):
        assert _adaptive_thinking_kwargs("anthropic/claude-sonnet-4-7", "high") == (None, None)
        assert _adaptive_thinking_kwargs("anthropic/claude-haiku-4-5", "high") == (None, None)




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
async def test_opus_47_max_sends_top_level_kwargs():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-7",
            max_tokens=100,
            reasoning_effort="max",
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_opus_46_max_sends_top_level_kwargs():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-6",
            max_tokens=100,
            reasoning_effort="max",
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_sonnet_46_high_uses_adaptive_path_now():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-sonnet-4-6",
            max_tokens=100,
            reasoning_effort="high",
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_opus_47_xhigh_passes_through():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-7",
            max_tokens=100,
            reasoning_effort="xhigh",
        )
    assert create_mock.call_args.kwargs["output_config"] == {"effort": "xhigh"}


@pytest.mark.asyncio
async def test_opus_47_legacy_numeric_budget_is_bucketed():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-7",
            max_tokens=100,
            reasoning_effort="24000",
        )
    assert create_mock.call_args.kwargs["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_opus_46_off_sends_no_thinking_or_output_config():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-6",
            max_tokens=100,
            reasoning_effort="none",
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


@pytest.mark.asyncio
async def test_opus_47_without_thinking_sends_nothing_extra():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-opus-4-7",
            max_tokens=100,
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


@pytest.mark.asyncio
async def test_haiku_thinking_uses_legacy_budget_payload():
    messages = [{"role": "user", "content": "hi"}]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages, tools=None,
            model="anthropic/claude-haiku-4-5",
            max_tokens=100,
            reasoning_effort="high",
        )
    kwargs = create_mock.call_args.kwargs
    assert "reasoning_effort" not in kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 16000}
    assert "output_config" not in kwargs
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_cache_breakpoint_skips_reasoning_blocks_and_marks_tool_use():
    messages = [
        {"role": "user", "content": "Start."},
        {
            "role": "assistant",
            "content": [{
                "type": "thinking",
                "thinking": "private provider state",
                "signature": "signed-state",
            }, {
                "type": "redacted_thinking",
                "data": "opaque-provider-state",
            }],
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "done",
        },
        {"role": "user", "content": "Continue."},
    ]
    patcher, create_mock = _patch_anthropic_sdk()
    with patcher:
        await anthropic_completion(
            messages,
            tools=None,
            model="anthropic/claude-opus-4-7",
            max_tokens=100,
        )

    assistant_blocks = create_mock.call_args.kwargs["messages"][1]["content"]
    assert assistant_blocks[0] == {
        "type": "thinking",
        "thinking": "private provider state",
        "signature": "signed-state",
    }
    assert assistant_blocks[1] == {
        "type": "redacted_thinking",
        "data": "opaque-provider-state",
    }
    assert assistant_blocks[2] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "lookup",
        "input": {},
        "cache_control": {"type": "ephemeral"},
    }


def test_anthropic_adapter_classifies_signed_thinking():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="provider thinking",
                signature="signed-thinking",
            ),
            SimpleNamespace(type="text", text="answer"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    result = _parse_response(response, model="anthropic/claude-opus-4-7")

    assert result.reasoning_content == "provider thinking"
    assert result.reasoning_provenance == {
        "representation_kind": "anthropic_thinking",
        "native_signature": "signed-thinking",
        "native_signatures": ["signed-thinking"],
        "native_blocks": [{
            "type": "thinking",
            "thinking": "provider thinking",
            "signature": "signed-thinking",
        }],
    }


def test_anthropic_adapter_preserves_ordered_deduped_thinking_signatures():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="second",
                signature="signature-second",
            ),
            SimpleNamespace(
                type="thinking",
                thinking="first",
                signature="signature-first",
            ),
            SimpleNamespace(
                type="thinking",
                thinking="duplicate",
                signature="signature-second",
            ),
            SimpleNamespace(type="text", text="answer"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    result = _parse_response(response, model="anthropic/claude-opus-4-7")

    assert result.reasoning_content == "secondfirstduplicate"
    assert result.reasoning_provenance == {
        "representation_kind": "anthropic_thinking",
        "native_signatures": ["signature-second", "signature-first"],
        "native_blocks": [
            {"type": "thinking", "thinking": "second", "signature": "signature-second"},
            {"type": "thinking", "thinking": "first", "signature": "signature-first"},
            {"type": "thinking", "thinking": "duplicate", "signature": "signature-second"},
        ],
    }


def test_anthropic_adapter_preserves_exact_model_and_complete_typed_blocks():
    response = SimpleNamespace(
        model="claude-fable-5",
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="first thought",
                signature="signature-first",
            ),
            SimpleNamespace(
                type="redacted_thinking",
                data="redacted-provider-payload",
            ),
            SimpleNamespace(
                type="thinking",
                thinking="second thought",
                signature="signature-second",
            ),
            SimpleNamespace(type="text", text="answer"),
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    result = _parse_response(response, model="anthropic/catalog-alias")

    assert result.reported_model == "claude-fable-5"
    assert result.reasoning_content == "first thoughtsecond thought"
    assert result.reasoning_provenance == {
        "representation_kind": "anthropic_thinking",
        "source_effective_model": "claude-fable-5",
        "native_signatures": ["signature-first", "signature-second"],
        "native_blocks": [
            {
                "type": "thinking",
                "thinking": "first thought",
                "signature": "signature-first",
            },
            {
                "type": "redacted_thinking",
                "data": "redacted-provider-payload",
            },
            {
                "type": "thinking",
                "thinking": "second thought",
                "signature": "signature-second",
            },
        ],
    }


class _AsyncEventIter:

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


@pytest.mark.asyncio
async def test_anthropic_stream_classifies_signature_delta():
    events = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="provider thinking"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="signature_delta", signature="signed-thinking"),
        ),
        # The provider's terminal event: a stream that ends without it is cut off (#2485).
        SimpleNamespace(type="message_stop"),
    ]
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_AsyncEventIter(events))
    accepted = AsyncMock()

    result = await _streaming_completion(
        client,
        {"messages": [], "model": "claude-opus-4-7", "max_tokens": 100},
        model="anthropic/claude-opus-4-7",
        on_reasoning_chunk=None,
        on_content_chunk=None,
        on_tool_call_chunk=None,
        on_response_started=accepted,
    )

    accepted.assert_awaited_once_with()
    assert result.reasoning_content == "provider thinking"
    assert result.reasoning_provenance == {
        "representation_kind": "anthropic_thinking",
        "native_signature": "signed-thinking",
        "native_signatures": ["signed-thinking"],
        "native_blocks": [{
            "type": "thinking",
            "thinking": "provider thinking",
            "signature": "signed-thinking",
        }],
    }


@pytest.mark.asyncio
async def test_anthropic_stream_preserves_reported_model_and_redacted_blocks():
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                model="claude-fable-5",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="thinking", thinking="", signature=""),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="streamed thought"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="signature_delta", signature="stream-signature"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="redacted_thinking",
                data="stream-redacted-payload",
            ),
        ),
        # The provider's terminal event: a stream that ends without it is cut off (#2485).
        SimpleNamespace(type="message_stop"),
    ]
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_AsyncEventIter(events))

    result = await _streaming_completion(
        client,
        {"messages": [], "model": "catalog-alias", "max_tokens": 100},
        model="anthropic/catalog-alias",
        on_reasoning_chunk=None,
        on_content_chunk=None,
        on_tool_call_chunk=None,
    )

    assert result.reported_model == "claude-fable-5"
    assert result.reasoning_provenance == {
        "representation_kind": "anthropic_thinking",
        "source_effective_model": "claude-fable-5",
        "native_signature": "stream-signature",
        "native_signatures": ["stream-signature"],
        "native_blocks": [
            {
                "type": "thinking",
                "thinking": "streamed thought",
                "signature": "stream-signature",
            },
            {
                "type": "redacted_thinking",
                "data": "stream-redacted-payload",
            },
        ],
    }
