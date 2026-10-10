"""Schema v66 deletes checkpoint state; schema v67 adds comment quote context."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import logging
import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_TABLES = (
    "checkpoint_staged_edits",
    "checkpoints",
    "checkpoint_entry_search",
    "continuity_entries",
    "continuity_store",
)
_NOW = "2026-09-29T00:00:00+00:00"


def _add_thread_state(db: sqlite3.Connection, thread_id: str) -> None:
    """Write one row per cleared table for ``thread_id``."""
    checkpoint_id = f"cp-{thread_id}"
    order = db.execute(
        "INSERT INTO checkpoints(id,thread_id,history_epoch,through_seq,through_message_id,"
        "trigger,mode,status,source_harness,source_model,writer_harness,writer_model,"
        "continuity_base_revision,continuity_base_snapshot_json,started_at) "
        "VALUES (?,?,0,1,'m1','manual','warm','installed','native','m','native','m',0,'{}',?)",
        (checkpoint_id, thread_id, _NOW),
    ).lastrowid
    db.execute(
        "INSERT INTO checkpoint_staged_edits(checkpoint_id,edit_order,operation,entry_id,field) "
        "VALUES (?,1,'drop',1,'facts')",
        (checkpoint_id,),
    )
    db.execute(
        "INSERT INTO checkpoint_entry_search(thread_id,history_epoch,entry_id,revision,field,"
        "search_text,first_generation_order,last_generation_order) "
        "VALUES (?,0,1,1,'facts','narrative text',?,?)",
        (thread_id, order, order),
    )
    db.execute(
        "INSERT INTO continuity_store(thread_id,history_epoch,updated_at) VALUES (?,0,?)",
        (thread_id, _NOW),
    )
    db.execute(
        "INSERT INTO continuity_entries(thread_id,history_epoch,entry_id,field,body,"
        "created_at,updated_at) VALUES (?,0,1,'facts','{}',?,?)",
        (thread_id, _NOW, _NOW),
    )


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _TABLES}


def test_migration_deletes_deleted_threads_rows_keeps_live_ones(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('live','u',?,?)",
            (_NOW, _NOW),
        )
        _add_thread_state(db, "live")
        _add_thread_state(db, "gone")
        # A staged edit whose checkpoint row is already gone is an orphan too.
        db.execute(
            "INSERT INTO checkpoint_staged_edits(checkpoint_id,edit_order,operation,entry_id,"
            "field) VALUES ('cp-missing',1,'drop',1,'facts')"
        )
        db.execute("PRAGMA user_version=65")
        db.commit()
        assert _counts(db) == {**dict.fromkeys(_TABLES, 2), "checkpoint_staged_edits": 3}

        with caplog.at_level(logging.INFO, logger="infra.migration_65_to_66_delete_orphaned_thread_state"):
            result = migrate_to_current(db, 66)

        assert SCHEMA_VERSION == 106
        assert (result.migrated, result.from_version, result.to_version) == (True, 65, 66)
        assert _counts(db) == dict.fromkeys(_TABLES, 1)
        for table in _TABLES:
            assert db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                + ("checkpoint_id='cp-live'" if table == "checkpoint_staged_edits" else "thread_id='live'")
            ).fetchone()[0] == 1
        deleted = {"checkpoint_staged_edits": 2, **dict.fromkeys(_TABLES[1:], 1)}
        for table, count in deleted.items():
            assert f"deleted {count} orphaned {table} rows" in caplog.text
        assert db.execute(
            "SELECT COUNT(*) FROM checkpoint_entry_search_fts WHERE checkpoint_entry_search_fts "
            "MATCH 'narrative'"
        ).fetchone()[0] == 1

        rewind_to_v88_policy(db)


        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=65")
        db.commit()
        again = migrate_to_current(db, SCHEMA_VERSION)
        assert again.migrated
        assert _counts(db) == dict.fromkeys(_TABLES, 1)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_startup_migration_adds_comment_context_columns_idempotently() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE doc_comments (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                anchor_text TEXT,
                from_off INTEGER,
                to_off INTEGER
            );
            PRAGMA user_version=66;
        """)

        result = migrate_to_current(db, SCHEMA_VERSION)

        columns = {row[1] for row in db.execute("PRAGMA table_info(doc_comments)")}
        assert SCHEMA_VERSION == 106
        assert (result.migrated, result.from_version, result.to_version) == (True, 66, SCHEMA_VERSION)
        assert {"anchor_prefix", "anchor_suffix"} <= columns
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
