"""Streaming dispatcher for OpenAI Responses."""

from __future__ import annotations

from typing import Any

import json_repair
from openai import AsyncOpenAI
from llm.base_streaming import _notify_response_started
from llm.stream_idle import ProviderStreamStalled, ProviderStreamTruncated, await_with_idle_bound, bounded_stream, cut_off_response, output_reached_turn


_OUTPUT_EVENTS = frozenset({
    "response.output_item.done", "response.completed", "response.incomplete",
    "response.failed", "error",
})

#: The events that assemble one function call: its header, its arguments, its end.
_TOOL_CALL_EVENTS = frozenset({
    "response.output_item.added",
    "response.function_call_arguments.delta",
    "response.output_item.done",
})


def _is_output_event(event: Any) -> bool:
    """Status and keepalive events (created, in_progress, ...) are not progress."""
    etype = getattr(event, "type", None) or ""
    return etype.endswith(".delta") or etype in _OUTPUT_EVENTS


async def _streaming_responses_completion(
    client: AsyncOpenAI,
    kwargs: dict[str, Any],
    *,
    model: str,
    on_reasoning_chunk,
    on_content_chunk,
    on_tool_call_chunk=None, on_response_started=None,
) -> "LLMResponse":
    """Stream a Responses API call and build an ``LLMResponse``."""
    from llm.router import LLMResponse, ToolCallRequest

    response_iter = await await_with_idle_bound(
        client.responses.create(**{**kwargs, "stream": True}),
        "waiting for the provider to accept the request",
    )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_references: dict[str, None] = {}
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    _emitted_tool_indices: set[int] = set()
    finish_reason = "stop"
    final_response: Any = None
    async with bounded_stream(response_iter, is_progress=_is_output_event) as events:
        try:
            await _notify_response_started(on_response_started)
            async for event in events:
                etype = getattr(event, "type", None) or ""

                if etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        text_parts.append(delta)
                        if on_content_chunk:
                            await on_content_chunk(delta)

                elif etype == "response.reasoning_summary_text.delta":
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        reasoning_parts.append(delta)
                        reference = getattr(event, "item_id", "") or ""
                        if reference:
                            reasoning_references.setdefault(reference, None)
                        if on_reasoning_chunk:
                            await on_reasoning_chunk(delta)

                elif etype in _TOOL_CALL_EVENTS:
                    await _consume_tool_call_event(
                        event, etype, reasoning_references,
                        tool_calls_by_index, _emitted_tool_indices, on_tool_call_chunk,
                    )

                elif etype in ("response.completed", "response.incomplete"):
                    final_response = getattr(event, "response", None)
                    if etype == "response.incomplete":
                        finish_reason = "length"

                elif etype == "response.failed":
                    err_resp = getattr(event, "response", None)
                    err = getattr(err_resp, "error", None) if err_resp else None
                    raise RuntimeError(f"Responses API stream failed: {err}")

                elif etype == "error":
                    raise RuntimeError(f"Responses API stream error: {event}")
        except ProviderStreamStalled as stalled:
            # After completed/incomplete: just end. After output: terminal. Else retry.
            if final_response is None:
                if not output_reached_turn(text_parts, reasoning_parts, _emitted_tool_indices,
                                           on_content_chunk, on_reasoning_chunk):
                    raise
                return cut_off_response(stalled, model)
        if final_response is None:
            # The stream ended with no completed/incomplete event (#2485): the
            # same split as a stall, on the state #2422 already tracks.
            if not output_reached_turn(text_parts, reasoning_parts, _emitted_tool_indices,
                                       on_content_chunk, on_reasoning_chunk):
                raise ProviderStreamTruncated()
            return cut_off_response(ProviderStreamTruncated(), model)

    return _build_responses_result(
        LLMResponse, ToolCallRequest,
        text_parts=text_parts, reasoning_parts=reasoning_parts,
        reasoning_references=reasoning_references,
        tool_calls_by_index=tool_calls_by_index,
        finish_reason=finish_reason, final_response=final_response, model=model,
    )


def _track_output_item_added(
    event: Any, reasoning_references: dict[str, None],
    tool_calls_by_index: dict[int, dict[str, Any]],
) -> None:
    item = getattr(event, "item", None)
    item_type = getattr(item, "type", None) if item is not None else None
    if item_type == "reasoning":
        reference = getattr(item, "id", "") or ""
        if reference:
            reasoning_references.setdefault(reference, None)
    elif item_type == "function_call":
        idx = getattr(event, "output_index", 0) or 0
        tool_calls_by_index.setdefault(idx, {
            "id": getattr(item, "call_id", "") or getattr(item, "id", "") or "",
            "name": getattr(item, "name", "") or "",
            "arguments": getattr(item, "arguments", "") or "",
        })


