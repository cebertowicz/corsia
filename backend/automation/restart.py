"""Graceful restart drain over exact active Turns."""

from __future__ import annotations

import asyncio

from loguru import logger

from automation.restart_messages import RESTART_DRAIN_REFUSAL, TURN_STOPPED_REFUSAL
from automation.restart_resumption import idle_threads_with_held_input, resume_threads_detached
from infra.database import get_db, write_transaction

RESTART_PENDING_MESSAGE = (
    "A system restart is pending. Finish at the next safe boundary. If unfinished "
    "work must continue after startup, call restart_resume now."
)
RESTART_CANCELLED_MESSAGE = (
    "The pending restart was cancelled. Continue on this instance."
)
STEER_TIMEOUT_SECONDS = 4.0

# Detached resume tasks are held here only so the event loop cannot garbage
# collect a task that is still running; each entry removes itself when done.
_RESUME_TASKS: set[asyncio.Task] = set()


def _execution_class(row) -> str:
    from infra.execution_class import execution_class
    from llm.resolution import ModelUnavailableError

    try:
        return execution_class(row)
    except (ModelUnavailableError, ValueError):
        return "unresolved"


def _snapshot_active_turns() -> list[dict]:
    connection = get_db()
    try:
        rows = connection.execute(
            "SELECT t.id AS turn_id,t.turn_order,t.thread_id,s.* "
            "FROM active_turns AS t JOIN threads AS s ON s.id=t.thread_id "
            "WHERE t.status='running' "
            "ORDER BY t.turn_order"
        ).fetchall()
        return [{
            "turn_id": str(row["turn_id"]),
            "turn_order": int(row["turn_order"]),
            "thread_id": str(row["thread_id"]),
            "execution_class": _execution_class(row),
        } for row in rows]
    finally:
        connection.close()


def _turn_is_active(turn: dict) -> bool:
    connection = get_db()
    try:
        return connection.execute(
            "SELECT 1 FROM active_turns WHERE id=? AND thread_id=? "
            "AND status='running'",
            (turn["turn_id"], turn["thread_id"]),
        ).fetchone() is not None
    finally:
        connection.close()


async def _steer_turn(turn: dict, message: str) -> bool:
    execution = turn["execution_class"]
    if execution == "unresolved":
        return False
    if execution == "native":
        from core.native_turn_control import request_native_restart

        return request_native_restart(
            turn["thread_id"], turn["turn_id"], message,
        )
    if execution.startswith("cc_"):
        from subagents.cc_runtime import steer_restart_pending

        return await steer_restart_pending(
            turn["thread_id"], turn["turn_id"], message,
        )
    from subagents.codex_runtime import steer_restart_pending

    return await steer_restart_pending(
        turn["thread_id"], turn["turn_id"], message,
    )


def _restart_operation(reason: str, operation_id: str | None) -> str:
    from automation import self_restart

    if operation_id is not None:
        operation = self_restart.in_flight_request()
        if operation is None or str(operation["id"]) != operation_id:
            raise RuntimeError("restart operation disappeared before drain")
        return operation_id
    acquired = self_restart.try_acquire(None, None, None, None, reason)
    if acquired is None:
        raise RuntimeError("could not acquire restart cutoff")
    return acquired


def _enter_steering(operation_id: str) -> None:
    from automation import self_restart

    operation = self_restart.in_flight_request()
    if operation is None or str(operation["id"]) != operation_id:
        raise RuntimeError("restart operation changed before steering")
    phase = str(operation.get("phase") or "holding")
    if phase == "holding" and self_restart.transition_operation(
        operation_id, expected="holding", target="steering",
    ):
        return
    if phase != "steering":
        raise RuntimeError(f"restart operation cannot drain from {phase}")


async def _steer_one(turn: dict) -> tuple[str, bool]:
    """Return one provider-owned ACK/refusal without leaking provider failures."""
    try:
        accepted = await _steer_turn(turn, RESTART_PENDING_MESSAGE)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("restart steer failed for {}", turn["thread_id"])
        accepted = False
    return turn["turn_id"], accepted


