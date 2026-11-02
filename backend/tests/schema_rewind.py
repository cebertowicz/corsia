"""Helpers for schema-ladder tests that rewind a current-schema database."""

from __future__ import annotations

import sqlite3

#: Columns of ``checkpoints`` added after the version a ladder test rewinds to:
#: the snapshot columns by migration 42 -> 43 (#1602), ``through_at`` by
#: 73 -> 74 (#3196) and ``adopted_input_tokens`` by 168 -> 169. Step 37 -> 38
#: copies the table's current columns into a table with its own frozen shape,
#: so a column added later has to go before a ladder that runs through it.
_LATER_COLUMNS = ("artifact_snapshot_json", "plan_status_snapshot_json", "through_at", "adopted_input_tokens")


def rewind_plan_aggregates(db: sqlite3.Connection) -> None:
    """Undo schema 102 (#731): a database below it has none of the stored aggregates.

    A ladder test builds its old database through the plans service, which keeps
    the aggregate columns current, and then writes raw rows that bypass them. Left
    in place those columns go stale and a late step that fails or settles nodes
    through the service would read them. Dropping them makes the fixture what a
    pre-102 database is, so step 101 -> 102 builds them from the rows.
    """
    from infra.plan_aggregates_schema import (
        NODE_AGGREGATE_COLUMNS, NODE_AGGREGATE_INDEXES, PLAN_AGGREGATE_COLUMNS, TOUCH_INDEXES,
    )

    for index in (*NODE_AGGREGATE_INDEXES, *TOUCH_INDEXES):
        db.execute(f"DROP INDEX IF EXISTS {index}")
    for table, columns in (("plan_nodes", NODE_AGGREGATE_COLUMNS), ("plans", PLAN_AGGREGATE_COLUMNS)):
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, _ in columns:
            if name in present:
                db.execute(f"ALTER TABLE {table} DROP COLUMN {name}")


def rewind_to_v88_policy(db: sqlite3.Connection) -> None:
    """Restore the legacy policy table required by an authentic pre-v89 fixture."""
    rewind_plan_aggregates(db)
    db.execute("DROP TABLE IF EXISTS provider_policies")
    db.execute("DROP TABLE IF EXISTS project_policy_overrides")
    db.execute("""
        CREATE TABLE IF NOT EXISTS project_vendor_accounts (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            allowlist_json TEXT,
            default_account_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            switch_on_limit INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(project_id, provider)
        )
    """)


def rewind_below_v42(db: sqlite3.Connection) -> None:
    """Undo what schemas 42 and later added that the ladder's own steps create.

    A database below 42 has neither the ``plan_pane_views`` table (42) nor the
    checkpoint snapshot columns (43), and one below 74 has no ``through_at``. A
    ladder test builds its old database from the current schema and then rewinds
    ``user_version``; without this the rewound database already carries tables
    and columns, in their current shape, that the ladder's own steps add or copy
    around.
    """
    rewind_plan_aggregates(db)
    db.execute("DROP TABLE IF EXISTS plan_pane_views")
    present = {row[1] for row in db.execute("PRAGMA table_info(checkpoints)")}
    for column in _LATER_COLUMNS:
        if column in present:
            db.execute(f"ALTER TABLE checkpoints DROP COLUMN {column}")