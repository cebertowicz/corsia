"""Schema 142 -> 143: record the sent-back correction facts that earlier rejections never wrote."""
from __future__ import annotations

import sqlite3


def migrate_142_to_143(conn: sqlite3.Connection) -> None:
    """Write one fact per reviewed-node rejection, through the writer the rejection itself uses.

    A rejection creates its revision task (``follow_up_of`` the reviewed node, with a
    ``revision_created`` event) in the same mutation; the leaves a worker returned get the
    same feedback event but no revision, so they are not verdicts. Facts dedupe on the
    rejection event, so a rejection that already has one is left alone and a repeat is a no-op.
    """
    from core.sent_back_corrections import record_rejection_correction

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"plan_node_events", "plan_nodes", "plans", "outcome_events", "threads"} <= tables:
        return
    rejection_ids = [
        row[0] for row in conn.execute(
            """SELECT rejected.id FROM plan_node_events AS rejected
                WHERE rejected.event='review_rejected'
                  AND EXISTS (SELECT 1 FROM plan_nodes AS revision
                                JOIN plan_node_events AS created ON created.node_id=revision.id
                                 AND created.event='revision_created'
                               WHERE revision.follow_up_of=rejected.node_id)
                ORDER BY rejected.created_at, rejected.rowid"""
        )
    ]
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for rejection_id in rejection_ids:
            record_rejection_correction(conn, rejection_id)
    finally:
        conn.row_factory = previous
