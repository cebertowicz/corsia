"""Serialize Codex provider events behind exact durable input settlement."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial

from subagents import codex_runtime_events
from subagents.codex_runtime_state import (
    _CODEX_THREADS,
    _DEAD,
    _LiveCodex,
    finalize_live,
)

log = logging.getLogger(__name__)
_SAFE_BOUNDARY_TASKS: dict[str, asyncio.Task] = {}
_SAFE_BOUNDARY_WAKES: set[str] = set()

_DELIVERY_GATED_PROVIDER_EVENTS = frozenset(
    {
        "assistant_text",
        "provider_tool_violation",
        "result",
        "step_reasoning",
        "stream_line_dropped",
        "text_delta",
        "tool_result",
        "tool_use",
        "turn_failed",
    }
)
_TERMINAL_PROVIDER_EVENTS = frozenset({"result", "turn_failed"})


def arm_delivery_event_barrier(live: _LiveCodex) -> None:
    """Hold one input's provider output until delivery becomes durable."""
    live.input_delivery_unproven = False
    live.delivery_event_epoch += 1
    live.delivery_event_gate = asyncio.Event()


def discard_delivery_event_barrier(live: _LiveCodex) -> None:
    """Discard early events for a request that never became accepted."""
    gate = live.delivery_event_gate
    if gate is not None:
        gate.set()
    # Deferred callbacks captured the prior epoch at ingress. Invalidate it
    # before a fallback request can arm a new gate and release stale output.
    live.delivery_event_epoch += 1


async def release_delivery_event_barrier(live: _LiveCodex) -> None:
    """Open the current gate and drain output already deferred behind it."""
    gate = live.delivery_event_gate
    if gate is None:
        return
    gate.set()
    tail = live.delivery_event_tail
    if tail is None or tail.done():
        return
    # Settlement already made the input durable and its early events valid.
    # Caller cancellation must not escape this drain and invalidate that output.
    while not tail.done():
        try:
            await asyncio.shield(tail)
        except asyncio.CancelledError:
            continue
    tail.result()


def fail_delivery_event_barrier(thread_id: str, live: _LiveCodex) -> None:
    """Stop projecting output when durable input settlement itself failed."""
    del thread_id
    live.input_delivery_unproven = True
    discard_delivery_event_barrier(live)


async def await_delivery_settlement(
    settle_delivery: Callable[[], Awaitable[None]],
) -> None:
    """Finish durable settlement even if the sending caller disconnects."""
    settlement = asyncio.create_task(settle_delivery())
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
    settlement.result()


async def _deliver_codex_event(
    live: _LiveCodex,
    thread_id: str,
    inner,
    event: dict,
) -> None:
    terminal = event.get("kind") in ("result", "turn_failed")
    current = event.get("is_current_turn")
    if terminal and (
        current is False or (live.requires_terminal_identity and current is not True)
    ):
        log.warning(
            "codex_runtime: ignored stale terminal for session=%s turn=%s",
            thread_id,
            event.get("turn_id"),
        )
        return
    try:
        await inner(event)
    finally:
        if terminal and event.get("_corsia_terminal_committed") is True:
            finalize_live(thread_id, live)


def schedule_safe_boundary(thread_id: str) -> None:
    """Coalesce wakes, retaining one follow-on delivery without blocking the reader."""
    from subagents.codex_inbox_batch import deliver_live_inbox_batch

    pending = _SAFE_BOUNDARY_TASKS.get(thread_id)
    if pending is not None:
        # Completion owns removal, including wakes before its callback runs.
        _SAFE_BOUNDARY_WAKES.add(thread_id)
        return
    task = asyncio.create_task(
        deliver_live_inbox_batch(thread_id),
        name=f"codex-safe-boundary:{thread_id}",
    )
    _SAFE_BOUNDARY_TASKS[thread_id] = task
    task.add_done_callback(partial(_finish_safe_boundary, thread_id=thread_id))


def _finish_safe_boundary(task: asyncio.Task, *, thread_id: str) -> None:
    _observe_deferred_event(task, thread_id=thread_id, kind="safe_boundary")
    if _SAFE_BOUNDARY_TASKS.get(thread_id) is task:
        del _SAFE_BOUNDARY_TASKS[thread_id]
        if thread_id in _SAFE_BOUNDARY_WAKES:
            _SAFE_BOUNDARY_WAKES.remove(thread_id)
            schedule_safe_boundary(thread_id)


