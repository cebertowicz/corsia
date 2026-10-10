"""Durable managed background-process handles and retirement outbox."""

from __future__ import annotations

MANAGED_PROCESS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS managed_process_handles (
        handle TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        lease_id TEXT,
        description TEXT,
        long_running INTEGER NOT NULL DEFAULT 0 CHECK (long_running IN (0, 1)),
        pid INTEGER NOT NULL,
        pgid INTEGER NOT NULL,
        creation_fingerprint TEXT NOT NULL,
        log_path TEXT NOT NULL,
        state TEXT NOT NULL,
        outcome TEXT,
        reason TEXT,
        exit_code INTEGER,
        started_at TEXT NOT NULL,
        last_output_at TEXT,
        stalled_since TEXT,
        stall_after_seconds INTEGER,
        max_lifetime_seconds INTEGER,
        ended_at TEXT,
        log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
        event_message_id TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_managed_process_handles_thread
        ON managed_process_handles(thread_id, started_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_managed_process_handles_state
        ON managed_process_handles(state)
    """,
    """
    -- Deliberately no thread foreign key: this row owns post-delete file cleanup.
    CREATE TABLE IF NOT EXISTS managed_process_retirements (
        handle TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        head_log_name TEXT NOT NULL,
        tail_log_name TEXT,
        staged_at TEXT NOT NULL
    )
    """,
)

MANAGED_PROCESS_SCHEMA_SQL = ";\n".join(MANAGED_PROCESS_DDL) + ";"
