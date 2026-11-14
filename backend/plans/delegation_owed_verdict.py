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


def _owed_sql(node: str, review: str) -> str:
    """The one owed test, for every read: ``{review}`` joined plus its WHERE terms.

    The step is in review on a returned worker and its latest review event
    (``plans.harvest_review_event``) is the harvest's own transition. Binds the
    two ``HARVEST_EVENT`` values, in that order, after the caller's earlier ones.
    """
    return f"""JOIN plan_node_events AS {review} ON {review}.rowid=(
                {latest_review_event_sql("review_event.rowid", node + ".id")})
            WHERE {node_states.in_work_sql(node)} AND {node}.state='review_requested'
              AND {node}.spawned_thread_id IS NOT NULL
              AND {review}.event=? AND {review}.provenance=?"""


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
            {_owed_sql('n', 'review')}
              AND p.owner_kind='thread' AND p.state <> 'paused'{only}
              AND julianday(?) - julianday(review.created_at) >= ? / 86400.0
              AND NOT EXISTS (
                  SELECT 1 FROM messages AS reminder
                  WHERE reminder.id = ? || n.id || ':' || review.id || ?)
            ORDER BY n.id""",
        (now, *HARVEST_EVENT, *(node_ids or ()), now, VERDICT_OWED_AFTER_SECONDS,
         wake_head, wake_tail),
    ).fetchall()
    return [dict(row) for row in rows]


#: Upper bounds, in hours, of the age buckets ``owed_verdict_ages`` reports.
_AGE_BUCKETS = ((1, "under_1h"), (6, "1h_to_6h"), (24, "6h_to_1d"), (168, "1d_to_7d"))


def owed_verdict_ages(connection, *, user_id: str, now: str) -> dict:
    """Counts of every verdict still owed on this user's plans, by age since the return.

    The same owed test as ``owed_verdicts`` (``_owed_sql``), with no threshold, reminded steps and paused plans
    included: the measure of whether parents act, not the reminder's work list.
    Aggregates only; no node, plan or thread identity leaves this function.
    """
    rows = connection.execute(
        f"""SELECT n.plan_id, p.state='paused' AS paused,
                   (julianday(?) - julianday(review.created_at)) * 24 AS hours
            FROM threads AS t INDEXED BY idx_threads_user
            CROSS JOIN plans AS p ON p.owner_kind='thread' AND p.owner_id=t.id
            CROSS JOIN plan_nodes AS n INDEXED BY idx_plan_nodes_tree ON n.plan_id=p.id
            {_owed_sql('n', 'review')} AND t.user_id=?""",
        (now, *HARVEST_EVENT, user_id),
    ).fetchall()
    by_age = {label: 0 for _, label in _AGE_BUCKETS} | {"over_7d": 0}
    plans_over_1h: set[str] = set()
    over_1d_unpaused = 0
    for plan_id, paused, hours in rows:
        by_age[next((label for limit, label in _AGE_BUCKETS if hours < limit), "over_7d")] += 1
        if hours >= 1:
            plans_over_1h.add(plan_id)
        if hours >= 24 and not paused:
            over_1d_unpaused += 1
    return {
        "owed": len(rows),
        "over_1h": len(rows) - by_age["under_1h"],
        "parent_plans_over_1h": len(plans_over_1h),
        "over_1d_on_unpaused_plans": over_1d_unpaused,
        "by_age": by_age,
    }


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
