"""Schema 59 -> 60: every armed event wait on open plan work gets a deadline.

Every event wait ends now (``plans.event_wait``, W4 of
``shared/planning/plans-work-never-stops-2026-09-28.md``): 24 hours by default,
7 days at most. Until then a task could wait on an artifact's event alone, and
an event that never came left the task with nothing that would ever look at it
again. This gives each such wait the end it would have had.

What moves: an armed watch whose condition is not a deadline, bound to an
open, unarchived task on an unarchived plan a thread owns, while that thread
still exists (schema 58 archived gone owners' plans; one gone since is
skipped: a deadline would wake no one). A settled or archived task's wait, a
watch no plan holds, and one that already has a deadline are left alone.

Each watch keeps its id, artifact and event, so the event still ends it and an
agent holding the id can still resolve it (``plans.event_wait.give_deadline``,
through ``automation.watch_deadline``). Deadlines are spread evenly over the 7
days after this step runs, oldest wait first, the first more than an hour out,
so they do not all wake at once. Every node event is written under this
step's provenance, so no owner or parent is told
(``plans.plan_events.from_schema_step``). Rerunning finds nothing left to move.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

_PROVENANCE = "migration.59_to_60"
_FIRST = timedelta(hours=1)
_SPREAD = timedelta(days=7)

_EVENT_WAITS_SQL = """
    SELECT w.* FROM expected_event_watches w
      JOIN plan_nodes n ON n.id=w.plan_node_id
      JOIN plans p ON p.id=n.plan_id AND p.owner_kind='thread' AND p.state<>'archived'
      JOIN threads t ON t.id=p.owner_id AND t.lifecycle_state IS NOT 'purged'
     WHERE w.status='armed' AND w.condition_type<>'deadline'
       AND n.state IN ('pending','active','review_requested') AND n.archived_at IS NULL
     ORDER BY w.armed_at, w.id
"""


def migrate_59_to_60(conn: sqlite3.Connection) -> None:
    """Give every open event wait a deadline, spread over the next 7 days."""
    from plans import public

    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        waits = conn.execute(_EVENT_WAITS_SQL).fetchall()
        now = datetime.now(timezone.utc)
        count = len(waits)
        for index, watch in enumerate(waits):
            deadline = now + _FIRST + (_SPREAD - _FIRST) * (index + 1) / count
            public.give_event_wait_deadline(
                conn, watch, deadline=deadline.isoformat(), provenance=_PROVENANCE,
            )
    finally:
        conn.row_factory = caller_factory
