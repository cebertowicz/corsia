"""Streaming response assembly for the generic LiteLLM transport."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

import json_repair
from loguru import logger

from llm.stream_idle import ProviderStreamStalled, ProviderStreamTruncated, await_with_idle_bound, bounded_stream, cut_off_response, output_reached_turn
from llm.tool_input_stream import abandoning_unreturned, forward_tool_input, forwarded_ids

if TYPE_CHECKING:
    from llm.router import LLMResponse, ToolCallRequest


def _carries_output(chunk: Any) -> bool:
    """True when a chunk holds model output, usage, or the finish marker."""
    if getattr(chunk, "usage", None):
        return True
    for choice in chunk.choices or []:
        delta = choice.delta
        if choice.finish_reason or (delta is not None and (
            delta.content or getattr(delta, "reasoning_content", None) or delta.tool_calls
        )):
            return True
    return False


async def _notify_response_started(callback: Any) -> None:
    if callback is not None:
        await callback()


def _provider_sent_finish_reason(response: Any, *, chunk_finished: bool) -> bool:
    """Whether the PROVIDER sent a finish marker, not one litellm synthesized.

    ``CustomStreamWrapper`` ends every stream with a final chunk built by
    ``finish_reason_handler`` (litellm 1.84.0,
    ``litellm_core_utils/streaming_handler.py:1814-1827``), reported at
    :2227-2230 when the upstream ends without one; its ``"stop"`` fallback
    (:1820) is synthesized, so a chunk-based reading is always true on a real
    litellm stream and cannot tell a completed answer from a cut-off one.

    ``received_finish_reason`` / ``intermittent_finish_reason`` (:129-130) are
    set only from upstream chunks — on the OpenAI dialect every chunk reports
    through ``handle_openai_chat_completion_chunk`` (:1408-1422) — so they are
    the provider's own marker. Transports without the wrapper keep the
    chunk-based reading.
    """
    if response is None or not hasattr(response, "received_finish_reason"):
        return chunk_finished
    return bool(
        getattr(response, "received_finish_reason", None)
        or getattr(response, "intermittent_finish_reason", None)
    )


class _StreamDrainResult(NamedTuple):
    first_delta_ns: int | None
    content_parts: list[str]
    reasoning_parts: list[str]
    tool_calls_by_idx: dict[int, dict]
    finish_reason: str
    usage: dict[str, Any]
    cut_off: ProviderStreamStalled | ProviderStreamTruncated | None


async def _drain_stream(
    response: Any, model: str, api_base: str | None,
    on_reasoning_chunk: Any, on_content_chunk: Any,
    on_tool_call_chunk: Any, on_response_started: Any,
    tool_calls_by_idx: dict[int, dict],
) -> _StreamDrainResult:
    """Consume the provider stream, dispatching deltas; it is closed on exit.

    A call the provider cut off before any delta is raised so the retry ladder
    can retry it; one cut off after output already reached the Turn is returned
    in ``cut_off``. Silence and an early end are the same decision (#2422,
    #2485), decided on the ``finished`` marker this loop already tracks.
    """
    first_delta_ns: int | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    emitted_tool_indices: set[int] = set()
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    cut_off: ProviderStreamStalled | ProviderStreamTruncated | None = None
    finished = False  # the provider sent its finish marker
    async with bounded_stream(response, is_progress=_carries_output) as chunks:
        try:
            await _notify_response_started(on_response_started)
            async for chunk in chunks:
                finished = finished or bool(chunk.choices and chunk.choices[0].finish_reason)
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue
                first_delta_ns = await _consume_delta(
                    delta,
                    content_parts,
                    reasoning_parts,
                    tool_calls_by_idx,
                    emitted_tool_indices,
                    on_reasoning_chunk,
                    on_content_chunk,
                    on_tool_call_chunk,
                    first_delta_ns,
                )

                finish_reason = chunk.choices[0].finish_reason or finish_reason
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = _usage_from_chunk(chunk.usage, model, api_base)
        except ProviderStreamStalled as error:
            if not finished:  # silence after the finish marker just ends the stream
                if not output_reached_turn(
                    content_parts, reasoning_parts, emitted_tool_indices,
                    on_content_chunk, on_reasoning_chunk,
                ):
                    raise
                cut_off = error
        finished = _provider_sent_finish_reason(response, chunk_finished=finished)
        if not finished and cut_off is None:
            # The stream ended with no provider finish marker (#2485).
            if not output_reached_turn(
                content_parts, reasoning_parts, emitted_tool_indices,
                on_content_chunk, on_reasoning_chunk,
            ):
                raise ProviderStreamTruncated()
            cut_off = ProviderStreamTruncated()
    return _StreamDrainResult(
        first_delta_ns, content_parts, reasoning_parts,
        tool_calls_by_idx, finish_reason, usage, cut_off,
    )


async def streaming_completion(
    kwargs: dict[str, Any],
    model: str,
    on_reasoning_chunk: Any,
    on_content_chunk: Any,
    on_tool_call_chunk: Any = None, on_response_started: Any = None,
    *,
    acompletion_fn: Callable[..., Any],
) -> "LLMResponse":
    """Stream one call; a call whose input streamed but was not returned is abandoned here, the one place that knows."""
    tool_calls_by_idx: dict[int, dict] = {}
    return await abandoning_unreturned(
        _streaming_attempt(kwargs, model, on_reasoning_chunk, on_content_chunk, on_tool_call_chunk,
                           on_response_started, acompletion_fn, tool_calls_by_idx),
        lambda: forwarded_ids(tool_calls_by_idx),
    )


async def _streaming_attempt(
    kwargs: dict[str, Any], model: str, on_reasoning_chunk: Any, on_content_chunk: Any,
    on_tool_call_chunk: Any, on_response_started: Any, acompletion_fn: Callable[..., Any],
    tool_calls_by_idx: dict[int, dict],
) -> "LLMResponse":
    from llm.router import LLMResponse

    kwargs["stream"] = True
    kwargs.setdefault("stream_options", {})["include_usage"] = True
    request_sent_ns = time.monotonic_ns()
    response = await await_with_idle_bound(
        acompletion_fn(**kwargs), "waiting for the provider to accept the request",
    )
    drained = await _drain_stream(
        response, model, kwargs.get("api_base"),
        on_reasoning_chunk, on_content_chunk, on_tool_call_chunk, on_response_started,
        tool_calls_by_idx,
    )

    if drained.cut_off is not None:
        return cut_off_response(drained.cut_off, model)

    tool_calls = _assemble_tool_calls(drained.tool_calls_by_idx)
    content = "".join(drained.content_parts) or None
    reasoning = "".join(drained.reasoning_parts) or None
    reasoning_provenance = (
        {"representation_kind": "reasoning_content"}
        if reasoning is not None
        else None
    )
    usage = drained.usage
    usage["cost_usd"] = _calculate_cost(model, usage)
    usage["model"] = model

    ttft_ms = None
    if drained.first_delta_ns is not None:
        ttft_ms = (drained.first_delta_ns - request_sent_ns) / 1_000_000.0

    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=drained.finish_reason or "stop",
        usage=usage,
        reasoning_content=reasoning,
        reasoning_provenance=reasoning_provenance,
        ttft_ms=ttft_ms,
    )


async def _consume_delta(
    delta,
    content_parts: list[str],
    reasoning_parts: list[str],
    tool_calls_by_idx: dict[int, dict],
    emitted_tool_indices: set[int],
    on_reasoning_chunk: Any,
    on_content_chunk: Any,
    on_tool_call_chunk: Any,
    first_delta_ns: int | None,
) -> int | None:
    rc = getattr(delta, "reasoning_content", None)
    if rc:
        first_delta_ns = first_delta_ns or time.monotonic_ns()
        reasoning_parts.append(rc)
        if on_reasoning_chunk:
            await on_reasoning_chunk(rc)

    if delta.content:
        first_delta_ns = first_delta_ns or time.monotonic_ns()
        content_parts.append(delta.content)
        if on_content_chunk:
            await on_content_chunk(delta.content)

    if delta.tool_calls:
        first_delta_ns = first_delta_ns or time.monotonic_ns()
        await _consume_tool_call_deltas(
            delta.tool_calls,
            tool_calls_by_idx,
            emitted_tool_indices,
            on_tool_call_chunk,
        )

    return first_delta_ns


async def _consume_tool_call_deltas(
    deltas,
    tool_calls_by_idx: dict[int, dict],
    emitted_tool_indices: set[int],
    on_tool_call_chunk: Any,
) -> None:
    for tc_delta in deltas:
        idx = tc_delta.index
        if idx not in tool_calls_by_idx:
            tool_calls_by_idx[idx] = {
                "id": tc_delta.id or "",
                "name": getattr(tc_delta.function, "name", None) or "",
                "arguments": "",
            }
        else:
            if tc_delta.id:
                tool_calls_by_idx[idx]["id"] = tc_delta.id
            if getattr(tc_delta.function, "name", None):
                tool_calls_by_idx[idx]["name"] = tc_delta.function.name
        if getattr(tc_delta.function, "arguments", None):
            tool_calls_by_idx[idx]["arguments"] += tc_delta.function.arguments
        forward_tool_input(tool_calls_by_idx[idx])

        if on_tool_call_chunk and idx not in emitted_tool_indices:
            tc = tool_calls_by_idx[idx]
            await on_tool_call_chunk(idx, tc["id"], tc["name"], tc["arguments"])
            emitted_tool_indices.add(idx)


def _usage_from_chunk(
    raw_usage, model: str, api_base: str | None = None,
) -> dict[str, Any]:
    """Normalize a streamed usage chunk into disjoint ledger counts.

    Shares infra.provider_usage with the non-streaming path so a turn is
    measured the same way whether or not it streamed -- including the cache
    convention, which depends on the endpoint the request reached.
    """
    from infra import provider_usage

    measured = provider_usage.from_litellm(
        raw_usage, model=model, api_base=api_base,
    )
    if measured is None:
        logger.warning(
            "llm_usage: {} streamed no usable token measurement; "
            "recording the call as unmeasured",
            model,
        )
        return provider_usage.unmeasured_ledger_row()
    return measured.ledger_row()


def _assemble_tool_calls(tool_calls_by_idx: dict[int, dict]) -> list["ToolCallRequest"]:
    from llm.router import ToolCallRequest

    tool_calls = []
    for idx in sorted(tool_calls_by_idx):
        tc = tool_calls_by_idx[idx]
        args = tc["arguments"]
        if isinstance(args, str):
            args = json_repair.loads(args) if args.strip() else {}
        tool_calls.append(ToolCallRequest(id=tc["id"], name=tc["name"], arguments=args))
    return tool_calls


def _calculate_cost(model: str, usage: dict[str, Any]) -> float | None:
    try:
        from llm.pricing import calculate_cost

        return calculate_cost(model, usage)
    except Exception as exc:
        logger.warning("cost calculation failed for {}: {}", model, exc)
        return None
