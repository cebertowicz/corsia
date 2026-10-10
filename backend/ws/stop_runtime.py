"""Runtime-owner side of Stop: ask owners to end a Turn, then retire them."""

from __future__ import annotations

import asyncio

from loguru import logger as default_logger

from core.stop_causes import PlanLimitStop
from core.turn_core import TerminalSettlement, TurnIdentity
from infra import state


def _captured_native_identity(
    sid: str,
    turn_event: asyncio.Event | None,
) -> tuple[TurnIdentity | None, BaseException | None]:
    """Validate the exact Native cancellation handle captured at ingress."""
    if turn_event is None or state._active_turns.get(sid) is not turn_event:
        return None, None
    try:
        identity = getattr(turn_event, "turn_identity")
    except (AttributeError, RuntimeError):
        return None, RuntimeError(
            f"active turn on session {sid} has no exact turn identity"
        )
    if not isinstance(identity, TurnIdentity):
        return None, RuntimeError(
            f"active turn on session {sid} has no exact turn identity"
        )
    return identity, None


async def _stop_captured_native(
    sid: str,
    turn_event: asyncio.Event | None,
    llm_task: asyncio.Task | None,
    expected_turn_id: str,
    *,
    log=default_logger,
) -> tuple[bool, BaseException | None]:
    """Cancel and settle only the exact Native Turn captured at ingress."""
    identity, first_failure = _captured_native_identity(sid, turn_event)
    if identity is None or identity.turn_id != expected_turn_id:
        return False, first_failure
    if turn_event is None:
        return False, first_failure

    warm_task = state._active_warm_tasks.get(sid)
    if warm_task is not None and not warm_task.done():
        warm_task.cancel()
    turn_event.user_stop_requested = True
    turn_event.set()
    if llm_task is not None and state._active_llm_tasks.get(sid) is llm_task:
        llm_task.cancel()

    from core.native_turn_control import cancel_and_join_native_turn

    try:
        await cancel_and_join_native_turn(
            sid,
            expected_turn=identity,
            replacement_requested=False,
        )
    except BaseException as exc:
        first_failure = first_failure or exc
        log.exception(f"_do_stop: Native turn teardown raised for {sid}")

    from core.thread_warm import cancel_thread_warm

    if warm_task is not None:
        try:
            await cancel_thread_warm(sid, expected_task=warm_task)
        except BaseException as exc:
            first_failure = first_failure or exc
            log.exception(f"_do_stop: warm cleanup raised for {sid}")
    return True, first_failure


async def stop_runtimes(
    sid: str,
    turn_event: asyncio.Event | None,
    llm_task: asyncio.Task | None,
    turn_id: str,
    broadcast,
    *,
    log=default_logger,
    cause: PlanLimitStop | None = None,
) -> bool:
    """Ask every runtime owner to end the exact Turn; log failures, never raise.

    A system ``cause`` reaches each harness runtime's own terminal commit.
    """
    from doceditor.live_preview import previews
    from subagents import cc_runtime, codex_runtime

    # Stop removes the Turn's edit previews at once, before any runtime has wound down.
    previews.end_where("stopped", turn_id=turn_id)
    stopped = False
    # A user Stop keeps its exact call; only a system Stop names a cause.
    caused = {} if cause is None else {"cause": cause}
    try:
        stopped, failure = await _stop_captured_native(
            sid, turn_event, llm_task, turn_id, log=log,
        )
        if cause is not None and stopped:  # Plan limits exist only on harness connections.
            log.error(f"_do_stop: plan-limit Stop of {sid} reached a Native owner")
        if failure is not None and not stopped:
            log.error(f"_do_stop: Native owner of {sid} is not exact: {failure}")
    except BaseException:
        log.exception(f"_do_stop: Native stop raised for {sid}")
    for name, runtime in (("cc_runtime", cc_runtime), ("codex_runtime", codex_runtime)):
        if not runtime.has_pending_or_live(sid):
            continue
        try:
            stopped = await runtime.stop(
                sid, expected_turn_id=turn_id, broadcast=broadcast, **caused,
            ) or stopped
        except BaseException:
            log.exception(f"_do_stop: {name}.stop raised for {sid}")
    return stopped


def _harness_registries():
    from subagents import cc_runtime, codex_runtime

    return (("cc_runtime", cc_runtime._LIVE), ("codex_runtime", codex_runtime._LIVE))


async def _transition_locks_released(sid: str) -> bool:
    """Wait out each held transition lock; False if one outlives its bound."""
    from subagents.live_registry import acquire_stop_lock

    for name, registry in _harness_registries():
        lock = registry.existing_lock(sid)
        if lock is None or not lock.locked():
            continue
        if not await acquire_stop_lock(lock, thread_id=sid, runtime=name):
            return False
        lock.release()
    return True


def _handle_bound_to(sid: str, turn_id: str) -> bool:
    for _name, registry in _harness_registries():
        identity = getattr(registry.get(sid), "corsia_turn", None)
        if identity is not None and identity.turn_id == turn_id:
            return True
    return False


async def _retire_stop_owners(
    sid: str,
    turn_id: str,
    runtime_stop: asyncio.Task,
    broadcast,
) -> bool:
    """Stop whatever still owns the Turn; True once nothing does.

    A runtime Stop that failed fast proves nothing, so the pass also waits
    for every held transition lock and rechecks the registered handles.
    """
    await asyncio.wait({runtime_stop})
    await stop_runtimes(
        sid, state._active_turns.get(sid), state._active_llm_tasks.get(sid),
        turn_id, broadcast,
    )
    released = await _transition_locks_released(sid)
    return released and not _handle_bound_to(sid, turn_id)


def _abandon_stop_owners(sid: str, turn_id: str, broadcast) -> None:
    """Detach every owner still bound to the Turn and kill its process."""
    from subagents.cc_stop import abandon_stalled_stop as abandon_cc
    from subagents.codex_turn_transitions import abandon_stalled_stop as abandon_codex
    from subagents.runtime_guard import ensure_terminated
    from ws.ingress import track_background_task

    for session in (abandon_cc(sid, turn_id, broadcast), abandon_codex(sid, turn_id)):
        if session is not None:
            track_background_task(asyncio.create_task(ensure_terminated(session)))


async def retire_after_stop(
    sid: str,
    turn_id: str,
    runtime_stop: asyncio.Task,
    settlement: TerminalSettlement,
    inputs: tuple[str, ...],
    deadline: float,
    broadcast,
) -> None:
    """Retire the Turn's runtime within Stop's one deadline, then drive its successor.

    Only an applied Stop commit runs this, so the owner did not end the Turn
    itself. The deadline is total: whatever still owns the Turn when it
    expires, or when retirement ends without clearing it, is abandoned, and
    the successor is driven either way.
    """
    from core.turn_core import drive_turn_settlement
    from infra.send_broadcast import broadcast_message_rows
    from ws.ingress import track_background_task

    retirement = asyncio.create_task(
        _retire_stop_owners(sid, turn_id, runtime_stop, broadcast),
    )
    track_background_task(retirement)
    try:
        await broadcast_message_rows(sid, inputs)
        remaining = deadline - asyncio.get_running_loop().time()
        done, _pending = await asyncio.wait({retirement}, timeout=max(0.0, remaining))
        if (
            not done or retirement.cancelled()
            or retirement.exception() is not None or not retirement.result()
        ):
            default_logger.error(
                f"stop {sid}: owner of {turn_id} was not retired; abandoning it"
            )
            _abandon_stop_owners(sid, turn_id, broadcast)
    finally:
        await drive_turn_settlement(settlement)
