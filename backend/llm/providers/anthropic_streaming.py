"""Streaming path for the Anthropic SDK provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import json_repair

from llm.base_streaming import _notify_response_started
from llm.tool_input_stream import abandoning_unreturned, send_tool_input
from llm.stream_idle import ProviderStreamStalled, ProviderStreamTruncated, await_with_idle_bound, bounded_stream, cut_off_response, output_reached_turn
from llm.providers.anthropic_translation import (
    _annotate_usage,
    _map_stop_reason,
)

if TYPE_CHECKING:
    import anthropic

    from llm.router import LLMResponse


async def _streaming_completion(
    client: "anthropic.AsyncAnthropic",
    create_kwargs: dict[str, Any],
    *,
    model: str,
    on_reasoning_chunk,
    on_content_chunk,
    on_tool_call_chunk, on_response_started=None,
) -> "LLMResponse":
    """Stream a ``messages.create`` response and dispatch deltas.

    A call whose input this attempt streamed but did not return (it failed, was
    cut off, or will be retried) is abandoned here, the one place that knows.
    """
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    return await abandoning_unreturned(
        _stream_attempt(
            client, create_kwargs, tool_calls_by_index, model=model,
            on_reasoning_chunk=on_reasoning_chunk, on_content_chunk=on_content_chunk,
            on_tool_call_chunk=on_tool_call_chunk, on_response_started=on_response_started,
        ),
        lambda: [call["id"] for call in tool_calls_by_index.values()],
    )


async def _stream_attempt(
    client: "anthropic.AsyncAnthropic",
    create_kwargs: dict[str, Any],
    tool_calls_by_index: dict[int, dict[str, Any]],
    *,
    model: str,
    on_reasoning_chunk,
    on_content_chunk,
    on_tool_call_chunk, on_response_started,
) -> "LLMResponse":
    from llm.router import LLMResponse, ToolCallRequest

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    thinking_blocks_by_index: dict[int, dict[str, Any]] = {}
    redacted_blocks_by_index: dict[int, dict[str, str]] = {}
    _emitted_tool_indices: set[int] = set()
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    reported_model: str | None = None
    finished = False  # the provider sent its stop marker

    stream_kwargs = {**create_kwargs, "stream": True}
    stream = await await_with_idle_bound(
        client.messages.create(**stream_kwargs),
        "waiting for the provider to accept the request",
    )
    async with bounded_stream(
        stream, is_progress=lambda event: getattr(event, "type", None) != "ping",
    ) as events:
        try:
            await _notify_response_started(on_response_started)
            async for event in events:
                finish_reason, usage, reported_model = await _consume_stream_event(
                    event, text_parts=text_parts, reasoning_parts=reasoning_parts,
                    thinking_blocks_by_index=thinking_blocks_by_index,
                    redacted_blocks_by_index=redacted_blocks_by_index,
                    tool_calls_by_index=tool_calls_by_index,
                    emitted_tool_indices=_emitted_tool_indices,
                    finish_reason=finish_reason, usage=usage, reported_model=reported_model,
                    on_reasoning_chunk=on_reasoning_chunk, on_content_chunk=on_content_chunk,
                    on_tool_call_chunk=on_tool_call_chunk,
                )
                finished = finished or _is_stop_marker(event)
        except ProviderStreamStalled as stalled:
            # Silence after the stop marker just ends the stream. Otherwise
            # output already at the Turn ends it; before any, retry.
            if not finished:
                if not output_reached_turn(
                    text_parts, reasoning_parts, _emitted_tool_indices,
                    on_content_chunk, on_reasoning_chunk,
                ):
                    raise
                return cut_off_response(stalled, model)
        if not finished:
            # The stream ended without the provider's stop marker (#2485): the
            # same split as a stall, on the state #2422 already tracks.
            if not output_reached_turn(
                text_parts, reasoning_parts, _emitted_tool_indices,
                on_content_chunk, on_reasoning_chunk,
            ):
                raise ProviderStreamTruncated()
            return cut_off_response(ProviderStreamTruncated(), model)

    return _build_stream_response(
        LLMResponse, ToolCallRequest,
        text_parts=text_parts, reasoning_parts=reasoning_parts,
        thinking_blocks_by_index=thinking_blocks_by_index,
        redacted_blocks_by_index=redacted_blocks_by_index,
        tool_calls_by_index=tool_calls_by_index,
        finish_reason=finish_reason, usage=usage,
        reported_model=reported_model, model=model,
    )


def _is_stop_marker(event: Any) -> bool:
    """The event after which the provider has nothing left to say."""
    etype = getattr(event, "type", None)
    return etype == "message_stop" or (
        etype == "message_delta" and bool(getattr(getattr(event, "delta", None), "stop_reason", None))
    )


async def _consume_stream_event(
    event: Any, *,
    text_parts: list[str], reasoning_parts: list[str],
    thinking_blocks_by_index: dict[int, dict[str, Any]],
    redacted_blocks_by_index: dict[int, dict[str, str]],
    tool_calls_by_index: dict[int, dict[str, Any]],
    emitted_tool_indices: set[int],
    finish_reason: str, usage: dict[str, Any], reported_model: str | None,
    on_reasoning_chunk, on_content_chunk, on_tool_call_chunk,
) -> tuple[str, dict[str, Any], str | None]:
    """Fold one stream event into the accumulating state.

    Returns the (possibly updated) ``finish_reason``, ``usage``, and
    ``reported_model`` scalars; the dict/list/set arguments mutate in place.
    """
    etype = getattr(event, "type", None)

    if etype == "message_start":
        msg = getattr(event, "message", None)
        if msg is not None:
            usage = _merge_stream_usage(usage, getattr(msg, "usage", None))
            raw_model = getattr(msg, "model", None)
            if isinstance(raw_model, str) and raw_model.strip():
                reported_model = raw_model.strip()

    elif etype == "content_block_start":
        await _consume_content_block_start(
            event, thinking_blocks_by_index, redacted_blocks_by_index,
            tool_calls_by_index, emitted_tool_indices,
            reasoning_parts, on_reasoning_chunk, on_tool_call_chunk,
        )

    elif etype == "content_block_delta":
        await _consume_content_block_delta(
            event, text_parts, reasoning_parts, thinking_blocks_by_index,
            tool_calls_by_index, on_content_chunk, on_reasoning_chunk,
        )

    elif etype == "message_delta":
        delta = getattr(event, "delta", None)
        if delta is not None:
            sr = getattr(delta, "stop_reason", None)
            if sr:
                finish_reason = _map_stop_reason(sr)
        usage = _merge_stream_usage(usage, getattr(event, "usage", None))

    return finish_reason, usage, reported_model


async def _consume_content_block_start(
    event: Any,
    thinking_blocks_by_index: dict[int, dict[str, Any]],
    redacted_blocks_by_index: dict[int, dict[str, str]],
    tool_calls_by_index: dict[int, dict[str, Any]],
    emitted_tool_indices: set[int],
    reasoning_parts: list[str],
    on_reasoning_chunk, on_tool_call_chunk,
) -> None:
    """Handle a ``content_block_start`` event: thinking, redacted, or tool_use."""
    block = getattr(event, "content_block", None)
    idx = getattr(event, "index", 0) or 0
    block_type = getattr(block, "type", None) if block is not None else None
    if block_type == "thinking":
        state = thinking_blocks_by_index.setdefault(
            idx, {"thinking_parts": [], "signature_parts": []},
        )
        initial_thinking = getattr(block, "thinking", "") or ""
        if initial_thinking:
            state["thinking_parts"].append(initial_thinking)
            reasoning_parts.append(initial_thinking)
            if on_reasoning_chunk:
                await on_reasoning_chunk(initial_thinking)
        initial_signature = getattr(block, "signature", "") or ""
        if initial_signature:
            state["signature_parts"].append(initial_signature)
    elif block_type == "redacted_thinking":
        data = getattr(block, "data", "") or ""
        if data:
            redacted_blocks_by_index[idx] = {
                "type": "redacted_thinking",
                "data": data,
            }
    elif block_type == "tool_use":
        tc = tool_calls_by_index.setdefault(idx, {
            "id": getattr(block, "id", "") or "",
            "name": getattr(block, "name", "") or "",
            "arguments": "",
        })
        if not tc["id"]:
            tc["id"] = getattr(block, "id", "") or ""
        if not tc["name"]:
            tc["name"] = getattr(block, "name", "") or ""
        pre_input = getattr(block, "input", None)
        if isinstance(pre_input, dict) and pre_input:
            tc["arguments"] = json.dumps(pre_input)

        if on_tool_call_chunk and idx not in emitted_tool_indices:
            await on_tool_call_chunk(idx, tc["id"], tc["name"], tc["arguments"])
            emitted_tool_indices.add(idx)


async def _consume_content_block_delta(
    event: Any,
    text_parts: list[str], reasoning_parts: list[str],
    thinking_blocks_by_index: dict[int, dict[str, Any]],
    tool_calls_by_index: dict[int, dict[str, Any]],
    on_content_chunk, on_reasoning_chunk,
) -> None:
    """Handle a ``content_block_delta`` event: text, thinking, signature, or tool args."""
    delta = getattr(event, "delta", None)
    dtype = getattr(delta, "type", None) if delta is not None else None
    if dtype == "text_delta":
        t = getattr(delta, "text", "") or ""
        if t:
            text_parts.append(t)
            if on_content_chunk:
                await on_content_chunk(t)
    elif dtype == "thinking_delta":
        t = getattr(delta, "thinking", "") or ""
        if t:
            idx = getattr(event, "index", 0) or 0
            state = thinking_blocks_by_index.setdefault(
                idx, {"thinking_parts": [], "signature_parts": []},
            )
            state["thinking_parts"].append(t)
            reasoning_parts.append(t)
            if on_reasoning_chunk:
                await on_reasoning_chunk(t)
    elif dtype == "signature_delta":
        signature = getattr(delta, "signature", "") or ""
        if signature:
            idx = getattr(event, "index", 0) or 0
            state = thinking_blocks_by_index.setdefault(
                idx, {"thinking_parts": [], "signature_parts": []},
            )
            state["signature_parts"].append(signature)
    elif dtype == "input_json_delta":
        idx = getattr(event, "index", 0) or 0
        partial = getattr(delta, "partial_json", "") or ""
        tc = tool_calls_by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        tc["arguments"] = tc["arguments"] + partial
        if partial:
            send_tool_input(tc["id"], tc["name"], partial)


def _build_stream_response(
    llm_response_cls, tool_call_request_cls, *,
    text_parts: list[str], reasoning_parts: list[str],
    thinking_blocks_by_index: dict[int, dict[str, Any]],
    redacted_blocks_by_index: dict[int, dict[str, str]],
    tool_calls_by_index: dict[int, dict[str, Any]],
    finish_reason: str, usage: dict[str, Any],
    reported_model: str | None, model: str,
):
    """Fold accumulated stream state into the final ``LLMResponse``."""
    tool_calls = []
    for idx in sorted(tool_calls_by_index):
        tc = tool_calls_by_index[idx]
        args_str = tc["arguments"]
        if isinstance(args_str, str):
            args = json_repair.loads(args_str) if args_str.strip() else {}
        else:
            args = args_str or {}
        if not isinstance(args, dict):
            args = {}
        tool_calls.append(tool_call_request_cls(id=tc["id"], name=tc["name"], arguments=args))

    if tool_calls and finish_reason == "stop":
        finish_reason = "tool_calls"

    usage = _annotate_usage(usage, model)
    reasoning_content = "".join(reasoning_parts) or None
    reasoning_blocks: list[dict[str, str]] = []
    native_signatures: list[str] = []
    seen_signatures: set[str] = set()
    for idx in sorted(set(thinking_blocks_by_index) | set(redacted_blocks_by_index)):
        if idx in redacted_blocks_by_index:
            reasoning_blocks.append(redacted_blocks_by_index[idx])
            continue
        state = thinking_blocks_by_index[idx]
        thinking_text = "".join(state["thinking_parts"])
        signature = "".join(state["signature_parts"])
        block: dict[str, str] = {"type": "thinking", "thinking": thinking_text}
        if signature:
            block["signature"] = signature
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                native_signatures.append(signature)
        reasoning_blocks.append(block)
    reasoning_provenance: dict[str, Any] | None = None
    if reasoning_blocks or reasoning_content is not None:
        reasoning_provenance = {"representation_kind": "anthropic_thinking"}
        if reported_model is not None:
            reasoning_provenance["source_effective_model"] = reported_model
        if native_signatures:
            reasoning_provenance["native_signatures"] = native_signatures
            if len(native_signatures) == 1:
                reasoning_provenance["native_signature"] = native_signatures[0]
        if reasoning_blocks:
            reasoning_provenance["native_blocks"] = reasoning_blocks

    return llm_response_cls(
        content="".join(text_parts) or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning_content,
        reasoning_provenance=reasoning_provenance,
        reported_model=reported_model,
    )


def _merge_stream_usage(running: dict[str, Any], u: Any) -> dict[str, Any]:
    """Fold a per-event usage object into the running totals."""
    if u is None:
        return running
    from infra import provider_usage

    fields = {
        "input_tokens": "prompt_tokens", "output_tokens": "completion_tokens",
        "cache_creation_input_tokens": "cache_creation_tokens", "cache_read_input_tokens": "cache_read_tokens",
    }
    raw = {provider: running.get(ledger) for provider, ledger in fields.items()}
    for field in fields:
        value = getattr(u, field, None)
        if value is not None:
            raw[field] = value
    measured = provider_usage.from_anthropic(raw)
    return measured.ledger_row() if measured is not None else provider_usage.unmeasured_ledger_row()
