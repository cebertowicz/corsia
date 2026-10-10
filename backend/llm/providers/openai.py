"""OpenAI Responses API provider using the official SDK."""

from __future__ import annotations

import asyncio
from typing import Any

import openai
from openai import AsyncOpenAI
from loguru import logger

from infra.constants import LLM_MAX_RETRIES as _MAX_RETRIES, LLM_RETRY_DELAYS as _RETRY_DELAYS
from llm.providers.openai_streaming import _streaming_responses_completion
from llm.stream_idle import PROVIDER_CUT_OFF_ERRORS, error_kind_of
from llm.providers.openai_translation import (
    _ARESPONSES_KWARGS,
    _messages_to_responses_input,
    _parse_responses_response,
    _reasoning_active,
    _strip_openai_prefix,
    _to_text,
    _tools_to_responses_format,
)

__all__ = [
    "openai_completion",
    "_ARESPONSES_KWARGS",
    "_get_client",
    "_is_sdk_transient",
    "_messages_to_responses_input",
    "_parse_responses_response",
    "_reasoning_active",
    "_streaming_responses_completion",
    "_strip_openai_prefix",
    "_to_text",
    "_tools_to_responses_format",
]


_CLIENTS: dict[str | None, AsyncOpenAI] = {}


def _get_client(api_key: str | None) -> AsyncOpenAI:
    """Return a cached AsyncOpenAI client for ``api_key``."""
    client = _CLIENTS.get(api_key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
        _CLIENTS[api_key] = client
    return client


def _is_sdk_transient(e: Exception) -> bool:
    """True for SDK exceptions that warrant a retry."""
    if isinstance(e, PROVIDER_CUT_OFF_ERRORS):
        return True
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        return True
    if isinstance(e, openai.RateLimitError):
        return True
    if isinstance(e, openai.InternalServerError):
        return True
    if isinstance(e, openai.APIStatusError):
        status = getattr(e, "status_code", None)
        return isinstance(status, int) and status >= 500
    return False


async def openai_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str,
    max_tokens: int | None = None,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    on_reasoning_chunk=None,
    on_content_chunk=None,
    on_tool_call_chunk=None, on_response_started=None,
    retry_transient_errors: bool = True,
) -> "LLMResponse":
    """OpenAI completion via Responses API with transient-error retries."""
    if max_tokens is not None:
        max_tokens = max(1, max_tokens)

    instructions, input_items = _messages_to_responses_input(messages)
    resp_tools = _tools_to_responses_format(tools)

    kwargs: dict[str, Any] = {
        "model": _strip_openai_prefix(model),
        "input": input_items,
    }
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    if instructions:
        kwargs["instructions"] = instructions
    if resp_tools:
        kwargs["tools"] = resp_tools
        kwargs["tool_choice"] = "auto"
    if _reasoning_active(reasoning_effort):
        kwargs["reasoning"] = {"effort": reasoning_effort}

    client = _get_client(api_key)
    if not retry_transient_errors:
        client = client.with_options(max_retries=0)
    use_streaming = (
        on_reasoning_chunk is not None
        or on_content_chunk is not None
        or on_tool_call_chunk is not None
    )

    response = await _responses_with_retries(
        client, kwargs,
        model=model, messages=messages, tools=tools, max_tokens=max_tokens,
        use_streaming=use_streaming,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        retry_transient_errors=retry_transient_errors,
    )
    response.effort_sent = (kwargs.get("reasoning") or {}).get("effort")
    return response


async def _responses_with_retries(
    client: AsyncOpenAI,
    kwargs: dict[str, Any],
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int | None,
    use_streaming: bool,
    on_reasoning_chunk,
    on_content_chunk,
    on_tool_call_chunk, on_response_started,
    retry_transient_errors: bool,
) -> "LLMResponse":
    """Dispatch with transient retries and the I7 overflow discovery re-entry."""
    from llm.router import LLMResponse

    transient_attempts = _MAX_RETRIES if retry_transient_errors else 1
    attempt = 0
    overflow_retried = False
    while True:
        try:
            if use_streaming:
                result = await _streaming_responses_completion(
                    client, kwargs, model=model,
                    on_reasoning_chunk=on_reasoning_chunk,
                    on_content_chunk=on_content_chunk,
                    on_tool_call_chunk=on_tool_call_chunk,
                    on_response_started=on_response_started,
                )
            else:
                response = await client.responses.create(**kwargs)
                result = _parse_responses_response(response, model=model)
            if result.finish_reason == "length":
                logger.warning(
                    "OpenAI response truncated (finish_reason=length) — model={}, "
                    "max_output_tokens={}. Reasoning tokens may have starved the "
                    "visible answer; consider raising max_output_tokens or lowering "
                    "reasoning_effort.",
                    model, kwargs.get("max_output_tokens"),
                )
            return result
        except Exception as e:
            from llm.base import _humanize_error
            from llm.provider_overflow import (
                OverflowDisposition,
                context_overflow_response,
                overflow_disposition,
            )
            disposition = await overflow_disposition(
                e, model, messages=messages, tools=tools,
                max_tokens=max_tokens, already_retried=overflow_retried,
            )
            if disposition is OverflowDisposition.RETRY:
                overflow_retried = True
                continue
            if disposition is OverflowDisposition.HANDLED:
                return context_overflow_response()
            attempt += 1
            if _is_sdk_transient(e) and attempt < transient_attempts:
                wait = _RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "OpenAI Responses API failed (attempt {}/{}), retrying in {}s: {}",
                    attempt, transient_attempts, wait, e,
                )
                await asyncio.sleep(wait)
                continue
            logger.error(
                "OpenAI Responses API failed (attempt {}/{}): {}",
                attempt, transient_attempts, e,
            )
            return LLMResponse(
                content=f"Error calling LLM: {_humanize_error(e, model)}",
                finish_reason="error",
                error_kind=error_kind_of(e),
            )
