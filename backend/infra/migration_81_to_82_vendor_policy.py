"""Frozen schema81→82 project account policy and durable holds.

No existing account/model/binding/usage is rewritten or inferred.
"""
from __future__ import annotations

import sqlite3

_POLICY_SQL = """
CREATE TABLE IF NOT EXISTS project_vendor_accounts (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('claude-code','codex')),
    allowlist_json TEXT CHECK (allowlist_json IS NULL OR
        (json_valid(allowlist_json) AND json_type(allowlist_json)='array')),
    default_account_id TEXT REFERENCES vendor_accounts(id),
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id,provider)
);
CREATE TABLE IF NOT EXISTS thread_vendor_account_holds (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('claude-code','codex')),
    account_id TEXT REFERENCES vendor_accounts(id),
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(thread_id,provider)
);
"""


def migrate_81_to_82(conn: sqlite3.Connection) -> None:
    """Install policy in the migration runner transaction without committing."""
    for statement in _POLICY_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
