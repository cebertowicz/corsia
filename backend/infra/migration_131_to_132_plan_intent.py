"""Add the task intent and its append-only supporting words."""
from __future__ import annotations

import sqlite3


def migrate_131_to_132(conn: sqlite3.Connection) -> None:
    """Keep existing tasks unchanged while allowing newly created intent data."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "plan_nodes" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_nodes)")}
    if "intent" not in columns:
        conn.execute("ALTER TABLE plan_nodes ADD COLUMN intent TEXT")
    if "intent_node_id" not in columns:
        conn.execute("ALTER TABLE plan_nodes ADD COLUMN intent_node_id TEXT REFERENCES plan_nodes(id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_nodes_intent_node ON plan_nodes(intent_node_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS plan_node_intent_words (
        node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
        author TEXT NOT NULL CHECK (author IN ('person', 'agent')),
        source_kind TEXT NOT NULL CHECK (source_kind IN ('message', 'doc_comment', 'decision_answer', 'plan_edit')),
        source_id TEXT NOT NULL,
        quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
        role TEXT NOT NULL CHECK (role IN ('origin', 'bears_on', 'authority')),
        requirements_revision INTEGER NOT NULL CHECK (requirements_revision > 0),
        added_at TEXT NOT NULL,
        removed_at TEXT,
        removed_in_revision INTEGER,
        CHECK ((removed_at IS NULL) = (removed_in_revision IS NULL))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_node_intent_words_node ON plan_node_intent_words(node_id)")
