from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from loguru import logger as default_logger

from core.stop_causes import PlanLimitStop, frame_cause
from core.turn_core import TerminalSettlement, TurnIdentity
from core.turn_core import active_turns_for_owner
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra import state


OwnsThread = Callable[[dict, str], Awaitable[bool]]
_AppliedStop = tuple[TurnIdentity, TerminalSettlement, tuple[str, ...]]
StopOutcome = Literal[
    "stopped", "already_terminal", "stale_active", "no_active_turn",
]


@dataclass(frozen=True)
class StopReceipt:
    """The authoritative result of one exact requester-initiated Stop."""

    thread_id: str
    outcome: StopOutcome
    turn_id: str | None = None

    def payload(self) -> dict[str, str]:
        payload = {
            "type": "stop_receipt",
            "thread_id": self.thread_id,
            "outcome": self.outcome,
        }
        if self.turn_id is not None:
            payload["turn_id"] = self.turn_id
        return payload


async def resolve_active_turn_id(user: dict, sid: str, *, log=default_logger) -> str | None:
    """Return the owner's one current durable Turn ID for a client-unspecified Stop.

    An omitted client ID means the client missed lifecycle state, not that Stop
    may target an arbitrary replacement turn. Resolve exactly one current
    durable identity, then let every runtime keep its existing exact-ID guard.
    """
    active_turns = await run_in_db(
        active_turns_for_owner,
        user["id"],
        thread_id=sid,
    )
    if len(active_turns) != 1:
        if active_turns:
            log.error(
                "stop refused for {}: expected one active turn, found {}",
                sid,
                len(active_turns),
            )
        return None
    turn_id = active_turns[0]["turn_id"]
    return turn_id if isinstance(turn_id, str) and turn_id else None


def _turn_status(
    user_id: str,
    sid: str,
    turn_id: str,
    *,
    db=None,
) -> str | None:
    owns_db = db is None
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT active_turns.status FROM active_turns "
            "JOIN threads ON threads.id=active_turns.thread_id "
            "WHERE threads.user_id=? AND active_turns.thread_id=? "
            "AND active_turns.id=?",
            (user_id, sid, turn_id),
        ).fetchone()
        return str(row["status"]) if row is not None else None
    finally:
        if owns_db:
            connection.close()


async def _classify_stop_target(
    user: dict,
    sid: str,
    expected_turn_id: str,
) -> StopReceipt | None:
    """Return a no-op receipt unless this exact Turn is still active."""
    active_turns = await run_in_db(
        active_turns_for_owner, user["id"], thread_id=sid,
    )
    if len(active_turns) == 1:
        if active_turns[0]["turn_id"] == expected_turn_id:
            return None
        return StopReceipt(sid, "stale_active", expected_turn_id)
    if active_turns:
        return StopReceipt(sid, "stale_active", expected_turn_id)

    status = await run_in_db(
        _turn_status, user["id"], sid, expected_turn_id,
    )
    if status is not None:
        return StopReceipt(sid, "already_terminal", expected_turn_id)
    return StopReceipt(sid, "no_active_turn")


async def _stop_receipt(
    user: dict,
    sid: str,
    turn_id: str,
    status: str | None,
    runtime_stopped: bool,
) -> StopReceipt:
    """Classify a Stop whose commit found the exact Turn already ended."""
    if not runtime_stopped:
        classified = await _classify_stop_target(user, sid, turn_id)
        if classified is not None:
            return classified
    if status in {"cancelled", "interrupted"}:
        return StopReceipt(sid, "stopped", turn_id)
    if status is None:
        return StopReceipt(sid, "no_active_turn")
    return StopReceipt(sid, "already_terminal", turn_id)


def _commit_stop_terminal(
    user_id: str,
    sid: str,
    turn_id: str,
    *,
    db=None,
    cause: PlanLimitStop | None = None,
) -> tuple[str | None, _AppliedStop | None]:
    """End the owned exact Turn durably unless it has already ended.

    Returns the Turn's status after this transaction and, only when this Stop
    applied the terminal, what its caller publishes and drives. A fenced write
    with no outcome may have reached the provider, so it is recorded
    ambiguous; finishing the Turn records unfenced input cancelled, and every
    write fence requires the Turn to still be running. This is the one place
    every runtime's Stop durably commits, so it is also the one place that
    cancels a plan-liveness wake for the applied cancel, atomically with it;
    a failure there is not swallowed and rolls the cancel back with it rather
    than leaving a wake armed for a Turn that no longer exists.
    """
    from core.turn_core import TurnTerminalResult, persist_turn_settlement
    from infra.thread_recovery import ambiguous_stale_writes

    with write_transaction(db, scope="stop_turn_terminal") as connection:
        row = connection.execute(
            "SELECT a.status,a.turn_order FROM active_turns a JOIN threads t "
            "ON t.id=a.thread_id WHERE t.user_id=? AND a.thread_id=? AND a.id=?",
            (user_id, sid, turn_id),
        ).fetchone()
        if row is None or row["status"] != "running":
            return (None if row is None else str(row["status"])), None
        ambiguous_stale_writes(
            connection, turn_id, error="Stop ended the turn after provider dispatch",
        )
        # A user Stop must not restart the agent on a plan wake, armed or
        # already queued. Both end here, before the terminal claims a successor,
        # so no claim (now, or after a restart or admission hold lifts) can run
        # the queued one. A steer never commits here. A plan-limit pause is not
        # a user Stop: its wake survives, held by admission.
        if cause is None:
            from automation import plan_liveness
            from infra.queued_input_settlement import cancel_queued_liveness_wakes

            plan_liveness.cancel_plan_wake(sid, db=connection)
            cancel_queued_liveness_wakes(connection, sid)
        settlement = persist_turn_settlement(turn_id, "cancelled", cause, db=connection)
        if settlement.result is not TurnTerminalResult.APPLIED:
            return _turn_status(user_id, sid, turn_id, db=connection), None
        inputs = tuple(str(item["id"]) for item in connection.execute(
            "SELECT id FROM messages WHERE active_turn_id=? ORDER BY input_order",
            (turn_id,),
        ))
        identity = TurnIdentity(turn_id, int(row["turn_order"]))
        return "cancelled", (identity, settlement, inputs)


