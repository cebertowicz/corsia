"""Reminder to a parent that owes the verdict on a returned delegated step.

The harvest (``plans.delegation_harvest``) moves the parent's step into review
when its worker returns; the parent then owes the verdict. This is the read the
continuation runner sweeps and the one write that reminds the parent once.
"""

from __future__ import annotations

import os

import plans.node_states as node_states
import plans.plan_wake as plan_wake
from plans.harvest_review_event import HARVEST_EVENT, latest_review_event_sql

#: How long a returned step may wait on its parent's verdict before the parent
#: is reminded once; the returned worker sits idle until then.
VERDICT_OWED_AFTER_SECONDS = int(os.getenv("CORSIA_VERDICT_OWED_SECONDS", "1800"))

_OWED = "verdict_owed"


def owed_verdicts(connection, *, now: str, node_ids: list[str] | None = None) -> list:
    """Delegated steps whose harvest return has waited past the threshold, not yet reminded.

    A plain read, safe outside any write (#2748), and one query however many
    steps are returned or already reminded. The harvest's review is the step's
    latest review event (``plans.harvest_review_event``, which ``harvest_review``
    reads too) and counts only when it is the harvest's own transition. One
    reminder per return: the wake is keyed on the harvest event, so a later
    round's return is a new event with its own reminder, and a step whose wake
    message exists is excluded here. A paused plan gets none; a verdict still
    owed is found on the first sweep after it resumes.
    """
    only = "" if node_ids is None else f" AND n.id IN ({','.join('?' for _ in node_ids)})"
    # The wake id around its subject, so the key stays ``plan_wake.wake_id``'s.
    wake_head, wake_tail = plan_wake.wake_id(_OWED, chr(0), 0).split(chr(0))
    rows = connection.execute(
        f"""SELECT n.id AS node_id, n.plan_id, n.spawned_thread_id AS worker,
                   review.id AS event_id,
                   CAST((julianday(?) - julianday(review.created_at)) * 1440 AS INTEGER)
                       AS minutes
            FROM plan_nodes AS n
            JOIN plans AS p ON p.id=n.plan_id
            JOIN plan_node_events AS review ON review.rowid=(
                {latest_review_event_sql("review_event.rowid", "n.id")})
            WHERE {node_states.in_work_sql('n')} AND n.state='review_requested'
              AND n.spawned_thread_id IS NOT NULL
              AND p.owner_kind='thread' AND p.state <> 'paused'{only}
              AND review.event=? AND review.provenance=?
              AND julianday(?) - julianday(review.created_at) >= ? / 86400.0
              AND NOT EXISTS (
                  SELECT 1 FROM messages AS reminder
                  WHERE reminder.id = ? || n.id || ':' || review.id || ?)
            ORDER BY n.id""",
        (now, *(node_ids or ()), *HARVEST_EVENT, now, VERDICT_OWED_AFTER_SECONDS,
         wake_head, wake_tail),
    ).fetchall()
    return [dict(row) for row in rows]


def notify_owed_verdicts(connection, *, now: str, node_ids: list[str]) -> None:
    """Remind a parent once that a returned worker still awaits its verdict.

    Runs inside the plan write and rechecks ``node_ids``, the candidates
    ``owed_verdicts`` found before the write began.
    """
    from plans.delegation_notice import _wake_parent

    for row in owed_verdicts(connection, now=now, node_ids=node_ids):
        _wake_parent(
            connection, str(row["plan_id"]), str(row["worker"]),
            kind=_OWED, subject_id=f"{row['node_id']}:{row['event_id']}",
            revision=0, minutes=row["minutes"],
        )
