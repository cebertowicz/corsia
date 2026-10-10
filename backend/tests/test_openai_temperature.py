
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.providers.openai import (  # noqa: E402
    _reasoning_active,
    openai_completion,
)


class TestReasoningActive:
    def test_low(self):
        assert _reasoning_active("low") is True

    def test_medium(self):
        assert _reasoning_active("medium") is True

    def test_high(self):
        assert _reasoning_active("high") is True

    def test_xhigh(self):
        assert _reasoning_active("xhigh") is True

    def test_minimal(self):
        assert _reasoning_active("minimal") is True

    def test_auto_is_off(self):
        assert _reasoning_active("auto") is False

    def test_none_is_off(self):
        assert _reasoning_active("none") is False

    def test_null_is_off(self):
        assert _reasoning_active(None) is False

    def test_empty_is_off(self):
        assert _reasoning_active("") is False


def _fake_responses_response() -> object:
    return SimpleNamespace(
        output_text="hi",
        output=[],
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def _make_mock_client(create_mock: AsyncMock) -> MagicMock:
    client = MagicMock(name="AsyncOpenAI")
    client.responses = MagicMock(name="responses")
    client.responses.create = create_mock
    return client


@pytest.mark.asyncio
async def test_ordinary_openai_call_omits_output_cap():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5",
        )
    assert "max_output_tokens" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_o1_omits_temperature_on_first_call():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/o1", max_tokens=100,
        )
    assert create_mock.call_count == 1
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_o3_mini_omits_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/o3-mini", max_tokens=100,
        )
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt55_with_auto_omits_temperature_regression():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5.5", max_tokens=100,
            reasoning_effort="auto",
        )
    assert create_mock.call_count == 1
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt55_with_no_effort_omits_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5.5", max_tokens=100,
            reasoning_effort=None,
        )
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt5_nano_with_auto_omits_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5-nano", max_tokens=100,
            reasoning_effort="auto",
        )
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt5_with_reasoning_engaged_omits_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            reasoning_effort="high",
        )
    assert create_mock.call_count == 1
    assert "temperature" not in create_mock.call_args.kwargs
    assert create_mock.call_args.kwargs["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_gpt5_with_reasoning_off_sends_no_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            reasoning_effort="none",
        )
    assert "temperature" not in create_mock.call_args.kwargs
    assert "reasoning" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt5_with_auto_effort_sends_no_temperature_and_no_reasoning():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            reasoning_effort="auto",
        )
    assert "temperature" not in create_mock.call_args.kwargs
    assert "reasoning" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_gpt4o_sends_no_temperature():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-4o", max_tokens=100,
        )
    assert "temperature" not in create_mock.call_args.kwargs


@pytest.mark.asyncio
async def test_no_retry_loop_on_temperature_error():
    create_mock = AsyncMock(
        side_effect=Exception("400: temperature is not supported for this model")
    )
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-4o", max_tokens=100,
        )
    assert create_mock.call_count == 1


@pytest.mark.asyncio
async def test_model_prefix_stripped_for_sdk():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
        )
    assert create_mock.call_args.kwargs["model"] == "gpt-5"


@pytest.mark.asyncio
async def test_api_key_passed_to_client_construction():
    create_mock = AsyncMock(return_value=_fake_responses_response())
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client) as gc:
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-4o", max_tokens=100,
            api_key="sk-test-123",
        )
    gc.assert_called_once_with("sk-test-123")
    assert "api_key" not in create_mock.call_args.kwargs
