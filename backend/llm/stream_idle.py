"""Bound on silence during one streamed provider call.

The HTTP client's read timeout is per socket read, so a provider that keeps the
connection alive with heartbeat bytes but never sends a delta is never cut off
and the Turn waits on it indefinitely (#2333). This bounds silence on one call,
never the length of a call that keeps producing output.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from loguru import logger

from infra import constants

T = TypeVar("T")

STALLED_ERROR_KIND = "provider_stream_stalled"
TRUNCATED_ERROR_KIND = "provider_stream_truncated"


class ProviderStreamStalled(Exception):
    """A provider call sent no progress for longer than the silence bound."""

    def __init__(self, idle_seconds: float, phase: str) -> None:
        self.idle_seconds = idle_seconds
        self.phase = phase
        super().__init__(
            f"no data for {idle_seconds:g} seconds while {phase}"
        )


class ProviderStreamTruncated(Exception):
    """A provider stream ended without ever sending its terminal event (#2485).

    The provider closed the stream cleanly, so nothing was raised at the
    transport: the loop saw the end of the stream instead. What is missing is
    the marker that says the answer is complete, so the partial answer must not
    be presented as a finished one.
    """

    def __init__(self) -> None:
        super().__init__("the provider stream ended before its final event")


#: The provider cut the call off rather than finishing it: it went silent, or
#: it ended the stream early. Both are retried, then classified the same way.
PROVIDER_CUT_OFF_ERRORS: tuple[type[Exception], ...] = (
    ProviderStreamStalled, ProviderStreamTruncated,
)


async def await_with_idle_bound(awaitable: Awaitable[T], phase: str) -> T:
    """Await one provider step, failing as a stall when it stays silent."""
    idle = constants.LLM_STREAM_IDLE_TIMEOUT_S
    try:
        async with asyncio.timeout(idle) as scope:
            return await awaitable
    except TimeoutError:
        if scope.expired():
            raise ProviderStreamStalled(idle, phase) from None
        raise


@asynccontextmanager
async def bounded_stream(
    stream: Any,
    *,
    is_progress: Callable[[Any], bool] | None = None,
) -> AsyncIterator[AsyncIterator[Any]]:
    """Yield an iterator over ``stream`` that raises when progress stops.

    This is the one owner of the stream's connection: it is closed on every
    exit (finished, stalled, failed, cancelled), so no call site closes it.

    ``is_progress`` marks items that carry model output. Heartbeat items that do
    not are still yielded but leave the deadline unchanged, so they cannot keep
    a silent call alive (#2422 review).
    """
    try:
        yield _iterate_with_deadline(stream, is_progress)
    finally:
        await _close_stream(stream)


async def _iterate_with_deadline(
    stream: Any, is_progress: Callable[[Any], bool] | None,
) -> AsyncIterator[Any]:
    idle = constants.LLM_STREAM_IDLE_TIMEOUT_S
    iterator = stream.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + idle
    while True:
        try:
            async with asyncio.timeout_at(deadline) as scope:
                item = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            if scope.expired():
                raise ProviderStreamStalled(idle, "streaming the response") from None
            raise
        progressed = is_progress is None or is_progress(item)
        yield item
        if progressed:
            # Time the consumer spent handling the item is not provider silence.
            deadline = loop.time() + idle


async def _close_stream(stream: Any) -> None:
    """Best-effort close via whichever async close method the stream exposes."""
    close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as close_error:
        logger.warning("provider stream close failed: {}", close_error)


def error_kind_of(error: Exception) -> str | None:
    """The terminal ``error_kind`` a retry ladder attaches once it gives up."""
    if isinstance(error, ProviderStreamTruncated):
        return TRUNCATED_ERROR_KIND
    return STALLED_ERROR_KIND if isinstance(error, ProviderStreamStalled) else None


def output_reached_turn(
    text_parts: list[str], reasoning_parts: list[str], emitted_tool_indices: set[int],
    on_content_chunk: Any, on_reasoning_chunk: Any,
) -> bool:
    """True once a delta was actually handed to a Turn callback; text or reasoning only count when its callback exists.

    A tool-call header parsed but not yet emitted has reached nothing, so a cut-off stream
    there is still retryable. Every streaming loop decides this the same way.
    """
    return bool(
        (text_parts and on_content_chunk)
        or (reasoning_parts and on_reasoning_chunk)
        or emitted_tool_indices
    )


def cut_off_response(error: Exception, model: str):
    """The classified terminal response for a call the provider cut off.

    One builder for both reasons a call is cut off before its terminal event:
    silence (#2422) or an early end (#2485). It owns the error kind and the
    sentence for each, so no caller has to inspect the error's type.
    """
    from llm.router import LLMResponse

    if isinstance(error, ProviderStreamTruncated):
        error_kind = TRUNCATED_ERROR_KIND
        content = (
            f"The provider for {model} ended the response early, so the call was "
            "ended. Retry, or switch models if it persists."
        )
    else:
        error_kind = STALLED_ERROR_KIND
        content = (
            f"The provider for {model} sent {error}, so the call was ended. "
            "Retry, or switch models if it persists."
        )
    logger.error("provider stream cut off ({}): {}", error_kind, error)
    return LLMResponse(
        content=content,
        finish_reason="error",
        error_kind=error_kind,
    )