async def _cancel_and_settle_steering(tasks) -> None:
    """Cancel and await every provider task still owned by this steering pass."""
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _steer_turns(turns: list[dict]) -> dict[str, bool]:
    """Resolve concurrent provider decisions under one whole-call bound."""
    tasks = {
        asyncio.create_task(_steer_one(turn)): turn
        for turn in turns
    }
    if not tasks:
        return {}
    try:
        done, pending = await asyncio.wait(tasks, timeout=STEER_TIMEOUT_SECONDS)
        await _cancel_and_settle_steering(pending)
    except BaseException:
        await _cancel_and_settle_steering(tasks)
        raise
    outcomes = {
        tasks[task]["turn_id"]: task.result()[1]
        for task in done
    }
    outcomes.update({tasks[task]["turn_id"]: False for task in pending})
    return outcomes


async def begin_restart_drain(
    reason: str,
    *,
    operation_id: str | None = None,
    exclude_thread_ids: set[str] | None = None,
    include_thread_ids: set[str] | None = None,
    allow_hard_cap: bool = False,
) -> dict:
    """Hold ingress and ask every captured Turn to stop at its safe boundary."""
    from automation import self_restart
    from infra import drain

    operation_id = _restart_operation(reason, operation_id)
    try:
        _enter_steering(operation_id)
        drain.start_drain_projection(reason)
        excluded = exclude_thread_ids or set()
        turns = [
            turn for turn in _snapshot_active_turns()
            if turn["thread_id"] not in excluded
            and (
                include_thread_ids is None
                or turn["thread_id"] in include_thread_ids
            )
        ]
        self_restart.record_drain_total(operation_id, len(turns))
        outcomes = await _steer_turns(turns)
        controlled = [
            turn["thread_id"] for turn in turns if outcomes[turn["turn_id"]]
        ]
        uncontrolled = [
            turn["thread_id"] for turn in turns if not outcomes[turn["turn_id"]]
        ]
        if uncontrolled and not allow_hard_cap:
            raise RuntimeError(
                "restart control was not accepted by active task(s): "
                + ",".join(uncontrolled)
            )
        if not self_restart.transition_operation(
            operation_id, expected="steering", target="draining",
        ):
            raise RuntimeError("restart operation changed before steering settled")
        return {
            **drain.get_drain_status(),
            "operation_id": operation_id,
            "controlled": controlled,
            "accounted": [],
            "uncontrolled": uncontrolled,
            "excluded_thread_ids": sorted(excluded),
        }
    except asyncio.CancelledError:
        await abort_restart_control(
            operation_id,
            error="restart control cancelled during steering",
            cancelled=True,
            failure_code="drain_cancelled",
        )
        raise
    except Exception as exc:
        await abort_restart_control(
            operation_id,
            error="restart control setup failed",
            failure_code="drain_failed",
            failure_detail=f'{{"exception": "{type(exc).__name__}"}}',
        )
        raise


