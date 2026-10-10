"""Retire registrations of pull requests that ended before their end retired them."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def migrate_22_to_23(conn: sqlite3.Connection) -> None:
    """Retire every unretired registration of an already-ended pull request.

    While pull requests were exempt from node settlement but nothing yet
    retired them on merge, the merge webhook still recorded ``pr_state`` on
    the identity. Those rows are stamped and settled here exactly as the live
    writer (``registry.node_retirement.retire_registrations_for_ended_artifact``)
    would have: a pinned or already-quiet row keeps its surfacing, and a later
    unpin re-reads the stamp. This one-time cutover appends no audit events;
    the identity's recorded ``pr_state`` is the cause. The ended-PR predicate
    comes from the one shared definition rather than a local copy.
    """
    from registry.retirement_predicate import artifact_ended_clause

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"""
        UPDATE artifact_registrations AS r
           SET node_retired_at = ?,
               surfacing = CASE
                   WHEN r.relationship_state <> 'owner-pinned' AND r.surfacing = 'visible'
                       THEN 'settled'
                   ELSE r.surfacing
               END,
               updated_at = ?
         WHERE r.node_retired_at IS NULL
           AND {artifact_ended_clause('r.artifact_id')}
        """,
        (now, now),
    )
