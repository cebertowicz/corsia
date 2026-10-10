"""Clock/cadence source for the shared continuation runner."""

from __future__ import annotations

from automation import schedules
from automation.continuation.triggers import (
    TERMINAL_STATES,
    Trigger,
    TriggerRequest,
)


def _transition(schedule) -> str:
    return f"run:{schedule.runs + 1}:{schedule.next_run_at}"


class ScheduleSource:
    """Derive bounded automation triggers from durable schedules."""

    kind = "automation_clock"

    def due(self, now: str, *, db) -> list[TriggerRequest]:
        requests = []
        for schedule in schedules.due(now, db=db):
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=schedule.thread_id,
                    scope_id=schedule.scope_id,
                    subject_id=schedule.id,
                    transition=_transition(schedule),
                    reason="schedule_due",
                    details=schedule.payload,
                )
            )
        return requests

    def is_current(
        self,
        thread_id: str,
        subject_id: str | None,
        transition: str | None,
        details: dict,
        *,
        db,
    ) -> bool:
        """Return whether the due occurrence is still this thread's at append."""
        schedule = self._owned(thread_id, details, db)
        return (
            schedule is not None
            and schedule.id == subject_id
            and schedule.state == "active"
            and transition == _transition(schedule)
        )

    def still_actionable(self, thread_id: str, details: dict, *, db) -> bool:
        """Return whether the occurrence still stands for the thread claiming it.

        Delivery settles the run, and on the last run exhausts the schedule, so
        neither the run count nor an exhausted state says the wake is stale. A
        schedule its actor cancelled does.
        """
        schedule = self._owned(thread_id, details, db)
        return schedule is not None and schedule.state != "cancelled"

    def _owned(self, thread_id: str, details: dict, db):
        try:
            schedule = schedules.get(str(details.get("subject_id")), db=db)
        except KeyError:
            return None
        return schedule if schedule.thread_id == thread_id else None

    def settle(
        self,
        trigger: Trigger,
        outcome: str,
        _reason: str,
        *,
        db,
    ) -> None:
        """Count every terminal occurrence as one run, whether or not it ran.

        A stalled, cancelled or suppressed occurrence is a missed run: it
        advances a cadence exactly as a failed delivery does, so one busy
        hour cannot end a recurring schedule. Only ``max_runs`` or a one-shot
        clock exhausts it (#1612).
        """
        if outcome in TERMINAL_STATES:
            schedules.settle_run(
                trigger.subject_id,
                trigger.created_at,
                db=db,
            )
