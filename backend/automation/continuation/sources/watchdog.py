"""T1 active-node watchdog source for bounded continuation."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from automation.continuation.triggers import Trigger, TriggerRequest
from plans import public

T1_DEFAULT_BUDGET_SECONDS = int(
    os.getenv("CORSIA_T1_DEFAULT_BUDGET_SECONDS", "3600")
)
T1_MAX_CYCLES = 3


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _active_rows(db, node_id: str | None = None):
    node_filter = " AND n.id=?" if node_id is not None else ""
    return db.execute(
        # The watchdog measures from selection: a task active for hours but
        # selected just now has had no time to run.
        """SELECT n.id, n.revision, n.watchdog_budget_seconds,
                  a.id AS attempt_id, a.executor_thread_id,
                  a.selected_at AS active_since
           FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
           JOIN plan_node_attempts a ON a.node_id=n.id
             AND a.released_at IS NULL
             AND a.requirements_revision=n.requirements_revision
           WHERE n.state='active' AND n.archived_at IS NULL
             AND p.state='active'
        """ + node_filter,
        (node_id,) if node_id is not None else (),
    ).fetchall()


def _project_rows(rows, db) -> dict[str, dict]:
    """Batch admission truth for watchdog candidates without per-node reads."""
    node_ids = [str(row["id"]) for row in rows]
    if not node_ids:
        return {}
    marks = ",".join("?" for _ in node_ids)
    nodes = db.execute(
        f"SELECT * FROM plan_nodes WHERE id IN ({marks})",
        tuple(node_ids),
    ).fetchall()
    plan_ids = sorted({str(node["plan_id"]) for node in nodes})
    plan_marks = ",".join("?" for _ in plan_ids)
    plans = {
        str(row["id"]): row
        for row in db.execute(
            f"SELECT * FROM plans WHERE id IN ({plan_marks})",
            tuple(plan_ids),
        ).fetchall()
    }
    return public.project_admission_many(
        [(node, plans[str(node["plan_id"])]) for node in nodes],
        db=db,
    )


def _thread_id(row) -> str:
    return row["executor_thread_id"]


def _scope(thread_id: str, db) -> str:
    row = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    return row["project_id"] if row and row["project_id"] else "__workspace__"


def _cycle(node_id: str, active_since: str, db) -> int:
    return int(
        db.execute(
            """SELECT COUNT(*) FROM continuation_triggers
               WHERE kind='t1_watchdog' AND subject_id=?
                 AND created_at>=? AND state='delivered'""",
            (node_id, active_since),
        ).fetchone()[0]
    )


class WatchdogSource:
    """Derive due watchdog cycles and append audit-only settlements."""

    kind = "t1_watchdog"

    def due(self, now: str, *, db) -> list[TriggerRequest]:
        """Return active nodes whose current watchdog cycle is due."""

        now_dt = _parse(now)
        requests = []
        rows = _active_rows(db)
        projected = _project_rows(rows, db)
        for row in rows:
            if projected[str(row["id"])]["condition"] != "running":
                continue
            thread_id = _thread_id(row)
            active_since = row["active_since"]
            cycle = _cycle(row["id"], active_since, db)
            budget = row["watchdog_budget_seconds"] or T1_DEFAULT_BUDGET_SECONDS
            deadline = _parse(active_since) + timedelta(
                seconds=budget * (2**cycle)
            )
            if deadline > now_dt:
                continue
            transition = f"{active_since}:cycle:{cycle}"
            dedupe_key = f"{self.kind}:{thread_id}:{row['id']}:{transition}"
            if db.execute(
                """SELECT 1 FROM continuation_triggers
                   WHERE kind='t1_watchdog' AND subject_id=? AND dedupe_key=?
                     AND state IN ('pending','deferred','delivered','failed',
                                   'stalled','cancelled','suppressed')
                   LIMIT 1""",
                (row["id"], dedupe_key),
            ).fetchone() is not None:
                continue
            outcome = "stalled" if cycle >= T1_MAX_CYCLES else None
            reason = "cycle_cap_reached" if outcome else "watchdog_due"
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=thread_id,
                    scope_id=_scope(thread_id, db),
                    subject_id=row["id"],
                    transition=transition,
                    outcome=outcome,
                    reason=reason,
                    details={
                        "node_id": row["id"],
                        "node_revision": row["revision"],
                        "attempt_id": row["attempt_id"],
                        "cycle": cycle,
                    },
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
        """Return whether the collected watchdog still targets runnable work."""

        if subject_id is None:
            return False
        row = self._running_row(thread_id, subject_id, db)
        if row is None:
            return False
        cycle = _cycle(subject_id, row["active_since"], db)
        return (
            transition == f"{row['active_since']}:cycle:{cycle}"
            and details.get("cycle") == cycle
            and self._same_work(subject_id, details, row, db)
        )

    def still_actionable(self, thread_id: str, details: dict, *, db) -> bool:
        """Return whether the task this wake was collected for is still running.

        This is the only question a claim may ask. A delivered watchdog records
        its own audit event, which advances the cycle its transition names, and
        that bookkeeping says nothing about whether the task still needs a nudge.
        """
        subject_id = details.get("node_id")
        row = self._running_row(thread_id, subject_id, db)
        return row is not None and self._same_work(subject_id, details, row, db)

    def _running_row(self, thread_id: str, subject_id: str | None, db):
        """Return the node's open attempt row while it is genuinely running."""
        if subject_id is None:
            return None
        rows = _active_rows(db, subject_id)
        row = rows[0] if rows else None
        if row is None or _thread_id(row) != thread_id:
            return None
        if public.project_admission(subject_id, db=db)["condition"] != "running":
            return None
        return row

    def _same_work(self, subject_id, details: dict, row, db) -> bool:
        """Return whether the node and attempt are the ones this wake named."""
        node = public.get_node(subject_id, db=db)
        return (
            details.get("node_id") == subject_id
            and details.get("node_revision") == node.revision
            and details.get("attempt_id") == row["attempt_id"]
        )

    def settle(
        self,
        trigger: Trigger,
        outcome: str,
        reason: str,
        *,
        db,
    ) -> None:
        """Append one idempotent watchdog audit without changing node state."""

        if trigger.subject_id is None or outcome == "deferred":
            return
        event = {
            "stalled": "watchdog_escalated",
            "cancelled": "watchdog_cancelled",
        }.get(outcome, "watchdog_wake")
        public.record_runner_event(
            trigger.subject_id,
            event,
            provenance="continuation.t1",
            payload={
                "trigger_id": trigger.id,
                "outcome": outcome,
                "reason": reason,
            },
            db=db,
        )
