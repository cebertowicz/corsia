"""Schema 119 -> 120: withdraw retired asks and move legacy deferrals to holds."""
from __future__ import annotations

import sqlite3

_TABLES = (
    "artifact_identities", "plan_node_artifact_links", "plan_node_events", "plan_node_holds",
    "plan_nodes", "plans",
)


def migrate_119_to_120(conn: sqlite3.Connection) -> None:
    """Finalize retired asks and copy uniquely deferred legacy asks beside their hold."""
    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return
    withdraw_retired_user_asks(conn)
    copy_deferred_ask_holds(conn)


def withdraw_retired_user_asks(conn: sqlite3.Connection) -> int:
    """Clear orphaned user asks only when their task is positively retired."""
    from plans.public import TERMINAL_STATES, ask_ending_detail, clear_hold_write, open_asks_sql

    terminal = tuple(sorted(TERMINAL_STATES))
    marks = ",".join("?" for _ in terminal)
    rows = conn.execute(
        open_asks_sql("h.id AS hold_id, h.node_id", by="all")
        + f" AND (n.archived_at IS NOT NULL OR p.state='archived' OR n.state IN ({marks}))"
        " ORDER BY h.created_at, h.id",
        terminal,
    ).fetchall()
    for row in rows:
        # clear_hold_write only appends the hold event and touches the node: it neither
        # validates live state nor reconciles admission, so it is migration-safe.
        clear_hold_write(
            str(row["node_id"]), str(row["hold_id"]), expected_kind="blocked",
            actor_id=None, provenance="migration.119_to_120", detail=ask_ending_detail(why="task_ended"), db=conn,
        )
    return len(rows)


def copy_deferred_ask_holds(conn: sqlite3.Connection) -> int:
    """Copy only legacy deferrals with exactly one positively open ask."""
    from plans.public import open_asks_sql

    conn.execute(
        """CREATE TABLE IF NOT EXISTS plan_ask_deferrals (
               hold_id TEXT PRIMARY KEY REFERENCES plan_node_holds(id),
               deferred_at TEXT NOT NULL
           )"""
    )
    rows = conn.execute(
        "SELECT id,updated_at FROM artifact_identities "
        "WHERE json_extract(metadata, '$.attention_state')='deferred'"
    ).fetchall()
    copied = 0
    for row in rows:
        holds = conn.execute(
            open_asks_sql("h.id") + " ORDER BY h.created_at, h.id", (str(row["id"]),),
        ).fetchall()
        if len(holds) != 1:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO plan_ask_deferrals (hold_id,deferred_at) VALUES (?,?)",
            (str(holds[0]["id"]), str(row["updated_at"])),
        )
        copied += 1
    return copied
