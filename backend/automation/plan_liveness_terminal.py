"""Plan-liveness effects that are true only at the final Turn boundary."""

from __future__ import annotations

from automation import plan_liveness as liveness
from automation.continuation.progress import ADVANCED
from automation.turn_end_effects import PendingTurnEndEffects
from infra.database import write_transaction


def _stopped_open(after) -> bool:
    """Return whether the Turn ended with work its own liveness calls movable.

    Armed means actionable open work; a provider reset is F1's wait, and a
    plan that waits only on a watch or dependency never arms.
    """
    return (
        after is not None
        and after["status"] == liveness.ARMED
        and after["provider_reset_at"] is None
    )


def _stopped_below_cut(connection, thread_id: str, after) -> bool:
    """Return whether the Turn ended with open work only below the plan's Later cut.

    That work arms no wake, since the cut never wakes its owner, yet it is
    unfinished, so a delegated worker left holding it has stopped, not returned.
    """
    from plans import public

    return (after is None or after["status"] != liveness.ARMED) and bool(
        public.later_leaves(connection, thread_id)
    )


def withdraw_unread_wake(connection, thread_id: str, *, error) -> None:
    """After an error, withdraw the reminder the Turn never read and back off.

    A reminder delivered into a running Turn steers it at the flat in-Turn
    interval with the cycle held. A Turn blocked in one provider call never
    reads it, so left queued it would start the successor at once, with no
    backoff, on every error. ``finish_turn`` calls it, through
    ``queue_continue_or_withdraw_wake``, inside the terminal write before the
    successor claim, for an error that left no continue input; a person's
    input queued behind it still starts a Turn. A restart keeps its own
    recovery. A provider reset needs no exemption: its wait,
    armed by ``on_thread_turn_complete`` from the text or the harness's
    reset time, replaces this backoff and owns the resumption.
    """
    from infra.queued_input_settlement import cancel_queued_liveness_wakes
    from infra.thread_recovery import RESTART_TURN_ERROR
    from plans.public import liveness_signal

    if str(error or "") == RESTART_TURN_ERROR:
        return
    if not cancel_queued_liveness_wakes(connection, thread_id):
        return
    current = liveness.wake_for(thread_id, connection)
    if current is None or not _stopped_open(current):
        return
    signal = liveness_signal(thread_id, db=connection)
    advanced = signal.count > 0 and liveness._progress(signal, current) == ADVANCED
    liveness.arm_plan_wake(
        thread_id, signal, cycle=0 if advanced else current["cycle"] + 1, db=connection,
    )


def on_thread_turn_complete(
    thread_id: str,
    *,
    outcome: str = "completed",
    error: str | None = None,
    turn_id: str | None = None,
    clears_at: str | None = None,
    db=None,
    effects: PendingTurnEndEffects,
):
    """Apply terminal child and liveness effects through their canonical owners.

    Every harness's terminal handler calls this one seam, native and external.
    The one liveness effect that must precede the successor claim, withdrawing
    an unread reminder after an error, runs earlier in ``withdraw_unread_wake``.
    """
    from automation import provider_reset_liveness
    from plans import public

    with (
        write_transaction(db, scope="plan_thread_turn_complete") as connection,
        public.collect_wake_recipients(connection, effects.drives),
    ):
        public.harvest_ready_children(
            db=connection,
            drives=effects.drives,
            worker_thread_id=thread_id,
            changed_plan_ids=effects.changed_plan_ids,
        )
        if outcome == "error":
            reset = provider_reset_liveness.deadline(error or "", clears_at=clears_at)
            wake = (
                provider_reset_liveness.arm(
                    thread_id, turn_id=turn_id, reset_at=reset, db=connection,
                )
                if reset is not None and turn_id is not None
                else None
            )
            if wake is None:
                cleared_reset = False
                if turn_id is not None and provider_reset_liveness.selection(
                    thread_id, turn_id, "error", connection,
                ) is not None:
                    current = liveness.wake_for(thread_id, connection)
                    if current is not None and current["provider_reset_turn_id"] != turn_id:
                        cleared_reset = liveness.clear_provider_reset(thread_id, db=connection)
                wake = liveness.on_turn_complete(
                    thread_id, cycle=0 if cleared_reset else None, db=connection,
                )
            if turn_id is not None:
                public.notify_turn_error(
                    connection, thread_id, turn_id=turn_id, error=error or "",
                    resumes_at=wake.provider_reset_at if wake is not None else None,
                )
            public.on_worker_turn_end(
                connection, thread_id, changed_plan_ids=effects.changed_plan_ids,
                turn_errored=True,
            )
            return wake
        # Any success proves the provider serves this thread again, whether or
        # not the Turn still carries a selection (debt 26).
        cleared_reset = (
            turn_id is not None
            and provider_reset_liveness.latest_turn(
                thread_id, turn_id, "completed", connection,
            ) is not None
            and liveness.clear_provider_reset(thread_id, db=connection)
        )
        wake = liveness.on_turn_complete(
            thread_id, db=connection, cycle=0 if cleared_reset else None,
        )
        after = liveness.wake_for(thread_id, connection)
        told = public.on_worker_turn_end(
            connection, thread_id, changed_plan_ids=effects.changed_plan_ids,
        )
        if not told and (_stopped_open(after) or _stopped_below_cut(connection, thread_id, after)):
            wake_id = str(after["id"]) if after is not None else f"later:{thread_id}"
            public.notify_stopped_open(connection, thread_id, wake_id=wake_id)
        return wake
