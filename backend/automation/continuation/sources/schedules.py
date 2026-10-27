"""Clock/cadence source for the shared continuation runner."""

from __future__ import annotations

from automation import schedules
from infra.queries import get_thread, thread_gone_for_good, thread_unavailable_reason
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
        due = schedules.due(now, db=db)
        owners = sorted({schedule.thread_id for schedule in due})
        rows = db.execute(
            f"SELECT id, lifecycle_state, archived_reason FROM threads WHERE id IN ({','.join('?' * len(owners))})",
            owners,
        ).fetchall() if owners else []
        threads = {row["id"]: row for row in rows}
        for schedule in due:
            owner = threads.get(schedule.thread_id)
            if thread_unavailable_reason(owner) and not thread_gone_for_good(owner):
                continue
            details = dict(schedule.payload or {})
            if schedule.kind == "cadence" and schedule.runs + 1 >= schedule.max_runs:
                summary = details.get("summary") or "A bounded automation schedule is due."
                details.update({
                    "summary": f"{summary} This is the last of this schedule's {schedule.max_runs} runs; "
                               "it stops after this one. To keep it going, arm a new one with automation_schedule.",
                    "final_run": True, "runs": schedule.runs, "max_runs": schedule.max_runs,
                })
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=schedule.thread_id,
                    scope_id=schedule.scope_id,
                    subject_id=schedule.id,
                    transition=_transition(schedule),
                    reason="schedule_due",
                    details=details,
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
        if outcome == "failed" and thread_gone_for_good(get_thread(trigger.thread_id, db=db)):
            db.execute(
                "UPDATE automation_schedules SET state='cancelled', last_status='owner_gone', updated_at=? "
                "WHERE id=? AND state='active'",
                (trigger.created_at, trigger.subject_id),
            )
        elif outcome in TERMINAL_STATES:
            schedules.settle_run(
                trigger.subject_id,
                trigger.created_at,
                db=db,
            )
