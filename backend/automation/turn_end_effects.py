"""Canonical terminal plan write and its prepared post-commit effects."""

from __future__ import annotations

from dataclasses import dataclass, field

from infra.database import get_db, is_lock_refusal, write_transaction
from infra.db_async import run_in_db
from infra.lock_retry import LockRefused, retry_until_committed
from infra.unanswered_input_wake import queue_unanswered_input_wake
from plans import public


@dataclass
class PendingTurnEndEffects:
    """Facts collected only inside the canonical terminal write transaction."""

    drives: list[str] = field(default_factory=list)
    changed_plan_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TurnEndEffects:
    """Committed terminal effects ready for delivery and parent driving."""

    drives: tuple[str, ...]
    plans_changed: tuple[tuple[str, dict], ...]


def _prepare_committed_effects(pending: PendingTurnEndEffects) -> TurnEndEffects:
    """Resolve delivery facts only after the canonical write has committed."""
    plans_changed = public.prepare_after_commit(pending.changed_plan_ids)
    return TurnEndEffects(tuple(pending.drives), plans_changed)


def _record_turn_end(
    complete, thread_id: str, outcome: dict,
) -> TurnEndEffects | LockRefused:
    """Commit one terminal hook and prepare its effects on the same DB worker.

    Only the write's own refusal is returned for a retry: it is one
    transaction and its effects are collected fresh per attempt, so it left
    nothing behind. A refusal after it committed is raised to the caller,
    since a retry would repeat the write.
    """
    pending = PendingTurnEndEffects()
    try:
        from automation.plan_liveness_terminal import on_thread_turn_complete

        # Plans a write touched without naming (a dependent in another plan)
        # join the one terminal nudge rather than sending their own.
        with public.collect_touched() as touched:
            if complete is on_thread_turn_complete:
                from doceditor.feedback_batches import close_for_turn_end

                db = get_db()
                try:
                    with write_transaction(db, scope="record_turn_end") as connection:
                        liveness_wake = complete(
                            thread_id, effects=pending, db=connection, **outcome,
                        )
                        queue_unanswered_input_wake(
                            connection, thread_id, outcome, liveness_wake, pending,
                        )
                        pending.drives.extend(close_for_turn_end(connection, thread_id))
                        own_plans = public.thread_plan_ids(thread_id, db=connection)
                finally:
                    db.close()
            else:
                complete(thread_id, effects=pending, **outcome)
                db = get_db()
                try:
                    own_plans = public.thread_plan_ids(thread_id, db=db)
                finally:
                    db.close()
        pending.changed_plan_ids.extend(touched)
        # Ending changes what the thread's plan and its parent's row read from
        # Turn facts (running, stalled), whatever the outcome and whether or not
        # the write touched a plan.
        pending.changed_plan_ids.extend(own_plans)
    except Exception as error:
        if is_lock_refusal(error):
            return LockRefused(error)
        raise
    effects = _prepare_committed_effects(pending)
    if outcome.get("outcome", "completed") == "completed":
        from infra.onboarding_events import note_turn_completed

        note_turn_completed(thread_id)
    return effects


async def record_turn_end(complete, thread_id: str, **outcome) -> TurnEndEffects:
    """Commit one terminal plan write and return only prepared effects.

    Pending facts exist only inside ``run_in_db``. The same database worker
    commits the write and resolves the immutable post-commit payload, so no
    caller can receive an unprepared or caller-owned terminal effect.

    A lock refusal of the write is retried until it commits, here, the one
    writer every harness's terminal goes through, so a Turn end is not lost
    to a held lock (#2772). Any other failure, a refusal after the write
    committed included, is the caller's.
    """
    effects = await retry_until_committed(
        lambda: run_in_db(_record_turn_end, complete, thread_id, outcome),
        subject=f"thread {thread_id}",
    )
    from automation import worker_runner

    # Best-effort and never awaited: a Claude Code slot may have freed, a rule's worker may be done.
    worker_runner.poke(if_open=True)
    return effects


async def record_external_turn_end(thread_id: str, **outcome) -> TurnEndEffects:
    """Record one external-harness terminal through the canonical plan hook."""
    from automation.plan_liveness_terminal import on_thread_turn_complete

    return await record_turn_end(
        on_thread_turn_complete,
        thread_id,
        **outcome,
    )


def deliver_turn_end_invalidations(effects: TurnEndEffects) -> None:
    """Deliver invalidations prepared from the committed terminal write."""
    public.deliver_notifications(effects.plans_changed)


async def deliver_turn_end_after(
    effects: TurnEndEffects | None,
    release_terminal,
) -> None:
    """Deliver invalidations, then release before driving parents."""
    if effects is not None:
        deliver_turn_end_invalidations(effects)
    await public.drive_after(
        release_terminal(),
        effects.drives if effects is not None else (),
    )
