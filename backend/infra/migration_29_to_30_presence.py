"""Schema 29 -> 30: user presence tables.

Purely additive, like schema 29's measurement step: two new tables and their
indexes, no data to convert. The DDL is copied rather than imported from
``infra.presence_schema`` so this step keeps producing the shape it was
written for even after that module's DDL changes later.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_PRESENCE_30_DDL = (
    """
    CREATE TABLE IF NOT EXISTS thread_presence (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        present INTEGER NOT NULL DEFAULT 0 CHECK (present IN (0, 1)),
        last_entered_at TEXT NOT NULL,
        last_exited_at TEXT,
        previous_entered_at TEXT,
        exit_pending_at TEXT,
        exit_pending_since TEXT,
        exit_pending_reason TEXT CHECK (exit_pending_reason IS NULL
            OR exit_pending_reason IN (
                'navigated', 'hidden', 'idle', 'disconnected', 'restart'
            )),
        last_confirmed_at TEXT NOT NULL,
        turn_order_at_exit INTEGER,
        away_seconds INTEGER,
        turns_while_away INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, thread_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_thread_presence_thread"
    " ON thread_presence(thread_id)",
    """
    CREATE TABLE IF NOT EXISTS presence_visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        surface TEXT NOT NULL CHECK (length(trim(surface)) > 0),
        thread_id TEXT,
        project_id TEXT,
        object_id TEXT,
        entered_at TEXT NOT NULL,
        exited_at TEXT NOT NULL,
        seconds INTEGER NOT NULL CHECK (seconds >= 0),
        sessions INTEGER NOT NULL CHECK (sessions > 0),
        exit_reason TEXT NOT NULL CHECK (exit_reason IN (
            'navigated', 'hidden', 'idle', 'disconnected', 'restart'
        ))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_presence_visits_user_time"
    " ON presence_visits(user_id, entered_at)",
    "CREATE INDEX IF NOT EXISTS idx_presence_visits_thread"
    " ON presence_visits(thread_id, entered_at)",
)


def migrate_29_to_30(conn: sqlite3.Connection) -> None:
    """Create the user presence tables on an existing database."""
    for statement in _PRESENCE_30_DDL:
        conn.execute(statement)
