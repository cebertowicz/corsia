"""One disposable Native fork invocation over a warm or cold prefix."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from history.types import HistoryBoundary
from subagents.provider_context_limits import is_provider_context_limit

from core.fork_native_tools import (
    MeasuredInput,
    assistant_message,
    await_or_cancel as _await_or_cancel,
    input_fits_off_loop as _input_fits,
    input_room,
    measured_input,
    run_tool_calls as _run_tool_calls,
)
from core.fork_pressure import soft_pressure_remaining
from core.fork_runtime import ForkKind, ForkStepLimitError, ForkStepLimitReached
from core.fork_failures import ForkCredentialsUnavailable
from core.fork_types import (
    ProviderForkRejected,
    WarmForkUnusable,
    ProviderForkRequest,
    ProviderForkResult,
)
from core.fork_types import (
    FORK_CONTEXT_TRUNCATED,
    FORK_TOOL_ROUND_TRIP_TOKENS,
    ColdInputCallback,
)

# A small window gives a Native fork a fifth of itself for output: cold fit
# reserves the allowance twice (reply, then its round trip), and a fixed 16,384
# left a 41,681-token local seat under 7k of input, so it never folded. A fifth
# still covers the largest local checkpoint call measured (7,336 tokens).
SMALL_WINDOW_OUTPUT_DIVISOR = 5

COLD_CLOSING_TURN = {
    "role": "user",
    "content": "Carry out the system instruction now, using the conversation above as its source.",
}


def _window_output(request: ProviderForkRequest) -> ProviderForkRequest:
    """Bound the fork's output allowance by its writer's window."""
    window = request.target.context_window
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        return request
    allowance = min(request.max_output_tokens, window // SMALL_WINDOW_OUTPUT_DIVISOR)
    if request.carried_tokens > 0:
        # Half the room between what the parent carries (fixed prompt and tool
        # definitions) and the launch point; the other half is left for turns
        # before the chat folds again. No room to halve leaves the allowance as
        # it was; a zero allowance cannot run.
        launch = window - soft_pressure_remaining(window)
        half_room = (launch - request.carried_tokens) // 2
        if half_room > 0:
            allowance = min(allowance, half_room)
    if allowance == request.max_output_tokens:
        return request
    return replace(request, max_output_tokens=allowance)


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
    request: ProviderForkRequest, tools: list[dict[str, Any]],
) -> Any:
    """Count the tool surface and fit the cold input; blocking, run off the loop."""
    from core.fork_cold_count import cold_input_tokens
    from core.fork_render import fit_cold_fork_input

    return fit_cold_fork_input(
        request,
        provider_surface_tokens=cold_input_tokens(tools, request.target),
        round_trip_tokens=request.max_output_tokens,
    )


async def _messages_and_tools(
    request: ProviderForkRequest,
    agent: Any,
    kind: ForkKind,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    source = request.warm_source
    if source is not None:
        if not isinstance(source, NativeWarmSource):
            raise TypeError(f"Native {kind.name} received another harness's warm source")
        messages = [dict(message) for message in source.prefix_messages]
        messages.append({"role": "user", "content": request.instruction})
        return messages, [dict(tool) for tool in source.tool_definitions], None
    # A cold fork has no live prefix to keep byte-stable for provider caching
    # (unlike the warm branch above), so it can advertise only the one tool
    # its loop will ever execute. Offering the full catalog only invites a
    # call the loop can just refuse (#2116): that round trip bills output
    # tokens for the refusal instead of narrative text, and can exhaust a
    # tightly-fit cold input before any narrative is produced.
    from tools.catalog import fork_tool_definitions

    definitions = fork_tool_definitions(agent.tools, kind.tool_allowlist)
    tools = [
        tool for tool in definitions
        if tool.get("function", {}).get("name") in kind.tool_allowlist
    ]
    fitted = await asyncio.to_thread(_fit_cold_input, request, tools)
    return cold_messages(request.instruction, fitted.messages), tools, fitted.first_included_seq


def cold_messages(
    instruction: str, source: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Frame a cold source so the fork's next turn answers the instruction.

    The source quotes history in its own roles. One that ends on an assistant
    reply (a manual, idle or model-switch checkpoint) left the system text as
    the last instruction, and a model echoed its frozen continuity index
    instead of writing; the warm branch already closes with a user turn.
    """
    from history.adapters import merge_checkpoint_source_blocks

    return [
        {"role": "system", "content": instruction},
        *merge_checkpoint_source_blocks([*source, COLD_CLOSING_TURN]),
    ]


