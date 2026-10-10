"""Plan-liveness effects that are true only at the final Turn boundary."""

from __future__ import annotations

from automation import plan_liveness as liveness
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
                turn_errored=turn_id is not None,
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
        if not told and after is not None and _stopped_open(after):
            public.notify_stopped_open(
                connection, thread_id, wake_id=str(after["id"]),
            )
        return wake
