"""Schema 101 -> 102: the reviewer index."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_101_to_102_reviewer_awareness import migrate_101_to_102
from infra.schema import SCHEMA_VERSION


def test_migration_registered_and_adds_the_reviewer_index(tmp_path):
    assert SCHEMA_VERSION == 144 and MIGRATIONS[101] is migrate_101_to_102
    path = tmp_path / "v101.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP INDEX idx_doc_review_requests_reviewer")
        db.execute("PRAGMA user_version=101")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = [r[2] for r in db.execute("PRAGMA index_info(idx_doc_review_requests_reviewer)")]
        assert columns == ["reviewer_thread_id", "created_at"]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