async def warm_input_fits(
    request: ProviderForkRequest, kind: ForkKind,
) -> bool:
    """Whether a warm fork's first input fits, checked before it is admitted.

    It keeps one tool round trip of room: the fork's tool-call reply joins its
    input before the call that writes the narrative.
    """
    request = _window_output(request)
    messages, tools, _first_included_seq = await _messages_and_tools(request, None, kind)
    return await _input_fits(request, messages, tools, FORK_TOOL_ROUND_TRIP_TOKENS)


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
    measured=None,
) -> ProviderForkResult:
    """A partial body keeps the exact truncation marker; an empty one fails, named.

    A fold with no narrative has nothing to install, so it never returns as a
    truncated success that capture can only reject as "produced no content"
    with the path and its numbers dropped (#2514, #2116).
    """
    if bodies:
        return ProviderForkResult(
            "\n".join(bodies), {"calls": usage_calls}, FORK_CONTEXT_TRUNCATED,
        )
    room = await asyncio.to_thread(input_room, request, messages, tools, 0, measured)
    raise ProviderForkRejected(
        f"{FORK_CONTEXT_TRUNCATED}: path={path} iteration={iteration} "
        f"counted={room.counted} bound={room.bound} unusable={room.unusable} "
        f"finish_reason={getattr(response, 'finish_reason', None)} "
        f"error_kind={getattr(response, 'error_kind', None)}"
    )


def _initial_input_refusal(request: ProviderForkRequest, kind: ForkKind) -> ProviderForkRejected:
    """Refuse an initial overflow; only a warm fork can be corrected to cold.

    A fitted cold input that still does not fit is terminal.
    """
    refusal = WarmForkUnusable if request.warm_source is not None else ProviderForkRejected
    return refusal(f"Native {kind.name} initial input exceeds selected context window")


def _provider_failure(response, kind: ForkKind) -> None:
    detail = response.content or f"Native {kind.name} provider call failed"
    if response.endpoint_failure == "auth":
        raise ForkCredentialsUnavailable(detail)
    kind.provider_failure("native", detail)


async def _measured_after(
    request: ProviderForkRequest,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    response: Any,
    usage: dict[str, Any],
) -> MeasuredInput | None:
    """Count the next call's input from the provider's reading of this one."""
    return await asyncio.to_thread(
        measured_input, request, [*messages, assistant_message(response)], tools, usage,
    )


def _reply_body(response: Any) -> str | None:
    """Return the reply's narrative, falling back to its reasoning."""
    for body in (response.content, response.reasoning_content):
        if isinstance(body, str) and body.strip():
            return body
    return None


async def _final_reply(
    response, bodies, usage_calls, iteration, request, messages, tools, measured, *, kind,
) -> ProviderForkResult:
    """End the fold on a reply without tool calls; one cut off at its output cap is truncated."""
    body = _reply_body(response)
    if response.finish_reason == "length":
        return await _truncated_result(
            [*bodies, body] if body is not None else bodies, usage_calls, "iv_output_cap",
            iteration, request, messages, tools, response, measured,
        )
    if body is None:
        raise ProviderForkRejected(f"Native {kind.name} produced no body")
    return ProviderForkResult(body, {"calls": usage_calls})


async def invoke_fork(
    request: ProviderForkRequest,
    cancel_event: asyncio.Event,
    *,
    kind: ForkKind,
    on_cold_input: ColdInputCallback | None = None,
) -> ProviderForkResult:
    """Run one disposable fork through the Native tool loop."""
    if request.target.harness != "native":
        raise TypeError("Native adapter requires a Native target")
    request = _window_output(request)
    agent = await _get_agent()
    messages, tools, first_included_seq = await _messages_and_tools(request, agent, kind)
    if request.warm_source is None and on_cold_input is not None:
        await on_cold_input(first_included_seq)
    if cancel_event.is_set():
        raise asyncio.CancelledError
    provider = _provider_params(request.target.transport_model)
    model = str(provider.get("model") or request.target.transport_model)
    usage_calls: list[dict[str, Any]] = []
    response: Any = None
    partial_bodies: list[str] = []
    measured = None

    from infra import state

    step_limit = kind.step_limit("native", agent)
    if step_limit is None:
        raise ForkStepLimitError(f"fork kind {kind.name!r} has no Native step limit")
    thread_token = state._current_thread_id.set(request.thread_id)
    try:
        for _iteration in range(step_limit):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            first_reserve = FORK_TOOL_ROUND_TRIP_TOKENS if _iteration == 0 else 0
            if not await _input_fits(request, messages, tools, first_reserve, measured):
                if _iteration == 0:
                    raise _initial_input_refusal(request, kind)
                return await _truncated_result(
                    partial_bodies, usage_calls, "i_loop_input_over_bound", _iteration,
                    request, messages, tools, response, measured,
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
                _provider_failure(response, kind)
            if response.has_tool_calls:
                if isinstance(response.content, str) and response.content.strip():
                    partial_bodies.append(response.content)
                measured = await _measured_after(request, messages, tools, response, usage_calls[-1])
                fits = await _run_tool_calls(
                    agent, response, messages, cancel_event, request, tools,
                    kind=kind, measured=measured,
                )
                if not fits:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError
                    return await _truncated_result(
                        partial_bodies, usage_calls, "iii_tool_results_over_bound",
                        _iteration, request, messages, tools, response, measured,
                    )
                continue
            return await _final_reply(
                response, partial_bodies, usage_calls, _iteration, request, messages, tools,
                measured, kind=kind,
            )
        raise ForkStepLimitReached(f"{kind.name} reached its Native step limit")
    finally:
        state._current_thread_id.reset(thread_token)
