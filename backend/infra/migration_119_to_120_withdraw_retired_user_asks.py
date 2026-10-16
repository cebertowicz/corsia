"""Schema 119 -> 120: withdraw open asks on retired tasks."""
from __future__ import annotations

import json
import sqlite3

_TABLES = (
    "artifact_identities", "plan_node_artifact_links", "plan_node_events", "plan_node_holds",
    "plan_nodes", "plans",
)


def migrate_119_to_120(conn: sqlite3.Connection) -> None:
    """Finalize retired asks and bind uniquely deferred asks to their open hold."""
    withdraw_retired_user_asks(conn)
    stamp_deferred_ask_holds(conn)


def withdraw_retired_user_asks(conn: sqlite3.Connection) -> int:
    """Clear orphaned user asks only when their task is positively retired."""
    from plans.ask_endings import detail
    from plans.public import TERMINAL_STATES, clear_hold_write, open_asks_sql

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0
    terminal = tuple(sorted(TERMINAL_STATES))
    marks = ",".join("?" for _ in terminal)
    rows = conn.execute(
        open_asks_sql("h.id AS hold_id, h.node_id", by="all")
        + f" AND (n.archived_at IS NOT NULL OR n.state IN ({marks})) ORDER BY h.created_at, h.id",
        terminal,
    ).fetchall()
    for row in rows:
        # clear_hold_write only appends the hold event and touches the node: it neither
        # validates live state nor reconciles admission, so it is migration-safe.
        clear_hold_write(
            str(row["node_id"]), str(row["hold_id"]), expected_kind="blocked",
            actor_id=None, provenance="migration.119_to_120", detail=detail(why="task_ended"), db=conn,
        )
    return len(rows)


def stamp_deferred_ask_holds(conn: sqlite3.Connection) -> int:
    """Stamp only deferred artifacts with exactly one positively open ask."""
    from plans.public import open_asks_sql

    rows = conn.execute(
        "SELECT id,metadata FROM artifact_identities WHERE json_extract(metadata, '$.attention_state')='deferred'"
    ).fetchall()
    stamped = 0
    for row in rows:
        holds = conn.execute(
            open_asks_sql("h.id") + " ORDER BY h.created_at, h.id", (str(row["id"]),),
        ).fetchall()
        if len(holds) != 1:
            continue
        metadata = json.loads(row["metadata"] or "{}")
        hold_id = str(holds[0]["id"])
        if metadata.get("deferred_hold_id") == hold_id:
            continue
        metadata["deferred_hold_id"] = hold_id
        conn.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (json.dumps(metadata, sort_keys=True), row["id"]))
        stamped += 1
    return stamped
