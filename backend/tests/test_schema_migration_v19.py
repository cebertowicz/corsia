"""Retiring derived summary search preserves stored history and checkpoint search."""

from contextlib import closing
import sqlite3

import pytest

from checkpoint_retrieval_fixtures import _install_checkpoint
from infra.database import initialize_schema
from infra.database_migrations import MigrationError, migrate_to_current
from infra.schema import SCHEMA_VERSION


@pytest.fixture
def legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        # Reconstruct the exact retired v18 index, independently of fresh DDL.
        db.executescript("""
            DROP TRIGGER IF EXISTS summaries_fts_insert;
            DROP TRIGGER IF EXISTS summaries_fts_update;
            DROP TRIGGER IF EXISTS summaries_fts_delete;
            DROP TABLE IF EXISTS summaries_fts;
            CREATE TABLE legacy_notes (
                id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                key_topics TEXT,
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE summaries_fts USING fts5(
                summary, key_topics, content=legacy_notes, content_rowid=rowid
            );
            CREATE TRIGGER summaries_fts_insert AFTER INSERT ON legacy_notes BEGIN
                INSERT OR IGNORE INTO summaries_fts(rowid,summary,key_topics)
                    VALUES(new.rowid,new.summary,new.key_topics);
            END;
            CREATE TRIGGER summaries_fts_update AFTER UPDATE ON legacy_notes BEGIN
                DELETE FROM summaries_fts WHERE rowid=old.rowid;
                INSERT INTO summaries_fts(rowid,summary,key_topics)
                    VALUES(new.rowid,new.summary,new.key_topics);
            END;
            CREATE TRIGGER summaries_fts_delete AFTER DELETE ON legacy_notes BEGIN
                DELETE FROM summaries_fts WHERE rowid=old.rowid;
            END;
            INSERT INTO threads(id,user_id,title,created_at,updated_at)
                VALUES('history','owner','Saved thread','t','t');
            INSERT INTO legacy_notes(id,summary,created_at)
                VALUES('note','saved legacy narrative','t');
            INSERT INTO messages(id,thread_id,role,content,created_at,delivery_state)
                VALUES('message','history','user','original historyneedle','t','delivered');
            PRAGMA user_version=18;
        """)
        _install_checkpoint(db, thread_id="history", narrative="checkpointneedle")
    return path


def _saved_rows(db):
    return {
        table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("threads", "messages", "legacy_notes", "checkpoints")
    }


def _legacy_objects(db):
    return db.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'summaries_fts%' ORDER BY name"
    ).fetchall()


def test_fresh_schema_has_no_legacy_summary_index():
    with closing(sqlite3.connect(":memory:")) as db:
        assert initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert _legacy_objects(db) == []


def test_upgrade_preserves_every_saved_row_and_active_search(legacy_database):
    with closing(sqlite3.connect(legacy_database)) as db:
        before = _saved_rows(db)
        result = migrate_to_current(db, 19)
        assert (result.from_version, result.to_version) == (18, 19)
        assert result.backup_path is not None
    with closing(sqlite3.connect(legacy_database)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (19,)
        assert _saved_rows(reader) == before
        assert _legacy_objects(reader) == []
        assert reader.execute(
            "SELECT content FROM messages_fts WHERE messages_fts MATCH 'historyneedle'"
        ).fetchall() == [("original historyneedle",)]
        assert reader.execute(
            "SELECT raw_result FROM checkpoints_fts WHERE checkpoints_fts MATCH 'checkpointneedle'"
        ).fetchall() == [("checkpointneedle",)]
    with closing(sqlite3.connect(result.backup_path)) as backup:
        assert backup.execute("PRAGMA user_version").fetchone() == (18,)
        assert _saved_rows(backup) == before
        assert _legacy_objects(backup)


def test_rejected_index_drop_rolls_back_trigger_drops_and_version(legacy_database):
    with closing(sqlite3.connect(legacy_database)) as db:
        before = _saved_rows(db)
        objects = _legacy_objects(db)

        def reject_drop(action, name, _second, _database, _source):
            if action == sqlite3.SQLITE_DROP_VTABLE and name == "summaries_fts":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(reject_drop)
        with pytest.raises(MigrationError, match="rolled back.*not authorized"):
            migrate_to_current(db, 19)
        assert not db.in_transaction
    with closing(sqlite3.connect(legacy_database)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (18,)
        assert _saved_rows(reader) == before
        assert _legacy_objects(reader) == objects
