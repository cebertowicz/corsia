"""Schema 36 -> 37: move dependency edges stranded on rejected submissions (#1730).

Before rejection moved a submission's dependents onto its revision, a dependent
kept waiting on the rejected submission: that node is terminal, so the edge
read ``dependency_failed`` forever and accepting the revision never released
it. This replays the move the runtime now makes at rejection time, with the
runtime's own writer (``plans.dependencies.move_dependents``) and the runtime's
own fact that a revision exists (its ``revision_created`` event), oldest
rejection first so an edge follows a chain of revisions to its newest one.
Only open, unarchived dependents move; settled work is never rewritten.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3

PROVENANCE = "migration.36_to_37"


def migrate_36_to_37(conn: sqlite3.Connection) -> None:
    """Point every open dependent of a rejected submission at its revision."""
    from plans import public

    # The plan writers read rows by name; the caller's connection is left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT r.follow_up_of AS source_id, r.id AS revision_id
                 FROM plan_node_events e JOIN plan_nodes r ON r.id=e.node_id
                WHERE e.event='revision_created' AND r.follow_up_of IS NOT NULL
                ORDER BY e.created_at, e.id"""
        ).fetchall()
        for row in rows:
            public.move_dependents(
                conn, str(row["source_id"]), str(row["revision_id"]),
                actor_id=None, provenance=PROVENANCE,
            )
    finally:
        conn.row_factory = caller_factory
