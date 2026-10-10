
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


@pytest.fixture(autouse=True)
def _loaded_omlx_preflight(monkeypatch):
    async def loaded(_api_base, _model, *, server_id=None):
        assert server_id is None

    monkeypatch.setattr(
        "llm.providers.omlx_lifecycle.require_loaded_model",
        loaded,
    )


@pytest.mark.asyncio
async def test_router_sends_no_sampling_and_no_extra_body():
    from llm.router import chat_completion
    with patch("llm.capabilities.get_model_max_output_tokens", return_value=4096), \
         patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [{"role": "user", "content": "hi"}],
            model="hosted_vllm/qwen3.6-35b",
        )
    assert "extra_body" not in mock_ac.call_args.kwargs
    assert "max_tokens" not in mock_ac.call_args.kwargs
    assert "temperature" not in mock_ac.call_args.kwargs


@pytest.mark.asyncio
async def test_external_row_with_legacy_sampling_values_sends_none():
    """Older rows stored 0.3/8192 and per-model overrides; none reach the wire."""
    from llm.router import chat_completion

    legacy_row = {
        "id": "ext-1",
        "provider": "external",
        "litellm_model": "external@ns-1/deepseek-v4.1-flash",
        "default_temperature": 0.3,
        "default_max_tokens": None,
        "inference_overrides": json.dumps({
            "temperature": 0.2, "top_p": 0.9, "max_tokens": 1024,
        }),
    }
    with patch("llm.catalog.get_model", return_value=legacy_row), \
         patch("llm.catalog.get_model_by_litellm", return_value=legacy_row), \
         patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [{"role": "user", "content": "hi"}],
            model="openai/deepseek-v4.1-flash",
            api_base="https://example.test/v1",
        )
    sent = mock_ac.call_args.kwargs
    for knob in ("temperature", "top_p", "top_k", "max_tokens",
                 "repetition_penalty", "presence_penalty", "frequency_penalty"):
        assert knob not in sent
        assert knob not in (sent.get("extra_body") or {})


@pytest.mark.asyncio
async def test_router_omits_anthropic_cap_when_caller_does_not_set_one():
    from llm.router import LLMResponse, chat_completion

    with patch(
        "llm.providers.anthropic.anthropic_completion", new_callable=AsyncMock
    ) as mock_completion:
        mock_completion.return_value = LLMResponse(content="ok")
        await chat_completion(
            [{"role": "user", "content": "ordinary Anthropic turn"}],
            model="anthropic/claude-sonnet-4-6",
        )

    assert "max_tokens" not in mock_completion.call_args.kwargs


@pytest.mark.asyncio
async def test_router_honors_explicit_output_cap_below_catalog():
    import llm.base  # noqa: F401
    from llm.router import chat_completion

    with patch(
        "llm.capabilities.get_model_max_output_tokens", return_value=64_000
    ), patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [{"role": "user", "content": "bounded checkpoint"}],
            model="hosted_vllm/qwen3.6-35b",
            max_tokens=8_192,
        )

    assert mock_ac.call_args.kwargs["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_router_honors_explicit_output_cap_for_managed_omlx():
    import llm.base  # noqa: F401
    from llm.router import chat_completion

    with patch(
        "llm.capabilities.get_model_max_output_tokens", return_value=64_000
    ), patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [{"role": "user", "content": "bounded checkpoint"}],
            model="openai/ReasoningModel",
            api_key="omlx",
            api_base="http://omlx.test/v1",
            max_tokens=8_192,
        )

    assert mock_ac.call_args.kwargs["max_tokens"] == 8_192


@pytest.mark.asyncio
async def test_managed_omlx_omits_output_cap_when_caller_does_not_set_one():
    import llm.base  # noqa: F401
    from llm.router import chat_completion

    with patch(
        "llm.capabilities.get_model_max_output_tokens", return_value=64_000
    ), patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [{"role": "user", "content": "ordinary managed turn"}],
            model="openai/ReasoningModel",
            api_key="omlx",
            api_base="http://omlx.test/v1",
        )

    assert "max_tokens" not in mock_ac.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_thinking", [True, False])
async def test_managed_omlx_threads_known_preserve_default_with_prior_reasoning(
    preserve_thinking,
):
    from llm.router import chat_completion

    with patch("llm.capabilities.get_model_max_output_tokens", return_value=4096), \
         patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            [
                {"role": "user", "content": "first"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "prior work",
                },
                {"role": "user", "content": "second"},
            ],
            model="openai/ReasoningModel",
            api_key="omlx",
            api_base="http://omlx.test/v1",
            preserve_thinking=preserve_thinking,
        )

    assert mock_ac.call_args.kwargs["messages"][1]["reasoning_content"] == "prior work"
    assert mock_ac.call_args.kwargs["extra_body"]["chat_template_kwargs"] == {
        "preserve_thinking": preserve_thinking,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve_thinking", [None, True, False])
async def test_managed_omlx_omits_preserve_kwarg_when_unknown_or_no_prior_reasoning(
    preserve_thinking,
):
    from llm.router import chat_completion

    messages = [{"role": "user", "content": "no prior reasoning"}]
    if preserve_thinking is None:
        messages = [
            {"role": "assistant", "content": "answer", "reasoning_content": "prior"},
            {"role": "user", "content": "next"},
        ]
    with patch("llm.capabilities.get_model_max_output_tokens", return_value=4096), \
         patch("llm.base.acompletion", new_callable=AsyncMock) as mock_ac:
        mock_ac.return_value = _fake_response()
        await chat_completion(
            messages,
            model="openai/ReasoningModel",
            api_key="omlx",
            api_base="http://omlx.test/v1",
            preserve_thinking=preserve_thinking,
        )

    extra_body = mock_ac.call_args.kwargs.get("extra_body") or {}
    assert "preserve_thinking" not in extra_body.get("chat_template_kwargs", {})
