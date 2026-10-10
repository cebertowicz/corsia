"""Anthropic completions via the official SDK."""

from __future__ import annotations

import asyncio
from typing import Any

import anthropic
import json_repair
from loguru import logger

from infra.constants import LLM_MAX_RETRIES as _MAX_RETRIES, LLM_RETRY_DELAYS as _RETRY_DELAYS
from llm.providers.anthropic_streaming import (
    _merge_stream_usage,
    _streaming_completion,
)
from llm.providers.anthropic_thinking import (
    adaptive_thinking_kwargs as _adaptive_thinking_kwargs,
    effort_enum_for as _effort_enum_for,
    effort_level_from as _effort_level_from,
    legacy_thinking_budget as _legacy_thinking_budget,
    model_uses_adaptive_thinking as _model_uses_adaptive_thinking,
    strip_anthropic_prefix as _strip_anthropic_prefix,
    thinking_active as _thinking_active,
)
from llm.providers.anthropic_translation import (
    _annotate_usage,
    _apply_cache_control,
    _map_stop_reason,
    _parse_usage,
    _stringify_content,
    _translate_content_blocks,
    _translate_messages_for_anthropic_sdk,
    _translate_tools_for_anthropic_sdk,
)

__all__ = [
    "anthropic_completion",
    "_adaptive_thinking_kwargs", "_effort_enum_for", "_effort_level_from",
    "_legacy_thinking_budget",
    "_model_uses_adaptive_thinking", "_strip_anthropic_prefix",
    "_thinking_active",
    "_apply_cache_control",
    "_translate_content_blocks", "_translate_messages_for_anthropic_sdk",
    "_translate_tools_for_anthropic_sdk", "_stringify_content",
    "_map_stop_reason", "_parse_usage", "_annotate_usage",
    "_streaming_completion", "_merge_stream_usage",
    "_parse_response",
]

_SDK_MAX_RETRIES = 0

#: The Messages API requires ``max_tokens``, so this adapter cannot omit it the
#: way the generic and OpenAI transports do (#1043). It is the one output limit
#: CORSIA sends without a caller budget; no sampling knob is ever sent. The
#: ceiling comes from the model's own catalog maximum; this anchor is only the
#: floor used when the catalog reports none. Discovering a real per-model
#: maximum is #1287.
_REQUIRED_MAX_TOKENS_FALLBACK = 8192


async def anthropic_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    model: str,
    max_tokens: int | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    reasoning_effort: str | None = None,
    on_reasoning_chunk=None,
    on_content_chunk=None,
    on_tool_call_chunk=None, on_response_started=None,
    retry_transient_errors: bool = True,
) -> "LLMResponse":
    """Call Anthropic with prompt caching, thinking, streaming, and retries."""
    if max_tokens is None:
        from llm.capabilities import get_model_max_output_tokens

        max_tokens = get_model_max_output_tokens(
            model, fallback=_REQUIRED_MAX_TOKENS_FALLBACK
        )
    max_tokens = max(1, max_tokens)

    cached_messages, cached_tools = _apply_cache_control(messages, tools)
    system_param, sdk_messages = _translate_messages_for_anthropic_sdk(cached_messages)
    sdk_tools = _translate_tools_for_anthropic_sdk(cached_tools)

    base_model = _strip_anthropic_prefix(model)

    thinking, output_config = _adaptive_thinking_kwargs(model, reasoning_effort)
    if thinking is None and _thinking_active(reasoning_effort) and not _model_uses_adaptive_thinking(model):
        budget = _legacy_thinking_budget(reasoning_effort)
        if budget is not None:
            thinking = {"type": "enabled", "budget_tokens": budget}

    create_kwargs: dict[str, Any] = {
        "model": base_model,
        "max_tokens": max_tokens,
        "messages": sdk_messages,
    }
    if system_param is not None:
        create_kwargs["system"] = system_param
    if sdk_tools:
        create_kwargs["tools"] = sdk_tools
    if thinking is not None:
        create_kwargs["thinking"] = thinking
    if output_config is not None:
        create_kwargs["output_config"] = output_config

    use_streaming = (
        on_reasoning_chunk is not None
        or on_content_chunk is not None
        or on_tool_call_chunk is not None
    )

    client = _get_client(api_key, api_base)
    response = await _messages_with_retries(
        client, create_kwargs,
        model=model, messages=messages, tools=tools, max_tokens=max_tokens,
        use_streaming=use_streaming,
        on_reasoning_chunk=on_reasoning_chunk,
        on_content_chunk=on_content_chunk,
        on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        retry_transient_errors=retry_transient_errors,
    )
    # Adaptive models carry the mapped level; a budget model carries the level
    # that produced its budget; nothing in the request means nothing was sent.
    response.effort_sent = (
        (output_config or {}).get("effort")
        or (reasoning_effort.strip().lower() if "thinking" in create_kwargs else None)
    )
    return response


