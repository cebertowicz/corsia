"""Schema 195 -> 196: give every Later pile made before its weekly review a review time.

``plans.later_review_at`` is armed when a task crosses below the cut
(``plans.later_review.arm``). Piles built between the cut (184 -> 185) and the
review that reads it were never armed, and ``plans.later_review.due`` only
reads a set time, so those plans would never be reviewed until some new task
crossed the cut -- the oldest, least-touched piles first of all.

This step arms them as ``arm`` would: a thread-owned plan with no review time
and at least one open, unarchived Later task. The time is the plan's oldest
open Later task's creation advanced by whole weeks to the first instant after
the migration runs, so plans keep different weekdays and the first sweep after
an upgrade wakes nobody. A plan already armed is left where it is, and a pile
the review later finds empty only clears its time, so arming one too many
plans costs nothing. The step runs inside the runner's transaction; it never
commits or rolls back.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

#: The review cadence in ``plans.later_review.REVIEW_DAYS``, frozen with this step.
_WEEK = timedelta(days=7)

_UNARMED_PILES_SQL = """
    SELECT p.id AS plan_id, MIN(n.created_at) AS oldest
    FROM plans AS p JOIN plan_nodes AS n ON n.plan_id = p.id
    WHERE p.owner_kind = 'thread' AND p.later_review_at IS NULL
      AND n.kind = 'task' AND n.later = 1 AND n.archived_at IS NULL
      AND n.state IN ('pending', 'active', 'review_requested')
    GROUP BY p.id
"""


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def first_review_after(anchor: datetime | None, now: datetime) -> datetime:
    """The first weekly instant after ``now`` on ``anchor``'s schedule; a week out without one."""
    if anchor is None or anchor > now:
        return now + _WEEK
    return anchor + _WEEK * ((now - anchor) // _WEEK + 1)


def migrate_195_to_196(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Arm each unarmed plan whose Later pile holds an open task."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"plans", "plan_nodes"} <= tables:
        return
    plan_columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
    node_columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_nodes)")}
    if "later_review_at" not in plan_columns or "later" not in node_columns:
        return
    clock = now or datetime.now(timezone.utc)
    for plan_id, oldest in conn.execute(_UNARMED_PILES_SQL).fetchall():
        conn.execute(
            "UPDATE plans SET later_review_at=? WHERE id=? AND later_review_at IS NULL",
            (first_review_after(_parse(oldest), clock).isoformat(), plan_id),
        )
