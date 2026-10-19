"""User-ask DDL that ``plan_schema`` composes: deferrals and the review-round index.

The index has its own constant so the schema step that adds it to an existing
database creates it from this one definition (migration_126_to_127).
"""

from __future__ import annotations

#: ``plans.user_ask.ask_rounds``: an artifact's user asks, newest raise first,
#: read on the answer path; without it that read scans every hold ever placed.
ASK_ROUNDS_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_plan_holds_user_ask_rounds
        ON plan_node_holds(owner_id, created_at, id)
        WHERE kind='blocked' AND owner_kind='user';
"""

PLAN_ASK_SCHEMA_SQL = """
    -- Deferrals belong to open asks; watches alone end them, so no condition is stored here.
    CREATE TABLE IF NOT EXISTS plan_ask_deferrals (
        hold_id TEXT PRIMARY KEY REFERENCES plan_node_holds(id),
        deferred_at TEXT NOT NULL
    );
""" + ASK_ROUNDS_INDEX_SQL
