"""WebSocket admission ordering, task ownership, and durable receipts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any

from fastapi import WebSocket
from loguru import logger

from infra.send_types import InboxQueuedInput
from infra.state import send_to_client


# Strong references keep detached provider work alive through completion.
background_tasks: set[asyncio.Task[Any]] = set()


def _observe_background_task(task: asyncio.Task[Any]) -> None:
    background_tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except (asyncio.CancelledError, RuntimeError):
        return
    if error is not None:
        coroutine = task.get_coro()
        coroutine_name = getattr(coroutine, "__qualname__", None) or "<unknown>"
        logger.opt(exception=error).error(
            "Background WebSocket work failed (task={}, coroutine={})",
            task.get_name(),
            coroutine_name,
        )


def track_background_task(task: asyncio.Task[Any]) -> None:
    """Retain detached WebSocket work and observe its terminal failure."""
    background_tasks.add(task)
    task.add_done_callback(_observe_background_task)


def _release_admission_when_done(
    event: asyncio.Event,
    _task: asyncio.Task[None],
) -> None:
    event.set()


async def process_messages(
    ws: WebSocket,
    user: dict,
    user_id: str,
    message_queue: asyncio.Queue[Any],
    handle_message: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """Serialize durable admission while provider work continues detached."""
    while True:
        data = await message_queue.get()
        if data is None:
            break
        if data.get("type") == "message":
            admission_complete = asyncio.Event()
            task: asyncio.Task[None] = asyncio.create_task(handle_message(
                ws, user, user_id, data,
                admission_complete=admission_complete.set,
            ))
            track_background_task(task)
            task.add_done_callback(partial(
                _release_admission_when_done, admission_complete,
            ))
            await admission_complete.wait()


def reserve_admission_receipt(
    ws: WebSocket,
    thread_id: str,
    input_id: str,
    result: Any,
) -> None:
    """Reserve one ordered socket write without delaying delivery work."""
    try:
        task = send_to_client(
            ws, _accepted_input_payload(thread_id, input_id, result),
        )
    except (RuntimeError, ConnectionError, AttributeError) as error:
        logger.warning(
            "Native WebSocket admission receipt failed for {}: {}",
            thread_id,
            error,
        )
        return
    task.add_done_callback(
        lambda completed: _observe_admission_receipt(completed, thread_id),
    )


def _observe_admission_receipt(task: asyncio.Task, thread_id: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "Native WebSocket admission receipt failed for {}: {}",
            thread_id,
            error,
        )


async def send_input_accepted(
    ws: WebSocket,
    thread_id: str,
    input_id: str,
    result: Any,
) -> None:
    """Acknowledge only after the durable row and exact Turn are accepted."""
    await send_to_client(
        ws, _accepted_input_payload(thread_id, input_id, result),
    )


def _accepted_input_payload(
    thread_id: str,
    input_id: str,
    result: Any,
) -> dict:
    if isinstance(result, InboxQueuedInput):
        receipt = result.receipt
        return {
            "type": "input_queued",
            "queue_kind": "messages",
            "thread_id": thread_id,
            "input_id": input_id,
            "message_id": receipt.message_id,
            "queue_order": receipt.input_order,
            "created_at": receipt.created_at,
            "delivery_state": "queued",
            "delivery_error": None,
            "delivery_revision": receipt.delivery_revision,
            "duplicate": result.duplicate,
        }
    turn_id = getattr(result, "turn_id", None)
    turn_order = getattr(result, "turn_order", None)
    if not isinstance(turn_id, str) or not isinstance(turn_order, int):
        raise RuntimeError("accepted WebSocket input has no Turn identity")
    payload = {
        "type": "input_accepted",
        "thread_id": thread_id,
        "input_id": input_id,
        "message_id": getattr(result, "message_id", None),
        "created_at": getattr(result, "created_at", None),
        "turn_id": turn_id,
        "turn_order": turn_order,
    }
    seq = getattr(result, "seq", None)
    if isinstance(seq, int) and seq > 0:
        payload["seq"] = seq
    return payload