def _capture_context_usage_frame(
    thread_id: str,
    live: _LiveCodex,
) -> dict | None:
    """Freeze usage and Turn identity at Codex safe-boundary ingress."""
    from infra.context_receipts import live_context_usage_payload
    from subagents.codex_terminal_context import live_measured_context_usage

    identity = live.corsia_turn
    context_usage = live_measured_context_usage(thread_id, live)
    if identity is None or context_usage is None:
        return None
    return {
        "type": "context_usage_updated",
        "thread_id": thread_id,
        "context_usage": live_context_usage_payload(context_usage),
        **identity.frame_fields(),
    }


async def _deliver_serialized(
    live: _LiveCodex,
    thread_id: str,
    event: dict,
    deliver,
    *,
    gate: asyncio.Event | None,
    previous: asyncio.Task | None,
    epoch: int,
) -> None:
    if previous is not None:
        try:
            await asyncio.shield(previous)
        except asyncio.CancelledError:
            return
        except Exception as error:
            # The observer already surfaced the completed failure. Preserve
            # ordering, but let a later provider event retry unsettled work.
            log.warning(
                "codex_runtime: continued after failed prior provider event "
                "for session=%s kind=%s: %s",
                thread_id,
                event.get("kind"),
                error,
            )
    if gate is not None:
        await gate.wait()
    if gate is not None and (
        live.input_delivery_unproven or live.delivery_event_epoch != epoch
    ):
        # A newer generation owns the handle, or input delivery was not proved.
        # Persisting this output would place it ahead of the redelivered input.
        log.warning(
            "codex_runtime: dropped provider event from a superseded or "
            "unproven delivery for session=%s kind=%s",
            thread_id,
            event.get("kind"),
        )
        return
    await deliver(event)


def _observe_deferred_event(
    task: asyncio.Task,
    *,
    thread_id: str,
    kind: object,
) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "codex_runtime: deferred provider event failed for %s (%s)",
            thread_id,
            kind,
            exc_info=(type(error), error, error.__traceback__),
        )


def runtime_event_handler(
    live: _LiveCodex, thread_id: str, bcast,
    checkpoint_generation_id: str | None = None,
):
    inner = codex_runtime_events.make_on_event(
        live,
        thread_id,
        bcast,
        dead_set=_DEAD,
        thread_registry=_CODEX_THREADS,
    )

    async def deliver(
        event: dict,
        *,
        context_usage_frame: dict | None = None,
    ) -> None:
        await _deliver_codex_event(live, thread_id, inner, event)
        if event.get("kind") == "tool_result":
            if context_usage_frame is not None:
                bcast(thread_id, context_usage_frame)
            schedule_safe_boundary(thread_id)

    async def on_event(event: dict) -> None:
        kind = event.get("kind")
        if kind == "thread_started" and checkpoint_generation_id is not None:
            event["_corsia_checkpoint_generation_id"] = checkpoint_generation_id
        if kind in _DELIVERY_GATED_PROVIDER_EVENTS and live.corsia_turn is not None:
            event.setdefault("_corsia_turn_identity", live.corsia_turn)
        context_usage_frame = (
            _capture_context_usage_frame(thread_id, live)
            if kind == "tool_result"
            else None
        )
        deliver_event = partial(
            deliver,
            context_usage_frame=context_usage_frame,
        )
        gate = live.delivery_event_gate
        terminal = kind in _TERMINAL_PROVIDER_EVENTS
        if not terminal and (
            kind not in _DELIVERY_GATED_PROVIDER_EVENTS or gate is None
        ):
            await deliver_event(event)
            return
        task = asyncio.create_task(
            _deliver_serialized(
                live,
                thread_id,
                event,
                deliver_event,
                gate=gate,
                previous=live.delivery_event_tail,
                epoch=live.delivery_event_epoch,
            ),
            name=f"codex-delivery-event:{thread_id}:{kind}",
        )
        live.delivery_event_tail = task
        task.add_done_callback(
            partial(
                _observe_deferred_event,
                thread_id=thread_id,
                kind=kind,
            )
        )
        if not terminal and gate is not None and gate.is_set():
            await task

    return on_event
