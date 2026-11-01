"""Private tool-loop mechanics for one disposable Native fork."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, NamedTuple

from loguru import logger

from core.fork_types import ProviderForkRequest
from core.fork_runtime import ForkKind
from infra import state
from infra.send_target import omlx_seat_and_model
from tools.recorded_call import (
    CompletedToolCall,
    CompletedToolFailure,
    completed_tool_failure,
    enrich_completed_failure,
)


async def await_or_cancel(awaitable, cancel_event: asyncio.Event):
    """Await one provider operation while honoring the fork stop signal."""
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


async def yield_to_turn_call(request: ProviderForkRequest, cancel_event: asyncio.Event) -> None:
    """On a local oMLX seat, start no fork call while this thread's Turn call runs.

    oMLX's prefill memory guard charges both sequences, so a fork call beside the
    Turn's call can get the Turn refused on a small Mac. The Turn's own task is
    only watched, never cancelled.
    """
    if omlx_seat_and_model(request.target) is None:
        return
    while (turn_call := state._active_llm_tasks.get(request.thread_id)) is not None and not turn_call.done():
        stopped = asyncio.create_task(cancel_event.wait())
        try:
            await asyncio.wait({turn_call, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
        if cancel_event.is_set():
            raise asyncio.CancelledError


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


class MeasuredInput(NamedTuple):
    """The provider's count of a fork's last call plus its reply, anchored to
    the rendered estimate of those same messages, as a warm source is."""

    provider_input_tokens: int
    rendered_input_tokens: int


def measured_input(request, messages, tools, usage) -> MeasuredInput | None:
    """Anchor the next call on what the provider measured for the last one.

    ``messages`` already ends with that reply. A cold count can be a UTF-8
    byte bound (a writer with no bundled tokenizer whose server cannot
    tokenize), several times its real tokens, so re-counting the reply's
    reasoning overflowed a small window the provider itself had room for.
    """
    from core.checkpoint_native_admission import effective_input_tokens
    from core.token_estimation import measure_request

    measured = effective_input_tokens(usage)
    completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if measured is None or isinstance(completion, bool) or not isinstance(completion, int):
        return None
    if completion < 0:
        return None
    rendered = measure_request({"messages": messages, "tools": tools}, target=request.target)
    return MeasuredInput(measured + completion, rendered.tokens)


def input_room(
    request, messages, tools, reserve: int = 0, measured: MeasuredInput | None = None,
) -> InputRoom:
    """Measure this fork's whole next input, including its private tool work.

    ``reserve`` keeps that much room free beyond the reply allowance. Once a
    call has been measured, later input counts from that measurement.
    """
    from core.token_estimation import measure_request

    bound = request.target.context_window - request.max_output_tokens - reserve
    source = measured if measured is not None else request.warm_source
    if source is None:
        # Cold must count exactly what its admission counted, or an admitted
        # fold is refused (or overflows) inside the loop that runs it.
        from core.fork_cold_count import cold_input_tokens

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


def input_fits(
    request, messages, tools, reserve: int = 0, measured: MeasuredInput | None = None,
) -> bool:
    room = input_room(request, messages, tools, reserve, measured)
    return room.unusable is None and room.counted <= room.bound


async def input_fits_off_loop(
    request, messages, tools, reserve: int = 0, measured: MeasuredInput | None = None,
) -> bool:
    """Run ``input_fits`` on a worker thread; tokenizing a full fold is slow."""
    return await asyncio.to_thread(input_fits, request, messages, tools, reserve, measured)


def assistant_message(response: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content,
        "tool_calls": _tool_call_dicts(response),
        **(
            {"reasoning_content": response.reasoning_content}
            if response.reasoning_content is not None
            else {}
        ),
    }


_OPERATION = re.compile(r"[a-z_]{1,24}")


def _log_tool_call(request: ProviderForkRequest, kind: ForkKind, call: Any, outcome: str,
                   result: str) -> None:
    """One content-free line per fork tool call: which call, its outcome, its size."""
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    operation = arguments.get("operation")
    if not (isinstance(operation, str) and _OPERATION.fullmatch(operation)):
        operation = "-" if operation is None else "other"
    logger.info(
        "fork.tool_call fork={} kind={} tool={} operation={} outcome={} result_chars={}",
        request.fork_id, kind.name, call.name, operation, outcome, len(result),
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
    measured: MeasuredInput | None = None,
) -> bool:
    """Run a Native fork batch while retaining every completed effect.

    A result that would not fit the remaining room is never sent: its effect
    stands, and the fork sees its kind's short instruction to finish. Only when even that does not fit does the batch fail.
    """
    messages.append(assistant_message(response))
    completed: list[CompletedToolCall] = []
    try:
        if not await input_fits_off_loop(request, messages, tools, 0, measured):
            return False
        for call in response.tool_calls:
            if call.name not in kind.tool_allowlist:
                result = kind.tool_refusal
                outcome = "refused"
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
                        fork_id=request.fork_id,
                    )
                    raise
                completed.append(CompletedToolCall(
                    name=call.name, result=result, arguments=dict(call.arguments),
                    thread_id=request.thread_id, invocation_id=call.id,
                    provider_tool_call_id=call.id, fork_id=request.fork_id,
                ))
                outcome = "error" if result.startswith("Error") else "ok"
            reply = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result,
            }
            messages.append(reply)
            if not await input_fits_off_loop(request, messages, tools, 0, measured):
                room = await asyncio.to_thread(
                    input_room, request, messages, tools, 0, measured,
                )
                reply["content"] = (
                    f"Error: this result ({len(result)} characters) is "
                    + kind.over_room_text.format(excess=room.counted - room.bound)
                )
                _log_tool_call(request, kind, call, "over_room", result)
                if not await input_fits_off_loop(request, messages, tools, 0, measured):
                    return False
            else:
                _log_tool_call(request, kind, call, outcome, result)
        return True
    except CompletedToolFailure:
        raise
    except Exception as error:
        if not completed:
            raise
        raise completed_tool_failure(tuple(completed), error, harness="native") from error