async def _consume_tool_call_event(
    event: Any, etype: str, reasoning_references: dict[str, None],
    tool_calls_by_index: dict[int, dict[str, Any]], emitted_tool_indices: set[int],
    on_tool_call_chunk: Any,
) -> None:
    """Fold one of ``_TOOL_CALL_EVENTS`` into the assembled function calls."""
    if etype == "response.output_item.added":
        _track_output_item_added(event, reasoning_references, tool_calls_by_index)
        return
    if etype == "response.output_item.done":
        await _finish_output_item(
            event, tool_calls_by_index, emitted_tool_indices, on_tool_call_chunk,
        )
        return

    # The remaining event is response.function_call_arguments.delta.
    idx = getattr(event, "output_index", 0) or 0
    delta = getattr(event, "delta", "") or ""
    tc = tool_calls_by_index.setdefault(idx, {
        "id": getattr(event, "item_id", "") or "",
        "name": "",
        "arguments": "",
    })
    tc["arguments"] = tc["arguments"] + delta

    if on_tool_call_chunk and idx not in emitted_tool_indices:
        await on_tool_call_chunk(idx, tc["id"], tc["name"], tc["arguments"])
        emitted_tool_indices.add(idx)


async def _finish_output_item(
    event: Any, tool_calls_by_index: dict[int, dict[str, Any]],
    emitted_tool_indices: set[int], on_tool_call_chunk: Any,
) -> None:
    item = getattr(event, "item", None)
    if item is not None and getattr(item, "type", None) == "function_call":
        idx = getattr(event, "output_index", 0) or 0
        tc = tool_calls_by_index.setdefault(
            idx, {"id": "", "name": "", "arguments": ""},
        )
        call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
        if call_id:
            tc["id"] = call_id
        if getattr(item, "name", None):
            tc["name"] = item.name
        final_args = getattr(item, "arguments", None)
        if isinstance(final_args, str) and final_args and not tc["arguments"]:
            tc["arguments"] = final_args

        if on_tool_call_chunk and idx not in emitted_tool_indices:
            await on_tool_call_chunk(idx, tc["id"], tc["name"], tc["arguments"])
            emitted_tool_indices.add(idx)


def _build_responses_result(
    llm_response_cls, tool_call_request_cls, *,
    text_parts: list[str], reasoning_parts: list[str],
    reasoning_references: dict[str, None],
    tool_calls_by_index: dict[int, dict[str, Any]],
    finish_reason: str, final_response: Any, model: str,
):
    """Fold accumulated stream state into the final ``LLMResponse``."""
    text = "".join(text_parts) or None
    reasoning = "".join(reasoning_parts) or None

    tool_calls = []
    for idx in sorted(tool_calls_by_index):
        tc = tool_calls_by_index[idx]
        args = tc["arguments"]
        if isinstance(args, str):
            args = json_repair.loads(args) if args.strip() else {}
        tool_calls.append(tool_call_request_cls(id=tc["id"], name=tc["name"], arguments=args))

    usage: dict[str, Any] = {}
    if final_response is not None:
        if finish_reason == "stop":
            status = getattr(final_response, "status", "completed")
            if status == "incomplete":
                finish_reason = "length"
        u = getattr(final_response, "usage", None)
        if u is not None:
            from llm.providers.openai_translation import _parse_responses_usage

            usage = _parse_responses_usage(u)

    cost_usd: float | None = None
    try:
        from llm.pricing import calculate_cost
        cost_usd = calculate_cost(model, usage)
    except Exception as e:
        from loguru import logger
        logger.warning(f"openai-streaming cost calc failed for {model}: {e}")
    usage["cost_usd"] = cost_usd
    usage["model"] = model
    reasoning_provenance: dict[str, Any] | None = None
    if reasoning is not None:
        reasoning_provenance = {"representation_kind": "opaque"}
        native_references = list(reasoning_references)
        if native_references:
            reasoning_provenance["native_references"] = native_references
            if len(native_references) == 1:
                reasoning_provenance["native_reference"] = native_references[0]

    return llm_response_cls(
        content=text,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning,
        reasoning_provenance=reasoning_provenance,
    )
