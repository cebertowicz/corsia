"""Schema v55 makes checkpoint FTS narrative-only."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS


def test_checkpoint_fts_migration_rebuilds_narrative_index() -> None:
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE checkpoints (
                generation_order INTEGER PRIMARY KEY,
                raw_result TEXT,
                continuity_snapshot_json TEXT
            );
            CREATE VIRTUAL TABLE checkpoints_fts USING fts5(
                raw_result, continuity_snapshot_json,
                content=checkpoints, content_rowid=generation_order
            );
            INSERT INTO checkpoints VALUES (1, 'narrative needle', 'continuity needle');
        """)

        MIGRATIONS[54](db)

        columns = [row[1] for row in db.execute("PRAGMA table_info(checkpoints_fts)")]
        assert columns == ["raw_result"]
        assert db.execute(
            "SELECT count(*) FROM checkpoints_fts WHERE checkpoints_fts MATCH 'narrative'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT count(*) FROM checkpoints_fts WHERE checkpoints_fts MATCH 'continuity'"
        ).fetchone()[0] == 0
    finally:
        db.close()
