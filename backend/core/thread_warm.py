"""Existing per-thread warm ownership and delivery-aware admission."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import sqlite3
from typing import Any

from loguru import logger

from infra import state
from infra.send_target import NO_MODEL_SELECTED


WarmFactory = Callable[[], Awaitable[dict[str, Any]]]
WarmAdmissionCheck = Callable[[], str | None]


def warm_suppression_reason(
    thread_id: str,
    *,
    db: sqlite3.Connection,
) -> str | None:
    """Return the canonical delivery owner that currently forbids warming.

    A row held under ``NO_MODEL_SELECTED`` has no delivery owner: only the
    person choosing a model lifts that hold, so it does not forbid a warm.
    Rows held for an account or an unavailable model already carry a model and
    can start a turn the moment recovery lifts the hold, so they still do.
    """
    active = db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? "
        "AND status='running' LIMIT 1",
        (thread_id,),
    ).fetchone()
    if active is not None:
        return "active_turn"
    queued = db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND input_order IS NOT NULL "
        "AND delivery_state='queued' AND active_turn_id IS NULL "
        "AND COALESCE(delivery_error, '') != ? LIMIT 1",
        (thread_id, NO_MODEL_SELECTED),
    ).fetchone()
    if queued is not None:
        return "queued_input"
    return None


def warm_prefix_key(
    thread_id: str,
    model_id: str,
    messages: list[dict],
    tools: list[dict],
    prompt_shape: dict[str, Any] | None = None,
) -> str:
    """Fingerprint one thread/model/prefix without retaining prompt bytes."""
    payload = json.dumps(
        [model_id, messages, tools, prompt_shape or {}],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"{thread_id}:{model_id}:{digest}"


async def _join_warm_task(task: asyncio.Task) -> None:
    """Join one warm owner even if its provider reports cancellation late."""
    joined = asyncio.gather(task, return_exceptions=True)
    deferred_cancel: asyncio.CancelledError | None = None
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError as exc:
            deferred_cancel = exc
    joined.result()
    if deferred_cancel is not None:
        raise deferred_cancel


async def cancel_thread_warm(
    thread_id: str,
    *,
    expected_task: asyncio.Task | None = None,
) -> bool:
    """Cancel and join the exact thread warm owned by the existing task map."""
    task = expected_task or state._active_warm_tasks.get(thread_id)
    if task is None:
        return False
    was_running = not task.done()
    try:
        if was_running:
            task.cancel()
            await _join_warm_task(task)
    finally:
        if state._active_warm_tasks.get(thread_id) is task:
            state._active_warm_tasks.pop(thread_id, None)
    return was_running


async def await_thread_warm(
    thread_id: str,
    *,
    cancel_event: asyncio.Event | None = None,
) -> bool:
    """Await only a registered warm, or this Turn's exact cancellation.

    Native delivery never creates provider work here. The shield keeps the
    warm independently owned: a Turn cancellation ends only its waiter, never
    the requested warm task.
    """
    task = state._active_warm_tasks.get(thread_id)
    if task is None or task.done():
        return False
    if cancel_event is None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            waiter = asyncio.current_task()
            if waiter is not None and waiter.cancelling():
                raise
            if task.cancelled():
                return False
            raise
        return True
    if cancel_event.is_set():
        return False

    warm_wait = asyncio.shield(task)
    cancellation_wait = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            (warm_wait, cancellation_wait),
            return_when=asyncio.FIRST_COMPLETED,
        )
        # A concurrently completed cancellation wins: the caller must take the
        # Native loop's normal pre-set-cancellation terminal path.
        if cancellation_wait in done:
            return False
        try:
            warm_wait.result()
        except asyncio.CancelledError:
            waiter = asyncio.current_task()
            if waiter is not None and waiter.cancelling():
                raise
            if task.cancelled():
                return False
            raise
        return True
    finally:
        if not warm_wait.done():
            warm_wait.cancel()
        if not cancellation_wait.done():
            cancellation_wait.cancel()
        await asyncio.gather(warm_wait, cancellation_wait, return_exceptions=True)


async def await_warm_outcome(task: asyncio.Task) -> dict[str, Any]:
    """The real outcome of a registered warm, without ever cancelling it.

    The shield keeps a client disconnect (which cancels this request) from
    cancelling the warm the owner registered; only the waiter stops.
    """
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        waiter = asyncio.current_task()
        if waiter is not None and waiter.cancelling():
            raise
        return {"ok": False, "message": "Warm was cancelled"}
    except Exception as exc:
        return {"ok": False, "message": str(exc) or type(exc).__name__}
    if isinstance(result, dict) and not result.get("ok"):
        return {"ok": False, "message": str(result.get("message") or "Warm failed")}
    return {"ok": True, "message": "Warm finished"}


async def join_requested_warm_for_delivery(
    thread_id: str,
    catalog_id: str,
    *,
    cancel_event: asyncio.Event | None = None,
) -> bool:
    """Join an explicit matching warm unless this exact Turn is cancelled.

    A warm of another model is left running on its own hardware: delivery
    neither waits for it nor cancels it.
    """
    task = state._active_warm_tasks.get(thread_id)
    if task is None or getattr(task, "_corsia_warm_model", None) != catalog_id:
        return False
    try:
        return await await_thread_warm(thread_id, cancel_event=cancel_event)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "Native cache warm failed before delivery for {}: {}",
            thread_id,
            error,
        )
        return False


async def start_thread_warm(
    thread_id: str,
    warm_key: str,
    factory: WarmFactory,
    *,
    model: str,
    admission_check: WarmAdmissionCheck,
) -> tuple[asyncio.Task | None, bool, str | None]:
    """Deduplicate or replace one thread warm through the existing owner.

    ``model`` is the warm's catalog id, stamped beside the key so delivery can
    tell whose prefix this is without parsing the key.
    """
    while True:
        prior = state._active_warm_tasks.get(thread_id)
        if prior is None:
            break
        if (
            not prior.done()
            and not prior.cancelling()
            and getattr(prior, "_corsia_warm_key", None) == warm_key
        ):
            return prior, False, None
        # Keep the old owner registered while it is joined. Concurrent
        # replacements then converge through this loop instead of starting
        # beside an unjoined task or overwriting another replacement.
        await cancel_thread_warm(thread_id)

    # A replacement join yields. Recheck real delivery ownership after every
    # prior warm is gone and immediately before task registration; no await
    # separates this check from ownership of the existing task map.
    suppression = admission_check()
    if suppression is not None:
        return None, False, suppression

    task = asyncio.create_task(factory())
    setattr(task, "_corsia_warm_key", warm_key)
    setattr(task, "_corsia_warm_model", model)
    state._active_warm_tasks[thread_id] = task

    def _release(completed: asyncio.Task) -> None:
        if state._active_warm_tasks.get(thread_id) is completed:
            state._active_warm_tasks.pop(thread_id, None)

    task.add_done_callback(_release)
    return task, True, None
