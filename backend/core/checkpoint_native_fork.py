"""One disposable Native checkpoint invocation over a warm or cold prefix."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from history.types import HistoryBoundary
from subagents.provider_context_limits import is_provider_context_limit

from core.checkpoint_native_tools import (
    CHECKPOINT_TOOL_NAME,
    await_or_cancel as _await_or_cancel,
    input_fits_off_loop as _input_fits,
    input_room,
    run_tool_calls as _run_tool_calls,
)
from core.checkpoint_types import (
    CHECKPOINT_CONTEXT_TRUNCATED,
    CHECKPOINT_TOOL_ROUND_TRIP_TOKENS,
    ColdInputCallback,
    ProviderCheckpointRejected,
    WarmForkUnusable,
    ProviderCheckpointRequest,
    ProviderCheckpointResult,
)


@dataclass(frozen=True, slots=True)
class NativeWarmSource:
    """The exact message/tool prefix an ordinary Native send would use."""

    thread_id: str
    boundary: HistoryBoundary
    prefix_messages: tuple[dict[str, Any], ...]
    tool_definitions: tuple[dict[str, Any], ...]
    prefix_sha256: str
    provider_input_tokens: int
    rendered_input_tokens: int


def capture_live_warm_source(
    thread_id: str,
    boundary: HistoryBoundary,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    reasoning_effort: str | None,
    preserve_thinking: bool | None,
    provider_input_tokens: int,
    rendered_input_tokens: int,
) -> NativeWarmSource:
    """Freeze the exact live Native prefix already accepted by the provider."""
    frozen_messages = tuple(deepcopy(message) for message in messages)
    frozen_tools = tuple(deepcopy(tool) for tool in tools)
    if (
        isinstance(provider_input_tokens, bool)
        or not isinstance(provider_input_tokens, int)
        or provider_input_tokens < 0
        or isinstance(rendered_input_tokens, bool)
        or not isinstance(rendered_input_tokens, int)
        or rendered_input_tokens < 0
    ):
        raise ValueError("Native warm source requires a nonnegative occupancy anchor")
    return NativeWarmSource(
        thread_id=thread_id,
        boundary=boundary,
        prefix_messages=frozen_messages,
        tool_definitions=frozen_tools,
        prefix_sha256=_prefix_sha256(
            frozen_messages,
            frozen_tools,
            reasoning_effort,
            preserve_thinking,
        ),
        provider_input_tokens=provider_input_tokens,
        rendered_input_tokens=rendered_input_tokens,
    )


def _prefix_sha256(
    messages: tuple[dict[str, Any], ...],
    tools: tuple[dict[str, Any], ...],
    reasoning_effort: str | None,
    preserve_thinking: bool | None,
) -> str:
    payload = json.dumps(
        {
            "messages": messages,
            "tools": tools,
            "reasoning_effort": reasoning_effort or "",
            "preserve_thinking": preserve_thinking,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _get_agent():
    from core.agent import get_agent

    return await get_agent()


async def _chat_completion(**kwargs):
    from llm import chat_completion

    return await chat_completion(**kwargs)


def _provider_params(model: str) -> dict[str, Any]:
    from llm.capabilities import make_provider_params

    return make_provider_params(model)


def _fit_cold_input(
    request: ProviderCheckpointRequest, tools: list[dict[str, Any]],
) -> Any:
    """Count the tool surface and fit the cold input; blocking, run off the loop."""
    from core.checkpoint_cold_count import cold_input_tokens
    from core.checkpoint_render import fit_cold_checkpoint_input

    return fit_cold_checkpoint_input(
        request,
        provider_surface_tokens=cold_input_tokens(tools, request.target),
        round_trip_tokens=request.max_output_tokens,
    )


async def _messages_and_tools(
    request: ProviderCheckpointRequest,
    agent: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    source = request.warm_source
    if source is not None:
        if not isinstance(source, NativeWarmSource):
            raise TypeError("Native checkpoint received another harness's warm source")
        messages = [dict(message) for message in source.prefix_messages]
        messages.append({"role": "user", "content": request.instruction})
        return messages, [dict(tool) for tool in source.tool_definitions], None
    from tools.catalog import advertised_tool_definitions

    # A cold fork has no live prefix to keep byte-stable for provider caching
    # (unlike the warm branch above), so it can advertise only the one tool
    # its loop will ever execute. Offering the full catalog only invites a
    # call the loop can just refuse (#2116): that round trip bills output
    # tokens for the refusal instead of narrative text, and can exhaust a
    # tightly-fit cold input before any narrative is produced.
    tools = [
        tool for tool in advertised_tool_definitions(agent.tools)
        if tool.get("function", {}).get("name") == CHECKPOINT_TOOL_NAME
    ]
    fitted = await asyncio.to_thread(_fit_cold_input, request, tools)
    return (
        [
            {"role": "system", "content": request.instruction},
            *(dict(message) for message in fitted.messages),
        ],
        tools,
        fitted.first_included_seq,
    )


async def warm_input_fits(request: ProviderCheckpointRequest) -> bool:
    """Whether a warm fork's first input fits, checked before it is admitted.

    It keeps one tool round trip of room: the fork's tool-call reply joins its
    input before the call that writes the narrative.
    """
    messages, tools, _first_included_seq = await _messages_and_tools(request, None)
    return await _input_fits(request, messages, tools, CHECKPOINT_TOOL_ROUND_TRIP_TOKENS)


async def _complete(request, messages, tools, model, provider, cancel_event):
    return await _await_or_cancel(
        _chat_completion(
            messages=[dict(message) for message in messages],
            tools=[dict(tool) for tool in tools],
            model=model,
            max_tokens=request.max_output_tokens,
            api_key=provider.get("api_key"),
            api_base=provider.get("api_base"),
            omlx_server_id=provider.get("omlx_server_id"),
            on_reasoning_chunk=None,
            on_content_chunk=None,
            on_tool_call_chunk=None,
            reasoning_effort=request.reasoning_effort,
            reasoning_levels=provider.get("reasoning_levels"),
            thread_id=None,
            preserve_thinking=request.target.preserve_thinking_default,
            retry_transient_errors=False,
        ),
        cancel_event,
    )


async def _truncated_result(
    bodies, usage_calls, path, iteration, request, messages, tools, response,
) -> ProviderCheckpointResult:
    """A partial body keeps the exact truncation marker; an empty one fails, named.

    A fold with no narrative has nothing to install, so it never returns as a
    truncated success that capture can only reject as "produced no content"
    with the path and its numbers dropped (#2514, #2116).
    """
    if bodies:
        return ProviderCheckpointResult(
            "\n".join(bodies), {"calls": usage_calls}, CHECKPOINT_CONTEXT_TRUNCATED,
        )
    room = await asyncio.to_thread(input_room, request, messages, tools)
    raise ProviderCheckpointRejected(
        f"{CHECKPOINT_CONTEXT_TRUNCATED}: path={path} iteration={iteration} "
        f"counted={room.counted} bound={room.bound} unusable={room.unusable} "
        f"finish_reason={getattr(response, 'finish_reason', None)} "
        f"error_kind={getattr(response, 'error_kind', None)}"
    )


def _initial_input_refusal(request: ProviderCheckpointRequest) -> ProviderCheckpointRejected:
    """Refuse an initial overflow; only a warm fork can be corrected to cold.

    A fitted cold input that still does not fit is terminal.
    """
    refusal = WarmForkUnusable if request.warm_source is not None else ProviderCheckpointRejected
    return refusal("Native checkpoint initial input exceeds selected context window")


async def invoke_checkpoint(
    request: ProviderCheckpointRequest,
    cancel_event: asyncio.Event,
    *,
    on_cold_input: ColdInputCallback | None = None,
) -> ProviderCheckpointResult:
    """Run one Native tool loop; captured text wins over terminal metadata."""
    if request.target.harness != "native":
        raise TypeError("Native adapter requires a Native target")
    agent = await _get_agent()
    messages, tools, first_included_seq = await _messages_and_tools(request, agent)
    if request.warm_source is None and on_cold_input is not None:
        await on_cold_input(first_included_seq)
    if cancel_event.is_set():
        raise asyncio.CancelledError
    provider = _provider_params(request.target.transport_model)
    model = str(provider.get("model") or request.target.transport_model)
    usage_calls: list[dict[str, Any]] = []
    response: Any = None
    partial_bodies: list[str] = []

    from infra.continuity_staging import checkpoint_execution_scope
    from infra import state

    thread_token = state._current_thread_id.set(request.thread_id)
    try:
        with checkpoint_execution_scope(request.checkpoint_id):
            for _iteration in range(agent.max_iterations):
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                first_reserve = CHECKPOINT_TOOL_ROUND_TRIP_TOKENS if _iteration == 0 else 0
                if not await _input_fits(request, messages, tools, first_reserve):
                    if _iteration == 0:
                        raise _initial_input_refusal(request)
                    return await _truncated_result(
                        partial_bodies, usage_calls, "i_loop_input_over_bound", _iteration,
                        request, messages, tools, response,
                    )
                response = await _complete(request, messages, tools, model, provider, cancel_event)
                usage_calls.append(dict(response.usage or {}))
                if request.on_usage is not None:
                    await request.on_usage(str(_iteration), model, usage_calls[-1])
                if response.finish_reason == "error":
                    if (response.error_kind == "provider_context_overflow"
                            or is_provider_context_limit(response.content)):
                        return await _truncated_result(
                            partial_bodies, usage_calls, "ii_provider_context_limit",
                            _iteration, request, messages, tools, response,
                        )
                    raise ProviderCheckpointRejected(
                        response.content or "Native checkpoint provider call failed"
                    )
                if response.has_tool_calls:
                    if isinstance(response.content, str) and response.content.strip():
                        partial_bodies.append(response.content)
                    fits = await _run_tool_calls(agent, response, messages, cancel_event, request, tools)
                    if not fits:
                        if cancel_event.is_set():
                            raise asyncio.CancelledError
                        return await _truncated_result(
                            partial_bodies, usage_calls, "iii_tool_results_over_bound",
                            _iteration, request, messages, tools, response,
                        )
                    continue
                body = response.content
                if not (isinstance(body, str) and body.strip()):
                    body = response.reasoning_content
                if isinstance(body, str) and body.strip():
                    return ProviderCheckpointResult(body, {"calls": usage_calls})
                break
    finally:
        state._current_thread_id.reset(thread_token)
    raise ProviderCheckpointRejected("Native checkpoint produced no body")
