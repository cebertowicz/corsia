"""T0 structured-plan liveness source for the continuation runner."""

from __future__ import annotations

from automation import plan_liveness
from automation.continuation.progress import (
    ADVANCED,
    classify_progress,
    wake_snapshot,
)
from automation.continuation.triggers import Trigger, TriggerRequest
from infra.thread_input_rows import (
    liveness_wake_queued,
    turn_running_before,
    turn_starting_work_pending,
)
from plans.public import liveness_signal


def _scope(thread_id: str, db) -> str:
    row = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if row is None:
        return "__workspace__"
    return row["project_id"] or "__workspace__"


def _transition(wake) -> str:
    return (
        f"cycle:{wake.cycle}:{wake.last_signal_hash}:"
        f"{wake.last_settled_events}:{wake.deadline}:"
        f"{wake.provider_reset_turn_id or ''}"
    )


class LivenessSource:
    """Derive and settle T0 triggers without owning delivery."""

    kind = "t0_liveness"

    def __init__(self, signal_resolver=None) -> None:
        self.signal_resolver = signal_resolver

    def _signal(self, thread_id: str, db):
        if self.signal_resolver is not None:
            return self.signal_resolver(thread_id)
        return liveness_signal(thread_id, db=db)

    def due(self, now: str, *, db) -> list[TriggerRequest]:
        """Return all due T0 requests, including durable terminal outcomes."""

        requests = []
        for wake in plan_liveness.due_plan_wakes(now, db=db):
            # One unread wake per thread: a Turn that outlasts several due
            # times reads one reminder, not a stack of them. The row's deadline
            # alone decides when; this only holds a second unread reminder.
            if liveness_wake_queued(db, wake.thread_id):
                continue
            requests.append(self._request(wake, db, now))
        return requests

    def _request(self, wake, db, now: str) -> TriggerRequest:
        signal = self._signal(wake.thread_id, db)
        outcome = None
        reason = None
        if signal.count <= 0:
            outcome, reason = "cancelled", "no_actionable_work"
        else:
            progress = classify_progress(signal, wake_snapshot(wake))
            reason = f"progress_{progress}"
        return TriggerRequest(
            kind=self.kind,
            thread_id=wake.thread_id,
            scope_id=_scope(wake.thread_id, db),
            subject_id=wake.id,
            transition=_transition(wake),
            outcome=outcome,
            reason=reason,
            details={
                "plan_id": signal.plan_id,
                "node_ids": list(signal.node_ids),
                "actionable_count": signal.count,
                "signal_hash": signal.signal_hash,
                "cycle": wake.cycle,
                "provider_reset_at": wake.provider_reset_at,
            },
        )

    def is_current(
        self,
        thread_id: str,
        subject_id: str | None,
        transition: str | None,
        details: dict,
        *,
        db,
    ) -> bool:
        """Return whether the collected wake still describes actionable work."""

        row = db.execute(
            """SELECT * FROM plan_liveness_wakes
               WHERE id=? AND thread_id=? AND status='armed'""",
            (subject_id, thread_id),
        ).fetchone()
        if row is None:
            return False
        wake = plan_liveness.row_to_wake(dict(row))
        if transition != _transition(wake):
            return False
        if details.get("provider_reset_at"):
            if turn_starting_work_pending(db, thread_id):
                return False
        return self.still_actionable(thread_id, details, db=db)

    def still_actionable(self, thread_id: str, details: dict, *, db) -> bool:
        """Return whether the work this wake was collected for can still move.

        This is the only question a claim may ask. Delivering a wake settles its
        source and re-arms the row, so asking again whether the row still
        matches the transition it was collected at would call a wake stale for
        having been delivered, and the Turn it waited for would never start.
        """
        wake = plan_liveness.wake_for(thread_id, db)
        if wake is not None and wake["status"] == plan_liveness.CANCELLED:
            return False  # its episode ended (Stop, pause, archive): never claimed
        signal = self._signal(thread_id, db)
        return (
            signal.count > 0
            and details.get("plan_id") == signal.plan_id
            and details.get("node_ids") == list(signal.node_ids)
            and details.get("actionable_count") == signal.count
            and details.get("signal_hash") == signal.signal_hash
        )

    def settle(
        self,
        trigger: Trigger,
        outcome: str,
        reason: str,
        *,
        db,
    ) -> None:
        """Project one durable trigger outcome back to the T0 wake store."""

        row = db.execute(
            "SELECT * FROM plan_liveness_wakes WHERE id=? AND status='armed'",
            (trigger.subject_id,),
        ).fetchone()
        if row is None:
            return
        wake = plan_liveness.row_to_wake(dict(row))
        if trigger.dedupe_key != (
            f"{self.kind}:{wake.thread_id}:{wake.id}:{_transition(wake)}"
        ):
            return
        if outcome == "delivered":
            self._settle_delivery(wake, trigger.created_at, db)
        elif outcome in {"cancelled", "stalled"}:
            plan_liveness._claim_fire(wake.id, outcome, db)
        elif outcome == "failed":
            # The trigger records the delivery failure; the wake is terminal
            # too, so no failed delivery can re-arm it or replay indefinitely.
            plan_liveness._claim_fire(wake.id, plan_liveness.STALLED, db)

    def _settle_delivery(self, wake, collected_at: str, db) -> None:
        signal = self._signal(wake.thread_id, db)
        progress = classify_progress(signal, wake_snapshot(wake))
        if turn_running_before(db, wake.thread_id, collected_at):
            # The wake steered a Turn that was already running: the flat
            # in-Turn interval, and the idle cycle does not advance. A wake that
            # started its own Turn is idle spacing, so the backoff still doubles.
            plan_liveness._rearm_due_wake(
                wake, signal, 0 if progress == ADVANCED else wake.cycle, db,
                seconds=plan_liveness.PLAN_WAKE_IN_TURN_SECONDS,
            )
            return
        next_cycle = 0 if progress == ADVANCED else wake.cycle + 1
        plan_liveness._rearm_due_wake(wake, signal, next_cycle, db)
