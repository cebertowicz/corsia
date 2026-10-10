
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.base import base_completion  # noqa: E402


def _fake_response() -> object:
    choice = SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=None),
        finish_reason="stop",
    )
    usage = SimpleNamespace(
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_no_temperature_or_output_cap_injected():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [{"role": "user", "content": "hi"}], tools=None,
            model="anything/model",
        )
    assert "temperature" not in mock_ac.call_args.kwargs
    assert "max_tokens" not in mock_ac.call_args.kwargs


@pytest.mark.asyncio
async def test_tools_and_tool_choice_forwarded_together():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        tools = [{"type": "function", "function": {"name": "x"}}]
        await base_completion(
            [{"role": "user", "content": "hi"}], tools=tools,
            model="anything/model", 
        )
    assert mock_ac.call_args.kwargs["tools"] == tools
    assert mock_ac.call_args.kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_no_tools_omits_tool_choice():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [{"role": "user", "content": "hi"}], tools=None,
            model="anything/model", 
        )
    assert "tools" not in mock_ac.call_args.kwargs
    assert "tool_choice" not in mock_ac.call_args.kwargs


@pytest.mark.asyncio
async def test_omlx_transport_preserves_openai_image_content():
    """A proven oMLX VLM receives native image_url blocks unchanged."""
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "content": "[Image: shot.png]"},
        {
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,cG5n"},
            }],
        },
    ]
    with (
        patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac,
        patch(
            "llm.base._omlx_loaded_only_preflight",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_ac.return_value = _fake_response()
        await base_completion(
            messages,
            tools=None,
            model="openai/VisionModel",
            api_key="omlx",
            api_base="http://127.0.0.1:5100/v1",
        )

    assert mock_ac.call_args.kwargs["messages"] == messages


@pytest.mark.asyncio
async def test_extra_body_sampling_overrides_passed_through():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [{"role": "user", "content": "hi"}], tools=None,
            model="anything/model", 
            extra_body={"repetition_penalty": 1.15, "top_k": 40},
        )
    eb = mock_ac.call_args.kwargs.get("extra_body") or {}
    assert eb.get("repetition_penalty") == 1.15
    assert eb.get("top_k") == 40


@pytest.mark.asyncio
async def test_extra_body_does_not_override_session_id():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [{"role": "user", "content": "hi"}], tools=None,
            model="anything/model", 
            thread_id="real-session",
            extra_body={"session_id": "imposter", "top_p": 0.9},
        )
    eb = mock_ac.call_args.kwargs.get("extra_body") or {}
    assert eb.get("session_id") == "real-session"
    assert eb.get("top_p") == 0.9


@pytest.mark.asyncio
async def test_assistant_reasoning_content_survives_generic_transport_sanitizer():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "prior private work",
                    "corsia_internal": "must not escape",
                },
            ],
            tools=None,
            model="openai-compatible/model",
        )

    assert mock_ac.call_args.kwargs["messages"][1] == {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "prior private work",
    }


@pytest.mark.asyncio
async def test_reasoning_content_is_stripped_from_non_assistant_roles():
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await base_completion(
            [
                {
                    "role": "user",
                    "content": "question",
                    "reasoning_content": "not assistant reasoning",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "result",
                    "reasoning_content": "not tool data",
                },
            ],
            tools=None,
            model="openai-compatible/model",
        )

    sent = mock_ac.call_args.kwargs["messages"]
    assert all("reasoning_content" not in message for message in sent)


@pytest.mark.asyncio
async def test_generic_adapter_classifies_plaintext_reasoning_content():
    response = _fake_response()
    response.choices[0].message.reasoning_content = "provider plaintext reasoning"
    with patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = response
        result = await base_completion(
            [{"role": "user", "content": "question"}],
            tools=None,
            model="openai-compatible/model",
        )

    assert result.reasoning_provenance == {
        "representation_kind": "reasoning_content",
    }


@pytest.mark.asyncio
async def test_generic_stream_adapter_classifies_plaintext_reasoning_content():
    async def _stream():
        yield SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content="streamed reasoning",
                    tool_calls=None,
                ),
                finish_reason="stop",
            )],
            usage=None,
        )

    async def _completion(**_kwargs):
        return _stream()

    seen = []

    async def _on_reasoning(chunk):
        seen.append(chunk)

    with patch("llm.base.acompletion", new=_completion):
        result = await base_completion(
            [{"role": "user", "content": "question"}],
            tools=None,
            model="openai-compatible/model",
            on_reasoning_chunk=_on_reasoning,
        )

    assert seen == ["streamed reasoning"]
    assert result.reasoning_provenance == {
        "representation_kind": "reasoning_content",
    }
