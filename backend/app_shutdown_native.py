"""Shut down Native turn owners against an exact handle snapshot.

Native turns are tracked across three registries in ``infra.state``, so
teardown snapshots all three per thread and removes only the exact handles
it joined: a turn that a concurrent owner replaced mid-shutdown must keep
its new handles.
"""

import asyncio

from app_shutdown_join import join_cleanup, join_cleanup_owner
from infra import state


def remaining_native_owners() -> tuple[str, ...]:
    """Snapshot Native owners that exact task cleanup did not prove done."""
    return tuple(dict.fromkeys((
        *state._active_turns,
        *state._active_llm_tasks,
        *state._active_turn_tasks,
    )))


def _snapshot_is_current(thread_id: str, snapshot: tuple) -> bool:
    cancel_handle, task, llm_task = snapshot
    return (
        state._active_turns.get(thread_id) is cancel_handle
        and state._active_turn_tasks.get(thread_id) is task
        and state._active_llm_tasks.get(thread_id) is llm_task
    )


async def _cancel_snapshot(thread_id: str, snapshot: tuple, cancel_and_join):
    if snapshot[0] is None or not _snapshot_is_current(thread_id, snapshot):
        return False, None, False
    result, pending_cancel = await join_cleanup_owner(cancel_and_join(thread_id))
    return result, pending_cancel, True


def _log_shutdown_result(
    thread_id: str,
    cancel_handle,
    snapshot_was_current: bool,
    result,
    logger,
) -> None:
    if cancel_handle is None:
        logger.warning(f"shutdown: native turn {thread_id} had no cancel handle")
    elif not snapshot_was_current:
        logger.warning(f"shutdown: native turn owner changed for {thread_id}")
    elif result is False:
        logger.warning(f"shutdown: native turn {thread_id} had no cancel handle")
    elif isinstance(result, asyncio.CancelledError):
        logger.warning(f"shutdown: native turn cleanup cancelled for {thread_id}")
    elif isinstance(result, BaseException):
        logger.warning(f"shutdown: native turn cleanup failed for {thread_id}: {result}")


async def _join_residue(thread_id: str, snapshot: tuple, logger):
    residue = []
    for candidate in snapshot[1:]:
        if (
            candidate is None
            or not hasattr(candidate, "__await__")
            or any(candidate is tracked for tracked in residue)
        ):
            continue
        residue.append(candidate)
        if not candidate.done():
            candidate.cancel()
    results, pending_cancel = await join_cleanup(residue)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError,
        ):
            logger.warning(
                f"shutdown: native task cleanup failed for {thread_id}: {result}"
            )
    return residue, pending_cancel


def _drop_snapshot(
    thread_id: str,
    snapshot: tuple,
    *,
    stopped: bool,
    snapshot_was_current: bool,
    residue: list,
) -> None:
    cancel_handle, task, llm_task = snapshot
    snapshot_still_current = (
        snapshot_was_current and _snapshot_is_current(thread_id, snapshot)
    )
    turn_done = stopped or any(task is owned for owned in residue)
    llm_done = stopped or any(llm_task is owned for owned in residue)
    if turn_done and task is not None and state._active_turn_tasks.get(thread_id) is task:
        state._active_turn_tasks.pop(thread_id, None)
    if llm_done and llm_task is not None and state._active_llm_tasks.get(thread_id) is llm_task:
        state._active_llm_tasks.pop(thread_id, None)
    if (
        turn_done
        and cancel_handle is not None
        and snapshot_still_current
    ):
        state._active_turns.pop(thread_id, None)


async def shutdown_native_turn(
    thread_id: str,
    snapshot: tuple[object | None, object | None, object | None],
    cancel_and_join,
    logger,
):
    """Join one snapshotted Native owner and remove only exact handles."""
    native_result, pending_cancel, snapshot_was_current = await _cancel_snapshot(
        thread_id, snapshot, cancel_and_join,
    )
    _log_shutdown_result(
        thread_id, snapshot[0], snapshot_was_current, native_result, logger,
    )
    residue, residue_cancel = await _join_residue(thread_id, snapshot, logger)
    _drop_snapshot(
        thread_id,
        snapshot,
        stopped=native_result is True,
        snapshot_was_current=snapshot_was_current,
        residue=residue,
    )
    return pending_cancel or residue_cancel


async def shutdown_native_turns(logger):
    """Join the exact Native owner snapshot, including task-only residue."""
    session_ids = remaining_native_owners()
    snapshots = {
        thread_id: (
            state._active_turns.get(thread_id),
            state._active_turn_tasks.get(thread_id),
            state._active_llm_tasks.get(thread_id),
        )
        for thread_id in session_ids
    }
    try:
        from core.native_turn_control import cancel_and_join_native_turn
    except Exception as exc:
        logger.warning(f"shutdown: native turn cleanup unavailable: {exc}")
        return None

    pending_cancel = None
    for thread_id, snapshot in snapshots.items():
        turn_cancel = await shutdown_native_turn(
            thread_id, snapshot, cancel_and_join_native_turn, logger,
        )
        pending_cancel = pending_cancel or turn_cancel
    return pending_cancel
