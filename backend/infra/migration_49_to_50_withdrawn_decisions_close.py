"""Schema 49 -> 50: review tasks of decisions withdrawn before #2589 close.

Until #2589 withdrawing a decision (``plans.decisions.invalidate``, or the
automatic withdrawal when its owner thread is archived) failed the decision's
review task, so a question nobody needed any more showed red. Withdrawal now
closes it with the reason "Decision withdrawn". This closes the earlier ones
the same way, so there is one representation of a withdrawn question.

The runtime's own write answers "was this failed by a withdrawal?": both
withdrawal paths fail the review task and then record ``decision_invalidated``
on it in the same write, with the same actor and provenance. A task qualifies
only when it is still failed, still on its plan, and that event is the very
next one after its last move to ``failed``. A task that failed for another
reason first and was withdrawn later has no such pairing (the withdrawal left
it alone), so its red stays true.

Each qualifying task moves to ``closed`` through the runtime's own writers, as
``plans.closure.close_node`` would record it: ``plans.service.touch_node`` (the
one writer of a node's state and revision, #2214) and a ``state_transition``
event carrying the reason, then ``plans.archival.release_held``, the release
every close shares: its dependency edges drop and its armed watches settle,
which a failed task still holds. The plan's revision advances by its trigger, so a
caller holding the old ``expected_revision`` is refused rather than acting on
a state that changed under it. Archived tasks are left alone: they are off
their plans, and nothing shows them.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3

#: The close reason ``plans.decision_lifecycle.WITHDRAWN`` records; copied, not
#: imported, so this step keeps writing what it was written to write.
_WITHDRAWN = "Decision withdrawn"
_PROVENANCE = "migration.49_to_50"

_CANDIDATES_SQL = """
    SELECT id FROM plan_nodes
     WHERE state='failed' AND archived_at IS NULL
       AND EXISTS (SELECT 1 FROM plan_node_events e
                    WHERE e.node_id=plan_nodes.id AND e.event='decision_invalidated')
"""

_EVENTS_SQL = """
    SELECT event, new_state, actor_id, provenance FROM plan_node_events
     WHERE node_id=? ORDER BY created_at, rowid
"""


def _withdrawn(events: list[sqlite3.Row]) -> bool:
    """Whether the node's last move to failed was the withdrawal's own."""
    failed_at = max(
        (i for i, e in enumerate(events)
         if e[0] == "state_transition" and e[1] == "failed"),
        default=None,
    )
    if failed_at is None or failed_at + 1 >= len(events):
        return False
    failed, following = events[failed_at], events[failed_at + 1]
    return (
        following[0] == "decision_invalidated"
        and following[2] == failed[2] and following[3] == failed[3]
    )


def migrate_49_to_50(conn: sqlite3.Connection) -> None:
    """Close every on-plan review task a decision withdrawal failed."""
    from plans import public

    # The plan writers read rows by name; the caller's connection is left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        for (node_id,) in conn.execute(_CANDIDATES_SQL).fetchall():
            if not _withdrawn(conn.execute(_EVENTS_SQL, (node_id,)).fetchall()):
                continue
            public.touch_node(conn, node_id, state="closed")
            public.record_node_event(
                conn, node_id, "state_transition", actor_id=None,
                provenance=_PROVENANCE, prior_state="failed", new_state="closed",
                payload={"reason": _WITHDRAWN},
            )
            public.release_held(
                conn, public.node_row(conn, node_id), actor_id=None,
                provenance=_PROVENANCE,
            )
    finally:
        conn.row_factory = caller_factory
