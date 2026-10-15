"""Schema 115 -> 116: the workspace conductor becomes the CORSIA agent.

Renames the persisted names of the workspace-scope agent in one transaction
(the runner's): the designation settings key, the daily health-check schedule
payload kind, the project-agent contact journal object id, and the designated
thread's default title. Data only; never commits.
"""

from __future__ import annotations

import json
import sqlite3

OLD_KEY = "workspace_conductor_session_id"
NEW_KEY = "corsia_agent_thread_id"
OLD_KIND = "conductor.health_check"
NEW_KIND = "corsia_agent.health_check"
OLD_CONTACT_SUFFIX = ":workspace-conductor-contact"
NEW_CONTACT_SUFFIX = ":corsia-agent-contact"
OLD_TITLE = "Conductor"
NEW_TITLE = "CORSIA"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rename_setting(conn: sqlite3.Connection) -> str | None:
    """Move the designation to the new key; return its thread id, if any.

    Old only: renamed. New only: untouched. Both: the new one wins and the old
    one is deleted.
    """
    old = conn.execute(
        "SELECT value FROM settings WHERE key=?", (OLD_KEY,)
    ).fetchone()
    new = conn.execute(
        "SELECT value FROM settings WHERE key=?", (NEW_KEY,)
    ).fetchone()
    if old is not None and new is None:
        conn.execute("UPDATE settings SET key=? WHERE key=?", (NEW_KEY, OLD_KEY))
    elif old is not None:
        conn.execute("DELETE FROM settings WHERE key=?", (OLD_KEY,))
    row = new if new is not None else old
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    thread_id = value.get("session_id") if isinstance(value, dict) else None
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _rename_health_check(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "automation_schedules"):
        return
    rows = conn.execute(
        "SELECT id, payload_json FROM automation_schedules "
        "WHERE payload_json LIKE ?",
        (f"%{OLD_KIND}%",),
    ).fetchall()
    for schedule_id, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("kind") != OLD_KIND:
            continue
        payload["kind"] = NEW_KIND
        conn.execute(
            "UPDATE automation_schedules SET payload_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), schedule_id),
        )


def _rename_contact_journal(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "context_change_journal"):
        return
    conn.execute(
        "UPDATE context_change_journal "
        "SET object_id=substr(object_id, 1, length(object_id) - ?) || ? "
        "WHERE object_kind='agent_roster' AND substr(object_id, -?) = ?",
        (
            len(OLD_CONTACT_SUFFIX),
            NEW_CONTACT_SUFFIX,
            len(OLD_CONTACT_SUFFIX),
            OLD_CONTACT_SUFFIX,
        ),
    )


def migrate_115_to_116(conn: sqlite3.Connection) -> None:
    """Rename the workspace conductor's persisted names to the CORSIA agent's."""
    if _has_table(conn, "settings"):
        thread_id = _rename_setting(conn)
        if thread_id is not None and _has_table(conn, "threads"):
            conn.execute(
                "UPDATE threads SET title=? WHERE id=? AND title=?",
                (NEW_TITLE, thread_id, OLD_TITLE),
            )
    _rename_health_check(conn)
    _rename_contact_journal(conn)
