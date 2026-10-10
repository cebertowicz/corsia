"""Generic chat completions via litellm.acompletion().

Used for the managed engine (oMLX) and External openai-dialect providers
(``openai/<model>`` + api_base), and any provider without a dedicated module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import json_repair
from litellm import acompletion
from loguru import logger

from infra import provider_usage
from infra.constants import LLM_MAX_RETRIES as _MAX_RETRIES, LLM_RETRY_DELAYS as _RETRY_DELAYS
from llm.base_streaming import streaming_completion as _streaming_completion_impl

_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})


_ACOMPLETION_KWARGS = frozenset({
    "model", "messages", "tools", "tool_choice", "max_tokens",
    "api_key", "api_base", "extra_headers", "reasoning_effort",
    "thinking", "output_config",
    "extra_body", "stream", "stream_options", "num_retries",
})


def filter_kwargs(kwargs: dict[str, Any], allowed: frozenset[str], where: str) -> dict[str, Any]:
    """Retain only allowed kwargs and log any drops."""
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, val in kwargs.items():
        if key in allowed:
            kept[key] = val
        else:
            dropped.append(key)
    if dropped:
        logger.warning("dropped unsupported kwargs at {}: {}", where, dropped)
    return kept


def _humanize_error(error: Exception, model: str) -> str:
    """Turn cryptic LLM errors into messages a human can act on."""
    msg = str(error).lower()
    if "upgrade required" in msg or "426" in msg:
        return (
            f"The server for '{model}' returned 'Upgrade Required' (HTTP 426) — "
            "its endpoint is not serving the expected HTTP API. Check the "
            "provider's URL/port and that the server is running."
        )
    if "connection refused" in msg or "connect error" in msg:
        return "The provider endpoint is not running or not reachable. Start the server and try again."
    if "model not loaded" in msg or ("404" in msg and "not loaded" in msg):
        return (
            f"'{model}' is no longer loaded on its server. "
            f"Load it on the remote server and try again."
        )
    if "rate limit" in msg or "429" in msg:
        return "Rate limited by the API provider. Wait a moment and try again."
    if "authentication" in msg or "401" in msg or "invalid api key" in msg:
        return f"Authentication failed for {model}. Check your API key in Settings > Providers."
    if "timeout" in msg:
        return f"Request to {model} timed out. The model may be overloaded or the server is slow."
    raw = str(error)
    for prefix in ["APIError: OpenAIException - ", "APIError: ", "OpenAIException - "]:
        if prefix in raw:
            raw = raw.split(prefix, 1)[1]
            break
    return raw


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-standard keys and ensure assistant messages have a content key."""
    sanitized = []
    for msg in messages:
        clean = {k: v for k, v in msg.items() if k in _ALLOWED_MSG_KEYS}
        if clean.get("role") == "assistant":
            if "content" not in clean:
                clean["content"] = None
            if "reasoning_content" in msg:
                clean["reasoning_content"] = msg["reasoning_content"]
        sanitized.append(clean)
    return sanitized


def sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace empty text content that causes provider 400 errors."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")

        if isinstance(content, str) and not content:
            clean = dict(msg)
            clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
            result.append(clean)
            continue

        if isinstance(content, list):
            filtered = [
                item for item in content
                if not (
                    isinstance(item, dict)
                    and item.get("type") in ("text", "input_text", "output_text")
                    and not item.get("text")
                )
            ]
            if len(filtered) != len(content):
                clean = dict(msg)
                if filtered:
                    clean["content"] = filtered
                elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                    clean["content"] = None
                else:
                    clean["content"] = "(empty)"
                result.append(clean)
                continue

        result.append(msg)
    return result


