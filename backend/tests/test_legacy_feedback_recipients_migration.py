"""Pending legacy feedback survives normalization and collision handling."""
import sqlite3

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.migration_199_to_200_feedback_recipients import migrate_199_to_200


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
    migrate_199_to_200(db)
    assert db.execute("SELECT id,recipient_id FROM doc_feedback_batches ORDER BY id").fetchall() == [
        ('delivered', 'project:p'), ('new', 'ui'), ('retry', 'ui')]
    assert db.execute("SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id='new' ORDER BY comment_id").fetchall() == [('c1',), ('c2',)]
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id='new'").fetchone()[0] == 2
    db.rollback()
    db.close()


def test_unmergeable_collision_is_unchanged_and_does_not_prevent_other_rows(caplog):
    db = _db()
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    _batch(db, 'later', 'project:p', 'undelivered', 'b')
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('old','c','reopened','1','a')")
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('new','c','reopened','2','b')")
    before = db.execute("SELECT * FROM doc_feedback_batches WHERE id IN ('old','new') ORDER BY id").fetchall()
    items = db.execute("SELECT * FROM doc_feedback_batch_items ORDER BY batch_id").fetchall()
    db.commit()
    db.execute('PRAGMA user_version=199')
    from infra.database_migrations import migrate_to_current

    result = migrate_to_current(db, 200)
    assert result.migrated
    assert db.execute('PRAGMA user_version').fetchone()[0] == 200
    assert db.execute("SELECT value FROM settings WHERE key='person_comment_recovery_pending'").fetchone() == ('1',)
    assert db.execute("SELECT * FROM doc_feedback_batches WHERE id IN ('old','new') ORDER BY id").fetchall() == before
    assert db.execute("SELECT * FROM doc_feedback_batch_items ORDER BY batch_id").fetchall() == items
    assert db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id='later'").fetchone()[0] == 'ui'
    assert 'could not be merged' in caplog.text
    db.close()


def test_referenced_collision_is_retained_with_foreign_keys_disabled():
    db = _db()
    db.execute('CREATE TABLE external_link(batch_id TEXT REFERENCES doc_feedback_batches(id))')
    _batch(db, 'old', 'project:p')
    _batch(db, 'new', 'ui')
    db.execute("INSERT INTO external_link VALUES ('old')")
    db.commit()
    db.execute('PRAGMA user_version=199')
    from infra.database_migrations import migrate_to_current

    assert migrate_to_current(db, 200).migrated
    assert db.execute("SELECT id,recipient_id FROM doc_feedback_batches ORDER BY id").fetchall() == [('new', 'ui'), ('old', 'project:p')]
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
