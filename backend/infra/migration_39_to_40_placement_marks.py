"""Schema 39 -> 40: the checkpoint placement mark.

Adds the three nullable mark columns to ``continuity_entries`` and lets a
checkpoint stage a ``review`` verdict. SQLite cannot widen a CHECK in place,
so ``checkpoint_staged_edits`` is rebuilt with every row copied.

Runs inside the runner's transaction and never commits: statements go through
``execute`` one at a time because ``executescript`` would commit.
"""

from __future__ import annotations

import sqlite3

_MARK_COLUMNS = (
    "placement_state TEXT CHECK (placement_state IS NULL OR placement_state IN ("
    "'reaffirmed','selected','written','already','proposed','stays','refused'))",
    "placement_revision INTEGER",
    "placement_detail TEXT",
)

# The staged-edits table as schema 40 defines it. Deliberately a copy of
# ``infra.continuity_staging.STAGING_SCHEMA_SQL`` rather than an import of it,
# so this step keeps producing schema 40 after that constant changes (the rule
# in ``infra.database_migrations``). The copy is checked against the live
# constant while 40 is current.
STAGING_40_DDL = """
CREATE TABLE IF NOT EXISTS checkpoint_staged_edits (
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
    edit_order INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('add','replace','drop','review')),
    entry_id INTEGER NOT NULL CHECK (entry_id > 0),
    field TEXT NOT NULL,
    body TEXT CHECK (body IS NULL OR json_valid(body)),
    reason TEXT,
    source_ref TEXT,
    base_entry_revision INTEGER,
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending','applied','conflict')),
    outcome_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    PRIMARY KEY (checkpoint_id, edit_order)
);
CREATE INDEX IF NOT EXISTS checkpoint_staged_edits_target
    ON checkpoint_staged_edits(checkpoint_id,entry_id,edit_order);
"""

_STAGED_COLUMNS = (
    "checkpoint_id,edit_order,operation,entry_id,field,body,reason,source_ref,"
    "base_entry_revision,outcome,outcome_detail,created_at,resolved_at"
)


def migrate_39_to_40(conn: sqlite3.Connection) -> None:
    """Add the placement mark and admit staged review verdicts."""
    existing = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(continuity_entries)")
    }
    for column in _MARK_COLUMNS:
        if column.split(" ", 1)[0] not in existing:
            conn.execute(f"ALTER TABLE continuity_entries ADD COLUMN {column}")

    conn.execute("DROP INDEX IF EXISTS checkpoint_staged_edits_target")
    conn.execute(
        "ALTER TABLE checkpoint_staged_edits RENAME TO checkpoint_staged_edits_v39"
    )
    for statement in STAGING_40_DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(
        f"INSERT INTO checkpoint_staged_edits ({_STAGED_COLUMNS}) "
        f"SELECT {_STAGED_COLUMNS} FROM checkpoint_staged_edits_v39"
    )
    conn.execute("DROP TABLE checkpoint_staged_edits_v39")
