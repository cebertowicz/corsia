"""Private tool-loop mechanics for one disposable Native checkpoint."""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple

from core.fork_types import ProviderForkRequest
from core.fork_runtime import ForkKind
from tools.recorded_call import (
    CompletedToolCall,
    CompletedToolFailure,
    completed_tool_failure,
    enrich_completed_failure,
)

#: The only tool a Native checkpoint call may execute (#2350). A cold fork
#: also uses this to decide what it advertises at all (#2116): offering a
#: tool it can only refuse invites a round trip that bills output tokens for
#: a refusal instead of narrative text.
CHECKPOINT_TOOL_NAME = "continuity_edit"


async def await_or_cancel(awaitable, cancel_event: asyncio.Event):
    """Await one provider operation while honoring the checkpoint stop signal."""
    operation = asyncio.create_task(awaitable)
    stopped = asyncio.create_task(cancel_event.wait())
    try:
        await asyncio.wait({operation, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.CancelledError
        return await operation
    finally:
        for task in (operation, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, stopped, return_exceptions=True)


def _tool_call_dicts(response) -> list[dict[str, Any]]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            },
        }
        for call in response.tool_calls
    ]


class InputRoom(NamedTuple):
    """One fit verdict with the numbers behind it; ``unusable`` names why they
    cannot be compared (a warm source with no valid provider anchor)."""

    counted: int
    bound: int
    unusable: str | None = None


def input_room(request, messages, tools, reserve: int = 0) -> InputRoom:
    """Measure this fork's whole next input, including its private tool work.

    ``reserve`` keeps that much room free beyond the reply allowance.
    """
    from core.token_estimation import measure_request

    bound = request.target.context_window - request.max_output_tokens - reserve
    source = request.warm_source
    if source is None:
        # Cold must count exactly what its admission counted, or an admitted
        # fold is refused (or overflows) inside the loop that runs it.
        from core.checkpoint_cold_count import cold_input_tokens

        counted = cold_input_tokens(messages, request.target) + cold_input_tokens(
            tools, request.target,
        )
        return InputRoom(counted, bound)
    count = measure_request({"messages": messages, "tools": tools}, target=request.target)
    provider_input = getattr(source, "provider_input_tokens", None)
    rendered_input = getattr(source, "rendered_input_tokens", None)
    if not (
        type(provider_input) is int
        and provider_input >= 0
        and type(rendered_input) is int
        and rendered_input >= 0
    ):
        return InputRoom(count.tokens, bound, "warm_token_fields_invalid")
    if count.tokens < rendered_input:
        return InputRoom(count.tokens, bound, "estimate_below_rendered_input")
    return InputRoom(provider_input + count.tokens - rendered_input, bound)


def input_fits(request, messages, tools, reserve: int = 0) -> bool:
    room = input_room(request, messages, tools, reserve)
    return room.unusable is None and room.counted <= room.bound


async def input_fits_off_loop(request, messages, tools, reserve: int = 0) -> bool:
    """Run ``input_fits`` on a worker thread; tokenizing a full fold is slow."""
    return await asyncio.to_thread(input_fits, request, messages, tools, reserve)


def _append_assistant_message(messages: list[dict[str, Any]], response: Any) -> None:
    messages.append(
        {
            "role": "assistant",
            "content": response.content,
            "tool_calls": _tool_call_dicts(response),
            **(
                {"reasoning_content": response.reasoning_content}
                if response.reasoning_content is not None
                else {}
            ),
        }
    )


async def run_tool_calls(
    agent: Any,
    response: Any,
    messages: list[dict[str, Any]],
    cancel_event: asyncio.Event,
    request: ProviderForkRequest,
    tools: list[dict[str, Any]],
    *,
    kind: ForkKind,
) -> bool:
    """Run a Native checkpoint batch while retaining every completed effect.

    A result that would not fit the remaining room is never sent: its effect
    stands, and the fork sees a short error telling it to write the
    checkpoint (#2514). Only when even that does not fit does the batch fail.
    """
    _append_assistant_message(messages, response)
    completed: list[CompletedToolCall] = []
    try:
        if not await input_fits_off_loop(request, messages, tools):
            return False
        for call in response.tool_calls:
            if call.name not in kind.tool_allowlist:
                result = kind.tool_refusal
            else:
                try:
                    result = await kind.tool_executor(
                        agent, call.name, call.arguments, cancel_event, request,
                    )
                except CompletedToolFailure as failure:
                    enrich_completed_failure(
                        failure,
                        prefix=tuple(completed),
                        name=call.name,
                        arguments=call.arguments,
                        thread_id=request.thread_id,
                        invocation_id=call.id,
                        provider_tool_call_id=call.id,
                        checkpoint_id=request.fork_id,
                    )
                    raise
                completed.append(CompletedToolCall(
                    name=call.name, result=result, arguments=dict(call.arguments),
                    thread_id=request.thread_id, invocation_id=call.id,
                    provider_tool_call_id=call.id, checkpoint_id=request.fork_id,
                ))
            reply = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result,
            }
            messages.append(reply)
            if not await input_fits_off_loop(request, messages, tools):
                room = await asyncio.to_thread(input_room, request, messages, tools)
                reply["content"] = (
                    f"Error: this result ({len(result)} characters) is "
                    + kind.over_room_text.format(excess=room.counted - room.bound)
                )
                if not await input_fits_off_loop(request, messages, tools):
                    return False
        return True
    except CompletedToolFailure:
        raise
    except Exception as error:
        if not completed:
            raise
        raise completed_tool_failure(tuple(completed), error, harness="native") from error
