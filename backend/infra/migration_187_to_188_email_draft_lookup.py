"""Schema 187 -> 188: index saved draft identity for every document open.

Additive, no row changes. The normal migration transaction owns this DDL.
"""
import sqlite3

from infra.action_drafts_schema import ACTION_DRAFTS_PATH_INDEX_SQL


def migrate_187_to_188(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_drafts'").fetchone():
        conn.execute(ACTION_DRAFTS_PATH_INDEX_SQL)
