"""Checkpoint schema: the one durable generation table and its index.

Split from `checkpoints.py` under the same convention as `plan_schema.py`, so
the table declaration has one home and the transition logic beside it stays
readable. `schema.py` composes this into the fresh-database script; migrations
carry their own copy of the DDL on purpose (see `database_migrations.py`).
"""

from __future__ import annotations

CHECKPOINT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    -- The cascade never fires at runtime: connections do not enable PRAGMA
    -- foreign_keys. Thread delete and purge remove these rows through
    -- infra.checkpoints.delete_thread_checkpoints (#2291). Migration 65 -> 66
    -- removed the rows earlier deletes left behind.
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    previous_generation_id TEXT,
    through_seq INTEGER NOT NULL,
    through_message_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual','pressure','model_switch','idle','agent')),
    mode TEXT NOT NULL CHECK (mode IN ('warm','cold')),
    status TEXT NOT NULL CHECK (
        status IN ('running','captured','installed','overtaken','failed','cancelled')
    ),
    source_harness TEXT NOT NULL,
    source_model TEXT NOT NULL,
    writer_harness TEXT NOT NULL,
    writer_model TEXT NOT NULL,
    originating_turn_id TEXT,
    first_included_seq INTEGER,
    continuity_base_revision INTEGER NOT NULL,
    continuity_base_snapshot_json TEXT NOT NULL,
    raw_result TEXT,
    continuity_snapshot_json TEXT,
    usage_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    captured_at TEXT,
    installed_at TEXT,
    ended_at TEXT,
    artifact_snapshot_json TEXT,
    plan_status_snapshot_json TEXT,
    -- The creation time of the boundary message this checkpoint's narrative
    -- covers (#3196): the rendered checkpoint dates its own history from it.
    -- NULL on rows installed before this column existed; never backfilled.
    through_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS checkpoints_one_live
    ON checkpoints(thread_id) WHERE status IN ('running','captured');
CREATE INDEX IF NOT EXISTS checkpoints_current
    ON checkpoints(thread_id,history_epoch,status,generation_order DESC);
CREATE INDEX IF NOT EXISTS checkpoints_listing
    ON checkpoints(thread_id,generation_order DESC);
CREATE INDEX IF NOT EXISTS checkpoints_idle_boundary
    ON checkpoints(thread_id,history_epoch,through_message_id) WHERE trigger='idle';
"""

# One row per retained frozen entry revision, rather than one row per whole
# checkpoint generation. The rowid is deliberately retained: the companion
# external-content FTS table uses it as its stable content key.
CHECKPOINT_ENTRY_SEARCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkpoint_entry_search (
    -- The cascade never fires at runtime: connections do not enable PRAGMA
    -- foreign_keys. Thread delete and purge remove these rows through
    -- infra.checkpoints.delete_thread_checkpoints (#2291). Migration 65 -> 66
    -- removed the rows earlier deletes left behind.
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    entry_id INTEGER NOT NULL CHECK (entry_id > 0),
    revision INTEGER NOT NULL CHECK (revision >= 0),  -- 0: entry carried none
    field TEXT NOT NULL,
    search_text TEXT NOT NULL,
    first_generation_order INTEGER NOT NULL
        REFERENCES checkpoints(generation_order) ON DELETE CASCADE,
    last_generation_order INTEGER NOT NULL
        REFERENCES checkpoints(generation_order) ON DELETE CASCADE,
    PRIMARY KEY (thread_id, history_epoch, entry_id, revision)
);
CREATE INDEX IF NOT EXISTS checkpoint_entry_search_identity
    ON checkpoint_entry_search(thread_id, history_epoch, entry_id);
"""

CHECKPOINT_SCHEMA_SQL += CHECKPOINT_ENTRY_SEARCH_SCHEMA_SQL
