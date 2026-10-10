"""Schema 56 -> 57: submissions a review rejection failed close as sent back.

Until this change rejecting a reviewed submission (``plans.rejection``) failed
it, so work sent back for a revision showed red as if its objective could not
be met. A rejection now closes the submission with the reason "Sent back:
<feedback>". This closes the earlier ones the same way, so there is one
representation of sent-back work.

The runtime's own write answers "was this failed by a rejection?": the
rejection recorded ``review_rejected`` with the feedback and then failed the
node in the same write, with the same actor and provenance. A node qualifies
only when it is still failed, still on its plan, and that event is the very
one before its last move to ``failed``. A node that failed some other way has
no such pairing, so its red stays true. So does one with open work beneath it:
the runtime send-back refuses that close (``archival._require_no_open_children``),
the old rejection never checked, and a closed node may hold nothing open.

Each qualifying node moves to ``closed`` through the runtime's own writers, as
``plans.rejection`` now records it: ``plans.service.touch_node`` (the one
writer of a node's state and revision, #2214) and a ``state_transition`` event
carrying the reason, then ``plans.archival.release_held``, the release every
close shares. The plan's revision advances by its trigger, so a caller holding
the old ``expected_revision`` is refused rather than acting on a state that
changed under it. The events carry this step's provenance, so a worker's
parent is not told of a rejection it already heard
(``plans.plan_events.from_schema_step``). Archived nodes are left alone:
nothing shows them.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import json
import sqlite3

#: The reason prefix ``plans.rejection.SENT_BACK`` records; copied, not
#: imported, so this step keeps writing what it was written to write.
_SENT_BACK = "Sent back"
_PROVENANCE = "migration.56_to_57"

_CANDIDATES_SQL = """
    SELECT id FROM plan_nodes
     WHERE state='failed' AND archived_at IS NULL
       AND EXISTS (SELECT 1 FROM plan_node_events e
                    WHERE e.node_id=plan_nodes.id AND e.event='review_rejected')
"""

_EVENTS_SQL = """
    SELECT event, new_state, actor_id, provenance, payload_json FROM plan_node_events
     WHERE node_id=? ORDER BY created_at, rowid
"""


def _rejection_feedback(events: list[sqlite3.Row]) -> str | None:
    """The feedback of the rejection that made the node's last move to failed."""
    failed_at = max(
        (i for i, e in enumerate(events)
         if e[0] == "state_transition" and e[1] == "failed"),
        default=None,
    )
    if not failed_at:
        return None
    rejected, failed = events[failed_at - 1], events[failed_at]
    if rejected[0] != "review_rejected" or (rejected[2], rejected[3]) != (failed[2], failed[3]):
        return None
    try:
        feedback = (json.loads(rejected[4] or "{}") or {}).get("feedback")
    except (TypeError, ValueError):
        feedback = None
    return str(feedback) if feedback else ""


def _open_below(conn: sqlite3.Connection, node_id: str) -> bool:
    """Whether the runtime send-back would refuse this close for open work below."""
    from plans import public

    try:
        public.require_no_open_children(conn, node_id)
    except public.PlanError:
        return True
    return False


def migrate_56_to_57(conn: sqlite3.Connection) -> None:
    """Close every on-plan submission a review rejection failed."""
    from plans import public

    # A database that never held plans has nothing to close.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_nodes'"
    ).fetchone():
        return

    # The plan writers read rows by name; the caller's connection is left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        for (node_id,) in conn.execute(_CANDIDATES_SQL).fetchall():
            feedback = _rejection_feedback(conn.execute(_EVENTS_SQL, (node_id,)).fetchall())
            if feedback is None or _open_below(conn, node_id):
                continue
            public.touch_node(conn, node_id, state="closed")
            public.record_node_event(
                conn, node_id, "state_transition", actor_id=None,
                provenance=_PROVENANCE, prior_state="failed", new_state="closed",
                payload={"reason": f"{_SENT_BACK}: {feedback}" if feedback else _SENT_BACK},
            )
            public.release_held(
                conn, public.node_row(conn, node_id), actor_id=None,
                provenance=_PROVENANCE,
            )
    finally:
        conn.row_factory = caller_factory
