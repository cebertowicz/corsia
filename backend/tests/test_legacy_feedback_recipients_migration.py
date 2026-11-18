"""Pending legacy feedback survives normalization and collision handling."""
import sqlite3

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.migration_200_to_201_feedback_recipients import migrate_200_to_201


def _db():
    db = sqlite3.connect(':memory:')
    db.executescript("""CREATE TABLE artifact_identities(id TEXT PRIMARY KEY);
        CREATE TABLE doc_comments(id TEXT PRIMARY KEY);
        CREATE TABLE plan_nodes(id TEXT PRIMARY KEY);
        INSERT INTO artifact_identities VALUES ('a'),('b'),('c');
        INSERT INTO doc_comments VALUES ('c'),('c1'),('c2');""")
    from infra.settings_schema import SETTINGS_SCHEMA_SQL
    db.executescript(SETTINGS_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL)
    return db


def test_fresh_schema_has_no_journal_and_reverse_is_a_noop():
    from infra.migration_200_to_201_feedback_recipients import reverse_201_to_200

    db = _db()
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_recipient_migration_journal'").fetchone() is None
    reverse_201_to_200(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_recipient_migration_journal'").fetchone() is None
    db.close()


def test_upgrade_without_pending_legacy_rows_has_no_journal():
    from infra.migration_200_to_201_feedback_recipients import reverse_201_to_200

    db = _db()
    migrate_200_to_201(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_recipient_migration_journal'").fetchone() is None
    reverse_201_to_200(db)
    db.close()


def _batch(db, batch_id, recipient, state='open', artifact='a'):
    db.execute("""INSERT INTO doc_feedback_batches
        (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,
         opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
        VALUES (?,?,'user','user',?,'immediate',?,'1','2','3',1,'1','2')""",
        (batch_id, artifact, recipient, state))


def test_pending_rows_and_mergeable_open_collision_are_normalized():
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    _batch(db, 'retry', 'project:p', 'undelivered', 'b')
    _batch(db, 'delivered', 'project:p', 'delivered', 'c')
    db.execute("INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES ('old','c1'),('new','c2')")
    db.execute('BEGIN') if not db.in_transaction else None
    migrate_200_to_201(db)
    assert db.execute("SELECT id,recipient_id FROM doc_feedback_batches ORDER BY id").fetchall() == [
        ('delivered', 'project:p'), ('new', 'ui'), ('retry', 'ui')]
    assert db.execute("SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id='new' ORDER BY comment_id").fetchall() == [('c1',), ('c2',)]
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id='new'").fetchone()[0] == 2
    db.rollback()
    db.close()


def test_unmergeable_collision_is_unchanged_and_does_not_prevent_other_rows(caplog):
    import logging
    caplog.set_level(logging.INFO)
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    _batch(db, 'later', 'project:p', 'undelivered', 'b')
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('old','c','reopened','1','a')")
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('new','c','reopened','2','b')")
    before = db.execute("SELECT * FROM doc_feedback_batches WHERE id IN ('old','new') ORDER BY id").fetchall()
    items = db.execute("SELECT * FROM doc_feedback_batch_items ORDER BY batch_id").fetchall()
    db.commit()
    db.execute('PRAGMA user_version=200')
    from infra.database_migrations import migrate_to_current

    result = migrate_to_current(db, 201)
    assert result.migrated
    assert db.execute('PRAGMA user_version').fetchone()[0] == 201
    assert db.execute("SELECT value FROM settings WHERE key='person_comment_recovery_pending'").fetchone() == ('1',)
    assert db.execute("SELECT * FROM doc_feedback_batches WHERE id IN ('old','new') ORDER BY id").fetchall() == before
    assert db.execute("SELECT * FROM doc_feedback_batch_items ORDER BY batch_id").fetchall() == items
    assert db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id='later'").fetchone()[0] == 'ui'
    assert 'reopen_conflicts=1' in caplog.text
    db.close()


def test_referenced_collision_is_retained_with_foreign_keys_disabled(caplog):
    import logging
    caplog.set_level(logging.INFO)
    db = _db()
    db.execute('CREATE TABLE external_link(batch_id TEXT REFERENCES doc_feedback_batches(id))')
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.execute("INSERT INTO external_link VALUES ('old')")
    db.commit()
    db.execute('PRAGMA user_version=200')
    from infra.database_migrations import migrate_to_current

    assert migrate_to_current(db, 201).migrated
    assert db.execute("SELECT id,recipient_id FROM doc_feedback_batches ORDER BY id").fetchall() == [('new', 'ui'), ('old', 'project:p')]
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert 'referenced=1' in caplog.text
    db.close()


def test_edit_snapshots_fold_only_when_compatible():
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.execute("INSERT INTO doc_feedback_edit_items VALUES ('old','Person','diff','a','old','b','new',1,'1','2')")
    before = db.execute('SELECT * FROM doc_feedback_edit_items').fetchall()
    migrate_200_to_201(db)
    assert db.execute('SELECT batch_id FROM doc_feedback_edit_items').fetchone() == ('new',)
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id='new'").fetchone() == (1,)
    from infra.migration_200_to_201_feedback_recipients import reverse_201_to_200
    reverse_201_to_200(db)
    assert db.execute('SELECT * FROM doc_feedback_edit_items').fetchall() == before
    db.close()


def test_identical_edit_snapshots_fold_as_one_item():
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.executemany("INSERT INTO doc_feedback_edit_items VALUES (?,'Person','diff','a','old','b','new',1,'1','2')", [('old',), ('new',)])
    migrate_200_to_201(db)
    assert db.execute('SELECT batch_id FROM doc_feedback_edit_items').fetchall() == [('new',)]
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id='new'").fetchone() == (1,)
    db.close()


def test_different_edit_snapshots_leave_source_open(caplog):
    import logging
    caplog.set_level(logging.INFO)
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.execute("INSERT INTO doc_feedback_edit_items VALUES ('old','Person','diff','a','old','b','new',1,'1','2')")
    db.execute("INSERT INTO doc_feedback_edit_items VALUES ('new','Person','diff','a','old','c','other',1,'1','2')")
    before = db.execute('SELECT * FROM doc_feedback_edit_items ORDER BY batch_id').fetchall()
    migrate_200_to_201(db)
    assert db.execute('SELECT * FROM doc_feedback_edit_items ORDER BY batch_id').fetchall() == before
    assert db.execute("SELECT state,recipient_id FROM doc_feedback_batches WHERE id='old'").fetchone() == ('open', 'project:p')
    assert 'edit_conflicts=1' in caplog.text
    db.close()


def test_reverse_is_atomic_when_an_earlier_batch_changed():
    import pytest
    from infra.migration_200_to_201_feedback_recipients import reverse_201_to_200

    db = _db()
    _batch(db, 'a-old', 'project:p', artifact='a')
    _batch(db, 'a-new', 'ui', artifact='a')
    _batch(db, 'z-old', 'project:p', artifact='b')
    _batch(db, 'z-new', 'ui', artifact='b')
    migrate_200_to_201(db)
    db.execute("UPDATE doc_feedback_batches SET item_count=5 WHERE id='a-new'")
    with pytest.raises(RuntimeError, match='changed after migration'):
        reverse_201_to_200(db)
    assert db.execute("SELECT id FROM doc_feedback_batches ORDER BY id").fetchall() == [('a-new',), ('z-new',)]
    assert db.execute('SELECT COUNT(*) FROM feedback_recipient_migration_journal').fetchone() == (2,)
    db.close()


def test_reverse_restores_folded_rows_and_refuses_later_change():
    from infra.migration_200_to_201_feedback_recipients import reverse_201_to_200

    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.execute("INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES ('old','c1'),('new','c2')")
    db.execute("INSERT INTO doc_feedback_edit_items VALUES ('old','Person','diff','a','old','b','new',1,'1','2')")
    before = db.execute('SELECT * FROM doc_feedback_batches ORDER BY id').fetchall()
    items = db.execute('SELECT * FROM doc_feedback_batch_items ORDER BY batch_id').fetchall()
    edits = db.execute('SELECT * FROM doc_feedback_edit_items ORDER BY batch_id').fetchall()
    migrate_200_to_201(db)
    db.execute("UPDATE doc_feedback_batches SET state='delivered' WHERE id='new'")
    import pytest
    with pytest.raises(RuntimeError, match='changed after migration'):
        reverse_201_to_200(db)
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE id='new'").fetchone() == ('delivered',)
    db.execute("UPDATE doc_feedback_batches SET state='open' WHERE id='new'")
    reverse_201_to_200(db)
    assert db.execute('SELECT * FROM doc_feedback_batches ORDER BY id').fetchall() == before
    assert db.execute('SELECT * FROM doc_feedback_batch_items ORDER BY batch_id').fetchall() == items
    assert db.execute('SELECT * FROM doc_feedback_edit_items ORDER BY batch_id').fetchall() == edits
    db.close()
