"""Document feedback DDL stays inside its caller's migration transaction."""
from contextlib import closing
import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.migration_71_to_72_doc_feedback import migrate_71_to_72
from infra.schema import SCHEMA_VERSION


def _ddl(db):
    return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def test_feedback_step_keeps_callers_transaction_and_exact_ddl_until_rollback():
    with closing(sqlite3.connect(':memory:')) as db, closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(DOC_FEEDBACK_SCHEMA_SQL)
        db.execute('BEGIN IMMEDIATE')
        migrate_71_to_72(db)
        assert db.in_transaction
        assert _ddl(db) == _ddl(reference)
        db.rollback()
        assert not db.in_transaction
        assert _ddl(db) == []
        assert db.execute('SELECT 1').fetchone() == (1,)


@pytest.mark.parametrize('has_outcome', [False, True])
def test_edit_step_keeps_writer_and_exact_ddl_for_both_existing_column_branches(has_outcome):
    from infra.migration_72_to_73_doc_edit_feedback import _EDIT_ITEMS_SQL, migrate_72_to_73

    with closing(sqlite3.connect(':memory:')) as db, closing(sqlite3.connect(':memory:')) as reference:
        for connection in (db, reference):
            connection.execute('CREATE TABLE doc_feedback_batches(id TEXT PRIMARY KEY'
                               + (',delivery_outcome TEXT)' if has_outcome else ')'))
            connection.commit()
        before = list(db.iterdump())
        if not has_outcome:
            reference.execute('ALTER TABLE doc_feedback_batches ADD COLUMN delivery_outcome TEXT '
                              "CHECK (delivery_outcome IS NULL OR delivery_outcome IN ('node','ui','empty'))")
        reference.execute(_EDIT_ITEMS_SQL)
        db.execute('BEGIN IMMEDIATE')
        migrate_72_to_73(db)
        assert db.in_transaction
        assert _ddl(db) == _ddl(reference)
        db.rollback()
        assert not db.in_transaction
        assert list(db.iterdump()) == before
        assert db.execute('SELECT 1').fetchone() == (1,)


def _partial71(db):
    # Synthetic partial schema exercises the canonical migration runner.
    db.execute('CREATE TABLE sentinel(id TEXT PRIMARY KEY,value TEXT)')
    db.execute("INSERT INTO sentinel VALUES ('old','preserved')")
    db.execute('PRAGMA user_version=71')
    db.commit()


def test_actual_startup_ladder_rolls_back_feedback_ddl_and_version_on_later_failure(monkeypatch):
    original = MIGRATIONS[81]
    def later_failure(db):
        original(db)
        raise RuntimeError('synthetic later migration refusal')
    monkeypatch.setitem(MIGRATIONS, 81, later_failure)
    with closing(sqlite3.connect(':memory:')) as db:
        _partial71(db)
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match='synthetic later migration refusal'):
            migrate_to_current(db, SCHEMA_VERSION)
        assert not db.in_transaction
        assert list(db.iterdump()) == before
        assert db.execute('PRAGMA user_version').fetchone()[0] == 71
        assert db.execute('SELECT * FROM sentinel').fetchall() == [('old','preserved')]


def test_actual_startup_ladder_commits_feedback_once_without_losing_old_rows():
    with closing(sqlite3.connect(':memory:')) as db:
        _partial71(db)
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 71, SCHEMA_VERSION)
        assert not db.in_transaction
        assert db.execute('SELECT * FROM sentinel').fetchall() == [('old','preserved')]
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            'doc_feedback_batches','doc_feedback_batch_items','doc_feedback_edit_items','doc_comment_thread_views'}
        after = list(db.iterdump())
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert list(db.iterdump()) == after
