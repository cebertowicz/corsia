"""Schema 120 -> 121: bind a deferred ask's metadata to its open hold."""
from __future__ import annotations

import json
import sqlite3

_TABLES = ("artifact_identities", "plan_node_artifact_links", "plan_node_holds")


def migrate_120_to_121(conn: sqlite3.Connection) -> None:
    """Stamp only deferred artifacts with exactly one positively open ask."""
    stamp_deferred_ask_holds(conn)


def stamp_deferred_ask_holds(conn: sqlite3.Connection) -> int:
    """Return the number of uniquely attributable deferred asks stamped.

    Legacy duplicates cannot be assigned safely: their old deferral is left
    visibly raised rather than silently attached to an arbitrary ask.
    """
    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0
    rows = conn.execute(
        """SELECT id,metadata FROM artifact_identities
             WHERE json_extract(metadata, '$.attention_state')='deferred'"""
    ).fetchall()
    stamped = 0
    for row in rows:
        artifact_id = str(row["id"])
        holds = conn.execute(
            """SELECT h.id FROM plan_node_holds h
                 JOIN plan_node_artifact_links l
                   ON l.node_id=h.node_id AND l.artifact_id=h.owner_id
                  AND l.role='attention'
                WHERE h.kind='blocked' AND h.owner_kind='user'
                  AND h.owner_id=? AND h.cleared_at IS NULL
                ORDER BY h.created_at,h.id""",
            (artifact_id,),
        ).fetchall()
        if len(holds) != 1:
            continue
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            continue
        hold_id = str(holds[0]["id"])
        if metadata.get("deferred_hold_id") == hold_id:
            continue
        metadata["deferred_hold_id"] = hold_id
        conn.execute(
            "UPDATE artifact_identities SET metadata=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), artifact_id),
        )
        stamped += 1
    return stamped