def _usage_from_litellm_response(
    response: Any, *, model: str, api_base: str | None,
) -> dict[str, Any]:
    """Build the usage ledger row for a litellm response; {} when it has none."""
    usage: dict[str, Any] = {}
    if hasattr(response, "usage") and response.usage:
        u = response.usage
        # One normalization for every vendor served through this path. Vendors
        # disagree about whether the cache counts sit inside prompt_tokens, so
        # infra.provider_usage owns that reconciliation and hands back disjoint
        # counts -- summing these columns is then always the real total.
        measured = provider_usage.from_litellm(
            u, model=model, api_base=api_base,
        )
        if measured is None:
            logger.warning(
                "llm_usage: {} reported no usable token measurement; "
                "recording the call as unmeasured",
                model,
            )
        usage = (
            measured.ledger_row()
            if measured is not None
            else provider_usage.unmeasured_ledger_row()
        )
        logger.debug(
            "llm_usage_raw: prompt_tokens={} completion={} total={} "
            "cache_creation={} cache_read={} details={}",
            u.prompt_tokens, u.completion_tokens, u.total_tokens,
            getattr(u, "cache_creation_input_tokens", None),
            getattr(u, "cache_read_input_tokens", None),
            getattr(u, "prompt_tokens_details", None),
        )
    return usage


def parse_response(
    response: Any, model: str = "", api_base: str | None = None,
) -> "LLMResponse":
    """Parse litellm acompletion response into LLMResponse."""
    from llm.router import LLMResponse, ToolCallRequest

    choice = response.choices[0]
    message = choice.message

    tool_calls = []
    if hasattr(message, "tool_calls") and message.tool_calls:
        for tc in message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json_repair.loads(args)

            tool_calls.append(ToolCallRequest(
                id=tc.id,
                name=tc.function.name,
                arguments=args,
            ))

    usage = _usage_from_litellm_response(response, model=model, api_base=api_base)

    cost_usd: float | None = None
    try:
        from llm.pricing import calculate_cost
        cost_usd = calculate_cost(model, usage)
    except Exception as e:
        logger.warning("cost calculation failed for {}: {}", model, e)
    usage["cost_usd"] = cost_usd
    usage["model"] = model

    reasoning_content = getattr(message, "reasoning_content", None) or None
    reasoning_provenance = (
        {"representation_kind": "reasoning_content"}
        if reasoning_content is not None
        else None
    )

    return LLMResponse(
        content=message.content,
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason or "stop",
        usage=usage,
        reasoning_content=reasoning_content,
        reasoning_provenance=reasoning_provenance,
        reported_model=(
            str(response.model).strip()
            if isinstance(getattr(response, "model", None), str)
            and str(response.model).strip()
            else None
        ),
    )


async def _streaming_completion(
    kwargs: dict[str, Any], model: str,
    on_reasoning_chunk: Any, on_content_chunk: Any,
    on_tool_call_chunk: Any = None, on_response_started: Any = None,
) -> "LLMResponse":
    return await _streaming_completion_impl(
        kwargs,
        model,
        on_reasoning_chunk,
        on_content_chunk,
        on_tool_call_chunk, on_response_started,
        acompletion_fn=acompletion,
    )


async def _omlx_loaded_only_preflight(
    api_key: str | None,
    api_base: str | None,
    model: str,
    server_id: str | None = None,
) -> "LLMResponse | None":
    if api_key != "omlx":
        return None
    from llm.providers.omlx_lifecycle import (
        OmlxLoadedStateError,
        require_loaded_model,
    )
    from llm.router import LLMResponse

    try:
        await require_loaded_model(api_base, model, server_id=server_id)
    except OmlxLoadedStateError as error:
        logger.error("oMLX loaded-only preflight failed for {}: {}", model, error)
        return LLMResponse(
            content=f"Error calling LLM: {_humanize_error(error, model)}",
            finish_reason="error",
        )
    return None


def _merge_provider_extra_body(
    kwargs: dict[str, Any],
    thread_id: str | None,
    extra_body: dict[str, Any] | None,
) -> None:
    if thread_id:
        kwargs.setdefault("extra_body", {})["session_id"] = thread_id
    if not extra_body:
        return
    merged = kwargs.setdefault("extra_body", {})
    for key, value in extra_body.items():
        if key != "session_id":
            merged[key] = value


