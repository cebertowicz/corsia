"""Schema 149 -> 150: backfill thread-authored plan correction facts."""
from __future__ import annotations

import sqlite3


def migrate_149_to_150(conn: sqlite3.Connection) -> None:
    """Replay historical correction events through the transactional writer."""
    from core.self_caught_corrections import record_self_caught_correction

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"plan_node_events", "plan_nodes", "outcome_events", "threads"} <= tables:
        return
    ids = [row[0] for row in conn.execute(
        "SELECT id FROM plan_node_events WHERE event='correction_created' ORDER BY created_at,rowid"
    )]
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for event_id in ids:
            record_self_caught_correction(conn, event_id)
    finally:
        conn.row_factory = previous