async def _stop_exact_turn(
    user: dict,
    sid: str,
    turn_event: asyncio.Event | None,
    llm_task: asyncio.Task | None,
    turn_id: str,
    *,
    log,
    cause: PlanLimitStop | None = None,
) -> StopReceipt:
    """Give the runtime owner one bounded chance, then end the Turn durably."""
    from subagents import live_registry
    from ws.ingress import track_background_task
    from ws.stop_runtime import retire_after_stop, stop_runtimes

    bound = live_registry.STOP_LOCK_TIMEOUT_S
    deadline = asyncio.get_running_loop().time() + bound
    broadcast = state.owner_broadcast(user["id"])
    runtime_stop = asyncio.create_task(stop_runtimes(
        sid, turn_event, llm_task, turn_id, broadcast, log=log, cause=cause,
    ))
    track_background_task(runtime_stop)
    await asyncio.wait({runtime_stop}, timeout=bound)
    runtime_stopped = (
        runtime_stop.done() and not runtime_stop.cancelled() and runtime_stop.result()
    )
    status, applied = await run_in_db(
        _commit_stop_terminal, user["id"], sid, turn_id, cause=cause,
    )
    if applied is None:  # Ended first, or its runtime owner committed it; nothing more to write.
        return await _stop_receipt(user, sid, turn_id, status, runtime_stopped)
    identity, settlement, inputs = applied
    state.turn_buffers.clear(sid, identity)
    try:
        state.broadcast_event(sid, user["id"], {
            "type": "turn_cancelled", "thread_id": sid,
            **identity.frame_fields(), **frame_cause(cause),
        })
    finally:
        track_background_task(asyncio.create_task(
            retire_after_stop(
                sid, turn_id, runtime_stop, settlement, inputs, deadline, broadcast,
            ),
            name=f"stop-settlement:{turn_id}",
        ))
    return StopReceipt(sid, "stopped", turn_id)


def _thread_owner(sid: str) -> str | None:
    connection = get_db()
    try:
        row = connection.execute("SELECT user_id FROM threads WHERE id=?", (sid,)).fetchone()
        return str(row["user_id"]) if row is not None else None
    finally:
        connection.close()


async def stop_turn_for_cause(sid: str, turn_id: str) -> StopReceipt:
    """The system caller of Stop: hold one exact Turn at the plan limit.

    It has no requester, so no ownership check: never reach it from a ws or
    API handler. It shares ``_stop_exact_turn`` with user Stop, so the cancel
    keeps one writer, lock and deadline, and never interrupts a harness
    directly. The limited verdict is already committed, so the successor
    claim in this Stop's terminal holds instead of spending.
    """
    from subagents.runtime_guard import join_task_through_cancellation

    user_id = await run_in_db(_thread_owner, sid)
    if user_id is None:
        return StopReceipt(sid, "no_active_turn")
    task = asyncio.create_task(_stop_exact_turn(
        {"id": user_id}, sid, state._active_turns.get(sid),
        state._active_llm_tasks.get(sid), turn_id,
        log=default_logger, cause=PlanLimitStop(),
    ))
    cancellation = await join_task_through_cancellation(task)
    receipt = task.result()
    if cancellation is not None:
        raise cancellation
    return receipt


async def do_stop(
    user: dict,
    sid: str,
    turn_event: asyncio.Event | None,
    llm_task: asyncio.Task | None,
    expected_turn_id: str,
    *,
    owns_thread_fn: OwnsThread,
    log=default_logger,
) -> StopReceipt:
    """End the exact Turn and return a requester-visible receipt.

    Runtime owners get one bounded chance to end it themselves; Stop then
    always commits the durable cancel. Only a failed commit raises.
    """
    from subagents.runtime_guard import join_task_through_cancellation

    if not await owns_thread_fn(user, sid):
        return StopReceipt(sid, "no_active_turn")
    # Runtime owners perform their own exact-ID comparison. Do not preflight
    # against a durable snapshot here: a successor can appear after it, while a
    # just-spawned exact owner can briefly precede its durable admission.
    task = asyncio.create_task(_stop_exact_turn(
        user, sid, turn_event, llm_task, expected_turn_id, log=log,
    ))
    cancellation = await join_task_through_cancellation(task)
    receipt = task.result()
    if cancellation is not None:
        raise cancellation
    return receipt
