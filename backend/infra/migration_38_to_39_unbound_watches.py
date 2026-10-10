"""Schema 38 -> 39: end the watches nothing could end (#1707).

Before ``automation.watches.arm_watch`` refused them, a watch with an event,
milestone or dependency condition could be armed on no registered artifact.
Only a deadline expires, and only an artifact's event resolves a watch, so such
a watch stayed armed for good and its node read "waiting" with nothing coming.

Each one becomes a deadline due now: the ordinary expiry sweep expires it on
its first pass and wakes its owner to decide what the wait was for. A plan-bound
watch also gets a ``watch_armed`` event on its node naming the condition it
replaces, so the node's history explains the expiry that follows.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

PROVENANCE = "migration.38_to_39"


def migrate_38_to_39(conn: sqlite3.Connection) -> None:
    """Turn every armed unbound non-deadline watch into a deadline due now."""
    from plans import public

    now = datetime.now(timezone.utc).isoformat()
    # The plan writer reads rows by name; the caller's connection is left as found.
    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT w.id, w.plan_node_id, w.expected_event, w.condition_type, "
            "w.condition_value, n.id AS node_id "
            "FROM expected_event_watches w "
            "LEFT JOIN plan_nodes n ON n.id=w.plan_node_id "
            "WHERE w.status='armed' AND w.artifact_integration IS NULL "
            "AND w.condition_type<>'deadline' ORDER BY w.armed_at, w.id"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE expected_event_watches "
                "SET condition_type='deadline', condition_value=?, updated_at=? "
                "WHERE id=?",
                (now, now, row["id"]),
            )
            if row["node_id"] is None:
                continue
            public.record_node_event(
                conn, str(row["node_id"]), "watch_armed", actor_id=None,
                provenance=PROVENANCE,
                payload={
                    "watch_id": row["id"],
                    "expected_event": row["expected_event"],
                    "condition": {"type": "deadline", "value": now},
                    "replaces": {
                        "type": row["condition_type"],
                        "value": row["condition_value"],
                    },
                },
            )
    finally:
        conn.row_factory = caller_factory