async def _messages_with_retries(
    client: "anthropic.AsyncAnthropic",
    create_kwargs: dict[str, Any],
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    use_streaming: bool,
    on_reasoning_chunk,
    on_content_chunk,
    on_tool_call_chunk, on_response_started,
    retry_transient_errors: bool,
) -> "LLMResponse":
    """Dispatch with transient retries and the I7 overflow discovery re-entry."""
    from llm.router import _is_transient_error, provider_error_response

    transient_attempts = _MAX_RETRIES if retry_transient_errors else 1
    attempt = 0
    overflow_retried = False
    while True:
        try:
            if use_streaming:
                return await _streaming_completion(
                    client, create_kwargs, model=model,
                    on_reasoning_chunk=on_reasoning_chunk,
                    on_content_chunk=on_content_chunk,
                    on_tool_call_chunk=on_tool_call_chunk,
                    on_response_started=on_response_started,
                )
            response = await client.messages.create(**create_kwargs)
            return _parse_response(response, model=model)
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
            if (
                _is_transient_anthropic_error(e) or _is_transient_error(e)
            ) and attempt < transient_attempts:
                wait = _RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "Anthropic SDK call failed (attempt {}/{}), retrying in {}s: {}",
                    attempt, transient_attempts, wait, e,
                )
                await asyncio.sleep(wait)
                continue
            logger.error(
                "Anthropic SDK call failed (attempt {}/{}): {}",
                attempt, transient_attempts, e,
            )
            return provider_error_response(e, f"Error calling LLM: {_humanize_error(e, model)}")


_CLIENTS: dict[tuple[str | None, str | None], "anthropic.AsyncAnthropic"] = {}


def _get_client(api_key: str | None, api_base: str | None) -> "anthropic.AsyncAnthropic":
    """Return a cached AsyncAnthropic client for the given auth/base pair."""
    key = (api_key, api_base)
    client = _CLIENTS.get(key)
    if client is None:
        kwargs: dict[str, Any] = {"max_retries": _SDK_MAX_RETRIES}
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["base_url"] = api_base
        client = anthropic.AsyncAnthropic(**kwargs)
        _CLIENTS[key] = client
    return client


def _is_transient_anthropic_error(e: Exception) -> bool:
    """Return whether an Anthropic SDK error is worth retrying."""
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(e, anthropic.RateLimitError):
        return True
    if isinstance(e, anthropic.APIStatusError):
        status = getattr(e, "status_code", 0) or 0
        return status >= 500
    return False


def _parse_response(response: Any, *, model: str) -> "LLMResponse":
    """Translate an Anthropic SDK ``Message`` into an ``LLMResponse``.

    Maps ``stop_reason`` to the OpenAI-flavoured ``finish_reason`` strings
    callers already handle (``stop``/``length``/``tool_calls``).
    """
    from llm.router import LLMResponse, ToolCallRequest

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_signatures: dict[str, None] = {}
    reasoning_blocks: list[dict[str, str]] = []
    tool_calls: list[ToolCallRequest] = []

    for block in (getattr(response, "content", None) or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            t = getattr(block, "text", "") or ""
            if t:
                text_parts.append(t)
        elif btype == "thinking":
            t = getattr(block, "thinking", "") or ""
            if t:
                reasoning_parts.append(t)
            signature = getattr(block, "signature", "") or ""
            if t or signature:
                native_block = {"type": "thinking", "thinking": t}
                if signature:
                    native_block["signature"] = signature
                    reasoning_signatures.setdefault(signature, None)
                reasoning_blocks.append(native_block)
        elif btype == "redacted_thinking":
            data = getattr(block, "data", "") or ""
            if data:
                reasoning_blocks.append({
                    "type": "redacted_thinking",
                    "data": data,
                })
        elif btype == "tool_use":
            args = getattr(block, "input", None)
            if isinstance(args, str):
                args = json_repair.loads(args) if args.strip() else {}
            elif args is None:
                args = {}
            tool_calls.append(ToolCallRequest(
                id=getattr(block, "id", "") or "",
                name=getattr(block, "name", "") or "",
                arguments=args if isinstance(args, dict) else {},
            ))

    finish_reason = _map_stop_reason(getattr(response, "stop_reason", None))
    usage = _annotate_usage(_parse_usage(getattr(response, "usage", None)), model)
    reasoning_content = "".join(reasoning_parts) or None
    reasoning_provenance: dict[str, Any] | None = None
    reported_model = (
        str(response.model).strip()
        if isinstance(getattr(response, "model", None), str)
        and str(response.model).strip()
        else None
    )
    if reasoning_blocks or reasoning_content is not None:
        reasoning_provenance = {"representation_kind": "anthropic_thinking"}
        if reported_model is not None:
            reasoning_provenance["source_effective_model"] = reported_model
        native_signatures = list(reasoning_signatures)
        if native_signatures:
            reasoning_provenance["native_signatures"] = native_signatures
            if len(native_signatures) == 1:
                reasoning_provenance["native_signature"] = native_signatures[0]
        if reasoning_blocks:
            reasoning_provenance["native_blocks"] = reasoning_blocks

    return LLMResponse(
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning_content,
        reasoning_provenance=reasoning_provenance,
        reported_model=reported_model,
    )
