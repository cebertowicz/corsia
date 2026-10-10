"""User presence tables: the current visit per thread and the visit history.

``thread_presence`` is the one-row-per-(user, thread) current state that the
Plans read and the agent's ``User presence`` line use. ``presence_visits`` is
the raw history, one row per finished visit, for measuring where the user's
time goes; the row also holds the user's Mark read/unread seen state.
``infra.presence`` is the only writer of both.
"""

from __future__ import annotations

PRESENCE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS thread_presence (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        present INTEGER NOT NULL DEFAULT 0 CHECK (present IN (0, 1)),
        last_entered_at TEXT NOT NULL,
        last_exited_at TEXT,
        previous_entered_at TEXT,
        -- The exit time this visit will record: back-dated to the moment the
        -- user stopped being here (hidden, last input, last confirmation).
        exit_pending_at TEXT,
        -- When the server detected that exit. The merge window runs from here,
        -- never from the back-dated exit_pending_at.
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
        -- Seen state for the update dot: Mark read raises the seen boundary
        -- to this time; Mark unread keeps the dot on until a visit opens.
        marked_read_at TEXT,
        marked_unread_at TEXT,
        PRIMARY KEY (user_id, thread_id)
    );
    CREATE INDEX IF NOT EXISTS idx_thread_presence_thread
        ON thread_presence(thread_id);
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
    );
    CREATE INDEX IF NOT EXISTS idx_presence_visits_user_time
        ON presence_visits(user_id, entered_at);
    CREATE INDEX IF NOT EXISTS idx_presence_visits_thread
        ON presence_visits(thread_id, entered_at);
"""