async def base_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str,
    max_tokens: int | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    omlx_server_id: str | None = None,
    on_reasoning_chunk: Any = None,
    on_content_chunk: Any = None,
    on_tool_call_chunk: Any = None, on_response_started: Any = None,
    reasoning_effort: str | None = None,
    thread_id: str | None = None,
    thinking: dict[str, Any] | None = None,
    output_config: dict[str, Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    retry_transient_errors: bool = True,
) -> "LLMResponse":
    """Generic litellm transport after provider-specific shaping."""
    preflight_error = await _omlx_loaded_only_preflight(api_key, api_base, model, omlx_server_id)
    if preflight_error is not None:
        return preflight_error

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": sanitize_messages(sanitize_empty_content(messages)),
    }

    if max_tokens is not None:
        kwargs["max_tokens"] = max(1, max_tokens)
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if api_key == "omlx" and omlx_server_id:
        from llm.providers.omlx_public_auth import public_headers
        kwargs["extra_headers"] = await public_headers({"server_id": omlx_server_id}, url=api_base)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    if thinking:
        kwargs["thinking"] = thinking
    if output_config:
        kwargs["output_config"] = output_config
    _merge_provider_extra_body(kwargs, thread_id, extra_body)
    if not retry_transient_errors:
        kwargs["num_retries"] = 0

    use_streaming = on_reasoning_chunk is not None or on_content_chunk is not None

    kwargs = filter_kwargs(kwargs, _ACOMPLETION_KWARGS, "base_completion → acompletion")

    return await _completion_with_retries(
        kwargs,
        model=model,
        use_streaming=use_streaming,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        retry_transient_errors=retry_transient_errors,
    )


async def _completion_with_retries(
    kwargs: dict[str, Any],
    *,
    model: str,
    use_streaming: bool,
    on_reasoning_chunk: Any,
    on_content_chunk: Any,
    on_tool_call_chunk: Any, on_response_started: Any,
    retry_transient_errors: bool,
) -> "LLMResponse":
    """Dispatch with transient retries and the I7 overflow discovery re-entry.

    A classified context-overflow rejection re-reads the live window, re-fits
    the payload, and retries once; if it still overflows it returns the
    handled ``provider_context_overflow`` response, never a provider sentence.
    """
    from llm.provider_overflow import (
        OverflowDisposition,
        context_overflow_response,
        overflow_disposition,
    )
    from llm.router import _is_transient_error, provider_error_response

    transient_attempts = _MAX_RETRIES if retry_transient_errors else 1
    attempt = 0
    overflow_retried = False
    while True:
        try:
            if use_streaming:
                return await _streaming_completion(
                    kwargs, model, on_reasoning_chunk, on_content_chunk,
                    on_tool_call_chunk, on_response_started,
                )
            response = await acompletion(**kwargs)
            # Read the endpoint from the request actually sent, not from an
            # outer variable: it decides how the response's cache counts are
            # interpreted, so it has to be the value the provider saw.
            return parse_response(
                response, model=model, api_base=kwargs.get("api_base"),
            )
        except Exception as e:
            disposition = await overflow_disposition(
                e,
                model,
                messages=kwargs.get("messages") or [],
                tools=kwargs.get("tools"),
                max_tokens=kwargs.get("max_tokens"),
                already_retried=overflow_retried,
            )
            if disposition is OverflowDisposition.RETRY:
                overflow_retried = True
                continue
            if disposition is OverflowDisposition.HANDLED:
                return context_overflow_response()
            attempt += 1
            if _is_transient_error(e) and attempt < transient_attempts:
                wait = _RETRY_DELAYS[attempt - 1]
                logger.warning("LLM call failed (attempt {}/{}), retrying in {}s: {}", attempt, transient_attempts, wait, e)
                await asyncio.sleep(wait)
                continue
            logger.error("LLM call failed (attempt {}/{}): {}", attempt, transient_attempts, e)
            return provider_error_response(e, f"Error calling LLM: {_humanize_error(e, model)}")
