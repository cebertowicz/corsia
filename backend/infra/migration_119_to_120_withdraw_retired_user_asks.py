"""Schema 119 -> 120: withdraw open asks on retired tasks."""
from __future__ import annotations

import sqlite3

_TABLES = (
    "plan_node_artifact_links", "plan_node_events", "plan_node_holds", "plan_nodes",
)


def migrate_119_to_120(conn: sqlite3.Connection) -> None:
    """Withdraw open asks on terminal or archived tasks without changing attention."""
    withdraw_retired_user_asks(conn)


def withdraw_retired_user_asks(conn: sqlite3.Connection) -> int:
    """Clear orphaned user asks only when their task is positively retired."""
    from plans.public import TERMINAL_STATES, clear_hold_write

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0
    terminal = tuple(sorted(TERMINAL_STATES))
    marks = ",".join("?" for _ in terminal)
    rows = conn.execute(
        f"""SELECT h.id AS hold_id,h.node_id
              FROM plan_node_holds h
              JOIN plan_node_artifact_links l
                ON l.node_id=h.node_id AND l.artifact_id=h.owner_id AND l.role='attention'
              JOIN plan_nodes n ON n.id=h.node_id
             WHERE h.kind='blocked' AND h.owner_kind='user' AND h.owner_id IS NOT NULL
               AND h.cleared_at IS NULL
               AND (n.archived_at IS NOT NULL OR n.state IN ({marks}))
             ORDER BY h.created_at,h.id""",
        terminal,
    ).fetchall()
    for row in rows:
        # clear_hold_write only appends the hold event and touches the node: it neither
        # validates live state nor reconciles admission, so it is migration-safe.
        clear_hold_write(
            str(row["node_id"]), str(row["hold_id"]), expected_kind="blocked",
            actor_id=None, provenance="migration.119_to_120", detail={"ended": "withdrawn"}, db=conn,
        )
    return len(rows)
