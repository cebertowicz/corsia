"""N85a automatic migration is additive, atomic and equivalent to fresh schema."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_to_v88_policy


def test_startup_adds_review_requests_preserving_existing_state(tmp_path):
    path = tmp_path / "reviews-v84.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)
        db.execute("DROP TABLE doc_review_requests")
        db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES ('keep','u','Kept','t0','t0')")
        db.execute("PRAGMA user_version=84")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 149
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT title FROM threads WHERE id='keep'").fetchone()[0] == "Kept"
        columns = {r[1] for r in db.execute("PRAGMA table_info(doc_review_requests)")}
        assert columns == {"id", "artifact_id", "requester_kind", "requester_id", "reviewer_thread_id", "note", "client_ids", "node_id", "created_at", "fork_id", "state", "state_changed_at", "reason", "requesting_node_id"}
        indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='doc_review_requests' AND sql IS NOT NULL")}
        assert indexes == {"idx_doc_review_reviewer", "idx_doc_review_requests_reviewer"}
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        with closing(sqlite3.connect(":memory:")) as fresh:
            initialize_schema(fresh)
            sql = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='doc_review_requests' ORDER BY type,name"
            assert db.execute(sql).fetchall() == fresh.execute(sql).fetchall()


def test_review_migration_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "reviews-rollback.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)
        db.execute("DROP TABLE doc_review_requests")
        db.execute("PRAGMA user_version=84")
        db.commit()
        apply = MIGRATIONS[84]

        def fail(connection):
            apply(connection)
            raise RuntimeError("injected DDL failure")

        monkeypatch.setitem(MIGRATIONS, 84, fail)
        with pytest.raises(Exception, match="injected DDL failure"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 84
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='doc_review_requests'").fetchone() is None
