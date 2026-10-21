"""One linear checkpoint execution and its exact cancellation handles."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Callable
from typing import Any

from core.checkpoint_capture import read_checkpoint as _read
from core.checkpoint_cold_fallback import invoke_with_cold_correction
from core.checkpoint_fold_gate import refuse_limited_target as _refuse_limited_target
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_types import (
    ForkNotAdmitted,
    ProviderForkRequest,
    ProviderForkResult,
)
from core.checkpoint_types import (
    CheckpointRecord,
    CheckpointRequest,
    CheckpointTargetLimited,
    IdleCheckpointAdmissionResult,
    IdleCheckpointDisposition,
    IdleCheckpointReason,
)
from infra import checkpoint_queries
from infra import checkpoints as checkpoint_store
from infra import idle_checkpoints as idle_checkpoint_store
from infra.checkpoints import CheckpointConflict
from infra.db_async import run_in_db
from infra.time import utc_now_iso


from core.fork_runtime import ForkHandle, handles as _tasks

_wakes: set[asyncio.Task[None]] = set()


async def _invoke(
    request: ProviderForkRequest,
    cancel_event: asyncio.Event,
) -> ProviderForkResult:
    if request.cold_source is not None:
        from infra.continuity_staging import frozen_continuity_index

        index = await run_in_db(
            frozen_continuity_index, request.fork_id, request.thread_id,
        )
        request = replace(
            request,
            instruction=(
                request.instruction
                + "\n\n[CORSIA frozen continuity index]\n"
                + index
            ),
        )
    from core.placement_review import attach_review

    request = await attach_review(request)
    request = await CHECKPOINT_KIND.usage_binder(request)
    if request.target.harness == "native":
        from core.checkpoint_native_fork import invoke_fork
    elif request.target.harness == "claude-code":
        from core.checkpoint_cc_fork import invoke_fork
    elif request.target.harness == "codex":
        from core.checkpoint_codex_fork import invoke_fork
    else:
        raise ValueError(f"unsupported checkpoint harness: {request.target.harness}")
    async def record_input(first_included_seq: int | None) -> None:
        await run_in_db(
            checkpoint_store.admit_cold_input, request.fork_id,
            first_included_seq=first_included_seq,
        )

    if request.target.harness == "claude-code":
        async def bind_executor(executor):
            handle = _tasks.get(request.fork_id)
            if handle is None or handle.thread_id != request.thread_id:
                raise ForkNotAdmitted("checkpoint invocation no longer owns its tools")
            handle.tool_executor = executor

        return await invoke_fork(
            request, cancel_event, kind=CHECKPOINT_KIND,
            on_cold_input=record_input, on_tool_executor=bind_executor,
        )
    return await invoke_fork(
        request, cancel_event, kind=CHECKPOINT_KIND, on_cold_input=record_input,
    )


def _broadcast_changed(thread_id: str) -> None:
    from infra import state
    from loguru import logger

    try:
        state.broadcast_to_thread(
            thread_id,
            {"type": "checkpoint_changed", "thread_id": thread_id},
        )
    except Exception:
        logger.exception("checkpoint invalidation broadcast failed: {}", thread_id)


async def _notify_completion(record: CheckpointRecord) -> None:
    """Invalidate the durable projection and re-drive the ordinary FIFO."""
    _broadcast_changed(record.thread_id)
    from infra.thread_execution import drive_thread
    from loguru import logger

    if record.status == "installed":
        from infra.send_broadcast import broadcast_message_rows

        try:
            await broadcast_message_rows(
                record.thread_id,
                (checkpoint_store.completion_receipt_id(record.id),),
            )
        except Exception:
            logger.exception(
                "checkpoint completion receipt broadcast failed: {}", record.id,
            )
        from core import placement_worker

        placement_worker.poke()

    # A failed wake belongs to its thread: the input stays in the durable FIFO
    # for the next drive, and service health is unaffected.
    async def redrive() -> None:
        try:
            await drive_thread(record.thread_id)
        except Exception:
            logger.exception(
                "checkpoint completion wake failed: {}", record.thread_id,
            )

    wake = asyncio.create_task(
        redrive(), name=f"checkpoint-wake:{record.thread_id}",
    )
    _wakes.add(wake)
    wake.add_done_callback(_wakes.discard)


async def _execute(
    checkpoint_id: str,
    provider_request: ProviderForkRequest,
    cancel_event: asyncio.Event,
) -> CheckpointRecord:
    # This task owns the attempt and ends its row terminal before it exits.
    # Only process shutdown after capture, or a failure write that itself
    # raises, leaves a row for reconcile_startup_checkpoints.
    try:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if provider_request.target.harness in {'claude-code', 'codex'}:
            from core.checkpoint_account import admit_checkpoint

            provider_request = admit_checkpoint(provider_request)
        result = await invoke_with_cold_correction(
            provider_request, cancel_event, _invoke,
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError
        try:
            return await CHECKPOINT_KIND.end_handler(checkpoint_id, result)
        except Exception:
            from loguru import logger

            logger.exception("checkpoint capture or install raised: {}", checkpoint_id)
            raise
    except asyncio.CancelledError:
        await run_in_db(
            checkpoint_store.cancel,
            checkpoint_id,
            ended_at=utc_now_iso(),
        )
        return await _read(checkpoint_id)
    except Exception as error:
        return await CHECKPOINT_KIND.limit_or_failure_handler(checkpoint_id, error)
    finally:
        record = await _read(checkpoint_id)
        await _notify_completion(record)


def _forget(checkpoint_id: str, task: asyncio.Task[CheckpointRecord]) -> None:
    handle = _tasks.get(checkpoint_id)
    if handle is not None and handle.task is task:
        _tasks.pop(checkpoint_id, None)
    if not task.cancelled():
        task.exception()


def _validate_pair(
    request: CheckpointRequest,
    provider_request: ProviderForkRequest,
) -> None:
    if (
        provider_request.fork_id != request.id
        or provider_request.thread_id != request.thread_id
    ):
        raise ValueError("checkpoint row and provider request identity differ")
    if provider_request.target.harness != request.writer_harness:
        raise ValueError("checkpoint writer harness differs from provider target")
    if provider_request.target.catalog_id != request.writer_model:
        raise ValueError("checkpoint writer model differs from provider target")


def _launch_checkpoint(
    request: CheckpointRequest,
    provider_request: ProviderForkRequest,
) -> None:
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        _execute(request.id, provider_request, cancel_event),
        name=f"checkpoint:{request.id}",
    )
    _tasks[request.id] = ForkHandle(
        task, cancel_event, request.thread_id, kind=CHECKPOINT_KIND, request=provider_request,
    )
    task.add_done_callback(lambda done: _forget(request.id, done))
    _broadcast_changed(request.thread_id)


async def request_checkpoint(
    request: CheckpointRequest,
    provider_request: ProviderForkRequest | None,
) -> CheckpointRecord:
    """Coalesce or start one already-admitted provider invocation."""
    active = await run_in_db(checkpoint_queries.active_for_thread, request.thread_id)
    if active is not None:
        return active
    if provider_request is None:
        raise ForkNotAdmitted("checkpoint provider capacity is unavailable")
    _validate_pair(request, provider_request)
    await _refuse_limited_target(provider_request)
    try:
        record = await run_in_db(checkpoint_store.create_running, request)
    except CheckpointConflict:
        active = await run_in_db(checkpoint_queries.active_for_thread, request.thread_id)
        if active is not None:
            return active
        raise
    if record.id == request.id:
        _launch_checkpoint(request, provider_request)
    return record


async def _commit_idle_marker(
    operation: Callable[..., IdleCheckpointAdmissionResult],
    *args: Any,
    **kwargs: Any,
) -> tuple[IdleCheckpointAdmissionResult, asyncio.CancelledError | None]:
    commit = asyncio.create_task(run_in_db(operation, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not commit.done():
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError as error:
            cancelled = error
    return commit.result(), cancelled


async def request_idle_checkpoint(turn_id: str, admission):
    """Atomically consume one due marker and start only its admitted warm call."""
    request = None if admission is None else admission.request
    provider_request = None if admission is None else admission.provider_request
    if request is not None:
        if provider_request is None:
            raise ForkNotAdmitted("idle checkpoint has no warm provider request")
        _validate_pair(request, provider_request)
    target_limited = False
    if request is not None and provider_request is not None:
        # Read outside the shield below so a cancelled fire is not held by the gate.
        try:
            await _refuse_limited_target(provider_request)
        except CheckpointTargetLimited:
            target_limited = True
            request = provider_request = None
    result, cancelled = await _commit_idle_marker(
        idle_checkpoint_store.consume_idle_marker,
        turn_id,
        request,
    )
    if target_limited and result.disposition is IdleCheckpointDisposition.REFUSED:
        result = replace(result, reason=IdleCheckpointReason.TARGET_LIMITED)
    if (
        result.disposition is IdleCheckpointDisposition.ADMITTED
        and result.record is not None
        and request is not None
        and provider_request is not None
    ):
        _launch_checkpoint(request, provider_request)
    if cancelled is not None:
        raise cancelled
    return result


async def fail_idle_checkpoint(
    turn_id: str,
    request: CheckpointRequest | None,
    error: BaseException,
) -> IdleCheckpointAdmissionResult:
    """Atomically consume an idle marker as a typed failed attempt."""
    detail = str(error).strip() or type(error).__name__
    result, cancelled = await _commit_idle_marker(
        idle_checkpoint_store.consume_idle_marker_failure,
        turn_id,
        request,
        error=detail,
        ended_at=utc_now_iso(),
    )
    if (
        result.disposition is IdleCheckpointDisposition.FAILED
        and result.record is not None
    ):
        _broadcast_changed(result.record.thread_id)
    if cancelled is not None:
        raise cancelled
    return result


async def await_checkpoint(checkpoint_id: str) -> CheckpointRecord:
    """Await live work without transferring cancellation ownership."""
    handle = _tasks.get(checkpoint_id)
    if handle is not None:
        return await asyncio.shield(handle.task)
    return await _read(checkpoint_id)


async def cancel_checkpoint(checkpoint_id: str) -> CheckpointRecord:
    """Signal only the exact live call and return its durable cancellation."""
    handle = _tasks.get(checkpoint_id)
    if handle is not None:
        handle.cancel_event.set()
    cancelled = await run_in_db(
        checkpoint_store.cancel,
        checkpoint_id,
        ended_at=utc_now_iso(),
    )
    record = cancelled or await _read(checkpoint_id)
    if cancelled is not None:
        _broadcast_changed(record.thread_id)
    return record


async def shutdown_fork_tasks() -> None:
    """Cancel and join the exact fork calls and wakes owned by this process.

    Every fork stops before its provider transport; only checkpoints have a
    checkpoint row to cancel. A review fork's request is escalated at the next
    startup (``artifact_review_capture.reconcile_startup_reviews``).
    """
    handles = tuple(_tasks.items())
    for _fork_id, handle in handles:
        handle.cancel_event.set()
    for fork_id, handle in handles:
        await handle.kind.shutdown_handler(fork_id)
    await asyncio.gather(
        *(handle.task for _fork_id, handle in handles),
        return_exceptions=True,
    )
    # Joined calls start their wakes on the way out, so take wakes last.
    wakes = tuple(_wakes)
    for wake in wakes:
        wake.cancel()
    await asyncio.gather(*wakes, return_exceptions=True)


async def reconcile_startup_checkpoints() -> tuple[CheckpointRecord, ...]:
    """Resolve the only two nonterminal crash windows once at startup."""
    records = await run_in_db(
        checkpoint_store.reconcile_startup,
        reconciled_at=utc_now_iso(),
    )
    for record in records:
        await _notify_completion(record)
    return records


def fork_task_count() -> int:
    """Expose live cancellation-handle count for lifecycle verification."""
    return len(_tasks)