async def abort_restart_control(
    operation_id: str,
    *,
    error: str = "restart cancelled",
    cancelled: bool = False,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> bool:
    """Release ingress and continue every explicitly registered task locally.

    This is itself the compensation for a cancelled restart, so a cancellation
    that arrives mid-release must not strand the threads the hold kept: the
    committed release, the projection clear and the resume finish as one
    unit, and only then does the cancellation reach the caller.
    """
    from subagents.runtime_guard import join_task_through_cancellation

    release = asyncio.create_task(
        _release_and_resume(operation_id, error, cancelled, failure_code, failure_detail)
    )
    cancellation = await join_task_through_cancellation(release)
    released = release.result()
    if cancellation is not None:
        raise cancellation
    return released


async def _release_and_resume(
    operation_id: str,
    error: str,
    cancelled: bool,
    failure_code: str | None,
    failure_detail: str | None,
) -> bool:
    from automation.update_failure_notice import tell_conductor_detached
    from infra import drain
    from infra.db_async import run_in_db
    from infra.thread_execution import drive_thread

    threads = await run_in_db(
        _release_restart_control, operation_id, error, cancelled, failure_code, failure_detail
    )
    if threads is None:
        return False
    drain.cancel_drain_projection()
    resume_threads_detached(threads, drive_thread, _RESUME_TASKS)
    tell_conductor_detached(operation_id, _RESUME_TASKS)
    return True


def _release_restart_control(
    operation_id: str,
    error: str,
    cancelled: bool,
    failure_code: str | None,
    failure_detail: str | None,
) -> tuple[str, ...] | None:
    """Release the operation's hold and its wake registrations; None when it was not ours to release."""
    from automation import self_restart
    from automation.restart_wakes import cancel_operation_registrations

    with write_transaction(None, scope="abort_restart_control") as connection:
        released = self_restart.abort_operation(
            operation_id,
            error=error,
            cancelled=cancelled,
            failure_code=failure_code,
            failure_detail=failure_detail,
            db=connection,
        )
        if not released:
            return None
        woken = cancel_operation_registrations(operation_id, db=connection)
        return tuple(dict.fromkeys((*woken, *idle_threads_with_held_input(connection))))


def advance_restart_readiness(operation_id: str) -> bool:
    from automation import self_restart
    from infra.drain import check_drain_ready

    operation = self_restart.in_flight_request()
    if operation is None or str(operation["id"]) != operation_id:
        return False
    if not check_drain_ready():
        return False
    phase = str(operation.get("phase") or "")
    return phase == "ready" or (
        phase == "draining"
        and self_restart.transition_operation(
            operation_id, expected="draining", target="ready",
        )
    )


def mark_restarting(operation_id: str) -> bool:
    from automation import self_restart

    operation = self_restart.in_flight_request()
    if operation is None or str(operation["id"]) != operation_id:
        return False
    phase = str(operation.get("phase") or "")
    return phase == "restarting" or (
        phase == "ready"
        and self_restart.transition_operation(
            operation_id, expected="ready", target="restarting",
        )
    )


async def _force_stop_turn(turn: dict) -> bool:
    execution = turn["execution_class"]
    if execution == "unresolved":
        return False
    if execution == "native":
        from core.native_turn_control import cancel_and_join_native_turn
        from core.turn_core import TurnIdentity

        return await cancel_and_join_native_turn(
            turn["thread_id"],
            expected_turn=TurnIdentity(turn["turn_id"], turn["turn_order"]),
        )
    if execution.startswith("cc_"):
        from subagents.cc_runtime import stop
    else:
        from subagents.codex_runtime import stop
    return await stop(
        turn["thread_id"], expected_turn_id=turn["turn_id"],
    )


async def enforce_restart_hard_cap(operation_id: str) -> dict:
    """Persist continuation intent before exact provider cancellation."""
    from automation import self_restart
    from automation.restart_wakes import register_hard_recovery

    operation = self_restart.in_flight_request()
    if operation is None or str(operation["id"]) != operation_id:
        raise RuntimeError("hard cap lost the restart operation")
    phase = str(operation.get("phase") or "")
    if phase == "steering":
        raise RuntimeError("restart steering is still pending")
    if phase not in {"draining", "ready"}:
        raise RuntimeError(f"hard cap cannot run from {phase}")
    failures = []
    for turn in _snapshot_active_turns():
        register_hard_recovery(turn["thread_id"], turn["turn_id"])
        try:
            stopped = await _force_stop_turn(turn)
        except Exception:
            logger.exception("restart hard cap could not stop {}", turn["thread_id"])
            stopped = False
        if not stopped and _turn_is_active(turn):
            failures.append(f"{turn['thread_id']}:stop")
    if failures:
        logger.warning(
            "restart hard cap force-stopping past unstoppable task(s): {}",
            ",".join(failures),
        )
    # Readiness is owned by durable recovery, not by a cooperative stop.
    # register_hard_recovery has already persisted continuation intent for every
    # captured Turn above, so the work survives the coming process death whether
    # or not the in-process stop landed. Refusing here protected nothing and
    # denied the operator their stop, which is the one thing a hard cap exists
    # to guarantee. The failures stay in the answer so the caller can report
    # exactly which tasks were force-terminated.
    if phase == "draining":
        self_restart.transition_operation(
            operation_id, expected="draining", target="ready",
        )
    return {"force_stop_ready": True, "failures": failures}
