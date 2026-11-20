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


def _ends_retries(connection, thread_id: str, error: str) -> bool:
    """Whether an error with no provider reset ends the thread's automatic retries.

    A restart keeps its recovery; a provider limit and a head held for its
    model, account or locality keep the wait that owns their resumption. Any
    other error is not retried on a backoff: the thread stays idle until a
    message wakes it, and whoever can act is told at once.
    """
    from infra.thread_recovery import RESTART_TURN_ERROR
    from plans.public import stalled_why
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    if error == RESTART_TURN_ERROR or is_provider_usage_or_quota_limit(error):
        return False
    return stalled_why(connection, thread_id) not in ("model", "account", "locality")


def _runs_again(connection, thread_id: str, turn_id: str | None, error: str) -> bool:
    """Whether the thread runs again with nobody waking it; that run answers for this error.

    A limit's queued continue, the one re-run a person's unanswered message gets,
    or a successor ``finish_turn`` already claimed from input queued behind this
    Turn. If that run fails too, its own end is the one that is told.
    """
    from infra.plan_limit_continue import rerun_queued
    from infra.unanswered_input_wake import will_retry_once

    if turn_id is None:
        return False
    return (
        rerun_queued(turn_id, db=connection)
        or will_retry_once(connection, thread_id, turn_id, error)
        or connection.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=? AND id<>? "
            "AND status IN ('running', 'waiting_retry') LIMIT 1",
            (thread_id, turn_id),
        ).fetchone() is not None
    )


def _tell_coordinator(connection, thread_id: str, *, turn_id: str | None, error: str) -> None:
    """Tell who answers for a standing agent that its Turn failed and it will not retry.

    A project agent's conductor, else the CORSIA agent; the CORSIA agent for a
    workspace agent or a conductor. A recipient that refuses passes it to the
    next; when none takes it, it is parked as a dead letter, never dropped. The
    CORSIA agent's own failure reaches the person as the error row in its chat,
    which Focus shows. A worker's parent is told by the plan's own wake
    (``notify_turn_error``).
    """
    import uuid

    from events import dead_letter
    from events.delivery import enqueue_event_wake, start_idle_after_commit, thread_exists_and_active
    from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator

    row = connection.execute(
        "SELECT title, project_id, COALESCE(agent_enabled, 0) AS standing "
        "FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if row is None or not row["standing"]:
        return
    lead = get_scope_coordinator(WORKSPACE_SCOPE, db=connection)
    if lead == thread_id:
        return
    chain = [get_scope_coordinator(str(row["project_id"]), db=connection)] if row["project_id"] else []
    chain = list(dict.fromkeys(c for c in (*chain, lead) if c and c != thread_id))
    task = connection.execute(
        "SELECT n.title FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
        "WHERE p.owner_kind='thread' AND p.owner_id=? AND n.state='active' "
        "AND n.kind='task' AND n.archived_at IS NULL ORDER BY n.updated_at DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    doing = task["title"] if task is not None else "no active task"
    name = row["title"] or thread_id
    key = turn_id or uuid.uuid4().hex
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia:turn-errored:{thread_id}:{key}"))
    # This Turn was already reported, delivered or parked: a repeat sends nothing new,
    # even if what the agent is working on has changed since.
    if connection.execute(
        "SELECT 1 FROM messages WHERE id=? UNION ALL SELECT 1 FROM dead_letter_events "
        "WHERE internal_event='agent.turn_errored' AND external_id=? LIMIT 1",
        (message_id, message_id),
    ).fetchone() is not None:
        return
    summary = f"{name} ({thread_id}) stopped on an error and will not retry on its own."
    details = {"thread_id": thread_id, "error": error, "working_on": doing,
               "next": "Read it with delegate_status. A delegate_send wakes it."}
    for told in chain:
        if not thread_exists_and_active(told, db=connection):
            continue
        connection.execute("SAVEPOINT turn_errored_notice")
        try:
            enqueue_event_wake(told, "agent.turn_errored", summary, details,
                               message_id=message_id, db=connection)
        except ValueError:
            # The delivery's refusal: this recipient can't take it, so the next one is asked.
            from loguru import logger

            logger.error("turn error of {}: notice to {} refused; trying next", thread_id, told)
            connection.execute("ROLLBACK TO turn_errored_notice")
            connection.execute("RELEASE turn_errored_notice")
            continue
        except Exception:
            # A failure of the delivery itself, not a refusal: park it under its own reason
            # rather than asking the next recipient or calling it unclaimed.
            from loguru import logger

            logger.exception("turn error of {}: notice to {} failed", thread_id, told)
            connection.execute("ROLLBACK TO turn_errored_notice")
            connection.execute("RELEASE turn_errored_notice")
            dead_letter.park("agent.turn_errored", {**details, "summary": summary, "told": told},
                             "turn_error_notice_failed", message_id, db=connection, code_named=True)
            return
        connection.execute("RELEASE turn_errored_notice")
        start_idle_after_commit(connection, told)
        return
    dead_letter.park("agent.turn_errored", {**details, "summary": summary},
                     "no_coordinator_took_turn_error", message_id, db=connection, code_named=True)


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


def _error_without_reset(
    connection, thread_id: str, turn_id: str | None, error: str, *, runs_again: bool,
):
    """An error with no provider reset: end the retries, or keep the wait that owns them."""
    from automation import provider_reset_liveness

    cleared_reset = False
    if turn_id is not None and provider_reset_liveness.selection(
        thread_id, turn_id, "error", connection,
    ) is not None:
        current = liveness.wake_for(thread_id, connection)
        if current is not None and current["provider_reset_turn_id"] != turn_id:
            cleared_reset = liveness.clear_provider_reset(thread_id, db=connection)
    # A replayed callback for a Turn a later one has ended changes nothing.
    if turn_id is not None and connection.execute(
        "SELECT 1 FROM active_turns WHERE id=? AND thread_id=?", (turn_id, thread_id),
    ).fetchone() is not None and provider_reset_liveness.latest_turn(
        thread_id, turn_id, "error", connection,
    ) is None:
        return None
    if _ends_retries(connection, thread_id, error):
        liveness.cancel_plan_wake(thread_id, db=connection)
        if not runs_again:
            _tell_coordinator(connection, thread_id, turn_id=turn_id, error=error)
        return None
    return liveness.on_turn_complete(
        thread_id, cycle=0 if cleared_reset else None, db=connection,
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
            runs_again = _runs_again(connection, thread_id, turn_id, error or "")
            if wake is None:
                wake = _error_without_reset(
                    connection, thread_id, turn_id, error or "", runs_again=runs_again,
                )
            # A reset wait replaces any re-run, and its notice says when it resumes.
            runs_again = runs_again and (wake is None or wake.provider_reset_at is None)
            if turn_id is not None and not runs_again:
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
