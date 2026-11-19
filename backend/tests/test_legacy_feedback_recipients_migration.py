"""Irreversible legacy recipient repair preserves queued feedback and delivered history."""
import sqlite3

import pytest

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL
from infra.migration_202_to_203_feedback_recipients import migrate_202_to_203, reverse_203_to_202


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript("""CREATE TABLE artifact_identities(id TEXT PRIMARY KEY, path TEXT, title TEXT, external_id TEXT NOT NULL);
        CREATE TABLE doc_comments(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE plan_nodes(id TEXT PRIMARY KEY);
        CREATE TABLE projects(id TEXT PRIMARY KEY, conductor_thread_id TEXT);
        INSERT INTO artifact_identities VALUES ('a','a','A','a'),('b','b','B','b'),('c','c','C','c');
        INSERT INTO doc_comments VALUES ('c','0'),('c1','1'),('c2','2');
        INSERT INTO projects VALUES ('p','conductor'),('empty',NULL);""")
    from infra.settings_schema import SETTINGS_SCHEMA_SQL
    conn.executescript(SETTINGS_SCHEMA_SQL + DOC_FEEDBACK_SCHEMA_SQL)
    yield conn
    conn.close()


def _batch(db, batch_id, recipient, state='open', artifact='a', start='1', end='2', deadline='3'):
    db.execute("""INSERT INTO doc_feedback_batches
        (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,
         opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
        VALUES (?,?,'user','ui',?,'immediate',?,?,?,?,1,?,?)""",
        (batch_id, artifact, recipient, state, start, end, deadline, start, end))


def _edit(db, batch, before='old\n', after='new\n', first='1', last='2', prior='', saves=1):
    import hashlib
    db.execute('INSERT INTO doc_feedback_edit_items VALUES (?,?,?,?,?,?,?,?,?,?)',
               (batch, 'Person', prior, hashlib.sha256(before.encode()).hexdigest(), before,
                hashlib.sha256(after.encode()).hexdigest(), after, saves, first, last))


def _snapshot(db):
    return [db.execute(f'SELECT * FROM {table} ORDER BY 1,2').fetchall() for table in
            ('doc_feedback_batches', 'doc_feedback_batch_items', 'doc_feedback_edit_items')]


def test_normalizes_conductor_and_absent_project_fallback_without_journal(db):
    _batch(db, 'open', 'project:p')
    _batch(db, 'retry', 'project:empty', 'undelivered', 'b')
    _batch(db, 'missing', 'project:missing', 'undelivered', 'c')
    migrate_202_to_203(db)
    assert db.execute('SELECT id,recipient_id FROM doc_feedback_batches ORDER BY id').fetchall() == [
        ('missing', 'ui'), ('open', 'conductor'), ('retry', 'ui')]
    assert db.execute("SELECT value FROM settings WHERE key='person_comment_recovery_pending'").fetchone() == ('1',)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='feedback_recipient_migration_journal'").fetchone() is None
    repaired = _snapshot(db)
    reverse_203_to_202(db)
    migrate_202_to_203(db)
    assert _snapshot(db) == repaired


def test_delivered_batches_items_snapshots_and_counts_untouched(db):
    _batch(db, 'delivered', 'project:p', 'delivered')
    db.execute("UPDATE doc_feedback_batches SET item_count=42,attempts=4,delivery_outcome='ui',closed_reason='send' WHERE id='delivered'")
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('delivered','c','reopened','2','person')")
    _edit(db, 'delivered')
    original = _snapshot(db)
    migrate_202_to_203(db)
    reverse_203_to_202(db)
    assert _snapshot(db) == original


@pytest.mark.parametrize('foreign_keys', [False, True])
def test_collisions_repoint_all_foreign_key_columns_before_deleting_source(db, foreign_keys):
    db.commit()
    db.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')
    db.execute('CREATE TABLE "external link" ("first batch" TEXT REFERENCES doc_feedback_batches(id), second TEXT REFERENCES doc_feedback_batches(id))')
    _batch(db, 'old', 'project:p')
    _batch(db, 'target', 'conductor')
    db.execute('INSERT INTO "external link" VALUES (?,?)', ('old', 'old'))
    db.execute("INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES ('old','c1'),('target','c2')")
    _edit(db, 'old')
    migrate_202_to_203(db)
    assert db.execute('SELECT * FROM "external link"').fetchone() == ('target', 'target')
    assert db.execute('SELECT id,recipient_id,item_count FROM doc_feedback_batches').fetchall() == [('target', 'conductor', 3)]
    assert db.execute('SELECT batch_id FROM doc_feedback_edit_items').fetchall() == [('target',)]
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('source_earlier', [False, True])
def test_conflicting_reopens_keep_first_queued_event_regardless_of_destination(db, source_earlier):
    _batch(db, 'old', 'project:p', start='0', end='9', deadline='99')
    _batch(db, 'target', 'conductor', start='1', end='8', deadline='100')
    source_at, target_at = ('2', '7') if source_earlier else ('7', '2')
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('old','c','reopened',?,'source')", (source_at,))
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('target','c','reopened',?,'target')", (target_at,))
    migrate_202_to_203(db)
    expected_actor = 'source' if source_earlier else 'target'
    assert db.execute('SELECT event,event_at,event_by FROM doc_feedback_batch_items').fetchall() == [('reopened', '2', expected_actor)]
    # touch_batch uses the latest item's deadline, even when a previous edit's delay was longer.
    assert db.execute('SELECT opened_at,last_item_at,closes_at,created_at,updated_at,item_count FROM doc_feedback_batches').fetchone() == ('0', '9', '99', '0', '9', 1)


def test_initial_comment_precedes_reopen_even_in_batch_opened_later(db):
    _batch(db, 'old', 'project:p', start='0')
    _batch(db, 'target', 'conductor', start='1')
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('old','c','reopened','2','source')")
    db.execute("INSERT INTO doc_feedback_batch_items VALUES ('target','c',NULL,NULL,NULL)")
    migrate_202_to_203(db)
    assert db.execute('SELECT event,event_at,event_by FROM doc_feedback_batch_items').fetchone() == (None, None, None)


@pytest.mark.parametrize('source_earlier', [False, True])
def test_contiguous_edit_snapshots_match_runtime_coalescing(db, source_earlier):
    from pathlib import Path
    from doceditor.feedback_edits import record_edit_item
    _batch(db, 'old', 'project:p')
    _batch(db, 'target', 'conductor')
    early, late = ('old', 'target') if source_earlier else ('target', 'old')
    _edit(db, early, 'zero\n', 'one\n', '1', '2', saves=2)
    _edit(db, late, 'one\n', 'two\n', '3', '4', saves=3)
    migrate_202_to_203(db)
    # The canonical runtime writer is the oracle for how a continuous save extends a segment.
    expected = sqlite3.connect(':memory:')
    expected.row_factory = sqlite3.Row
    expected.executescript(DOC_FEEDBACK_SCHEMA_SQL)
    _edit(expected, 'oracle', 'zero\n', 'one\n', '1', '2', saves=4)
    record_edit_item(expected, batch_id='oracle', actor_display='Person', path=Path('a'), before=b'one\n', after=b'two\n', now='4')
    actual = db.execute('SELECT * FROM doc_feedback_edit_items').fetchone()[1:]
    oracle = tuple(expected.execute('SELECT * FROM doc_feedback_edit_items').fetchone())[1:]
    assert actual == oracle
    expected.close()
    assert db.execute('SELECT item_count FROM doc_feedback_batches').fetchone() == (1,)


def test_discontinuous_edit_snapshots_preserve_closed_diffs_and_latest_segment(db):
    from pathlib import Path
    from registry.shared_edit_classification import capped_unified_diff, combine_capped_diffs
    _batch(db, 'old', 'project:p')
    _batch(db, 'target', 'conductor')
    _edit(db, 'target', 'old\n', 'edited\n', '1', '2', prior='older diff', saves=2)
    _edit(db, 'old', 'external\n', 'latest\n', '3', '4', prior='intervening diff', saves=3)
    migrate_202_to_203(db)
    row = db.execute('SELECT prior_segments_diff,baseline_text,latest_text,save_count,first_at,last_at FROM doc_feedback_edit_items').fetchone()
    assert row == (combine_capped_diffs('older diff', capped_unified_diff(Path('a'), b'old\n', b'edited\n'), 'intervening diff'),
                   'external\n', 'latest\n', 5, '1', '4')


def test_fold_ledger_recounts_only_pending_targets_and_reverse_preserves_repairs(db):
    db.execute('CREATE TABLE comment_identity_moves(table_name TEXT,row_id TEXT,original TEXT,moved_to TEXT,moved_at TEXT)')
    for batch, state, recipient in [('open', 'open', 'ui'), ('retry', 'undelivered', 'project:p'), ('delivered', 'delivered', 'project:p')]:
        _batch(db, batch, recipient, state)
        _edit(db, batch)
        db.execute('INSERT INTO doc_feedback_batch_items(batch_id,comment_id) VALUES (?,?)', (batch, 'c1'))
        db.execute('INSERT INTO comment_identity_moves VALUES (?,?,?,?,?)', ('doc_feedback_batch_folds', batch, '{}', batch, '1'))
    _batch(db, 'unrelated', 'ui', artifact='b')
    migrate_202_to_203(db)
    assert db.execute('SELECT id,item_count FROM doc_feedback_batches ORDER BY id').fetchall() == [
        ('delivered', 1), ('open', 2), ('retry', 2), ('unrelated', 1)]
    repaired = _snapshot(db)
    reverse_203_to_202(db)
    assert _snapshot(db) == repaired
    assert db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id='retry'").fetchone() == ('conductor',)


def test_schema_200_runs_notice_and_feedback_migrations_with_recovery_marker(db):
    from infra.database_migrations import migrate_to_current
    db.execute('CREATE TABLE message_update_state(id TEXT PRIMARY KEY)')
    _batch(db, 'old', 'project:p')
    db.commit()
    db.execute('PRAGMA user_version=200')
    assert migrate_to_current(db, 203).migrated
    assert 'notice_at' in {row[1] for row in db.execute('PRAGMA table_info(message_update_state)')}
    assert db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id='old'").fetchone() == ('conductor',)
    assert db.execute('PRAGMA user_version').fetchone() == (203,)
    assert db.execute("SELECT value FROM settings WHERE key='person_comment_recovery_pending'").fetchone() == ('1',)


def test_migration_framework_rolls_back_repair_and_references_on_failure(db):
    from infra.database_migrations import migrate_to_current
    _batch(db, 'old', 'project:p')
    _batch(db, 'target', 'conductor')
    db.execute('CREATE TABLE refs(batch TEXT REFERENCES doc_feedback_batches(id))')
    db.execute("INSERT INTO refs VALUES ('old')")
    db.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON doc_feedback_batches BEGIN SELECT RAISE(ABORT,'cannot delete'); END")
    db.commit()
    db.execute('PRAGMA user_version=201')
    before = _snapshot(db)
    with pytest.raises(Exception, match='cannot delete'):
        migrate_to_current(db, 203)
    assert _snapshot(db) == before
    assert db.execute('SELECT * FROM refs').fetchone() == ('old',)
    assert db.execute('PRAGMA user_version').fetchone() == (201,)


def test_all_legacy_sources_fold_in_edit_chronology_even_when_destination_is_newest(db):
    db.executemany('INSERT INTO projects VALUES (?,?)', [('second', 'conductor'), ('third', 'conductor')])
    _batch(db, 'oldest', 'project:p', start='1')
    _batch(db, 'middle', 'project:second', start='2')
    _batch(db, 'later', 'project:third', start='3')
    _batch(db, 'target', 'conductor', start='4')
    _edit(db, 'oldest', 'a\n', 'b\n', '1', '2')
    _edit(db, 'middle', 'x\n', 'y\n', '3', '4')
    _edit(db, 'later', 'u\n', 'v\n', '5', '6')
    _edit(db, 'target', 'm\n', 'n\n', '7', '8')
    migrate_202_to_203(db)
    assert db.execute('SELECT id,recipient_id,item_count FROM doc_feedback_batches').fetchall() == [('target', 'conductor', 1)]
    prior, baseline, latest, saves, first, last = db.execute('SELECT prior_segments_diff,baseline_text,latest_text,save_count,first_at,last_at FROM doc_feedback_edit_items').fetchone()
    assert prior.index('-a\n+b') < prior.index('-x\n+y') < prior.index('-u\n+v')
    assert (baseline, latest, saves, first, last) == ('m\n', 'n\n', 4, '1', '8')
    repaired = _snapshot(db)
    reverse_203_to_202(db)
    migrate_202_to_203(db)
    assert _snapshot(db) == repaired


def test_overlapping_edit_snapshots_keep_both_changes_and_newest_snapshot(db):
    _batch(db, 'old', 'project:p')
    _batch(db, 'target', 'conductor')
    _edit(db, 'old', 'a\n', 'b\n', '1', '9', prior='early history', saves=2)
    _edit(db, 'target', 'x\n', 'y\n', '2', '3', prior='other history', saves=3)
    migrate_202_to_203(db)
    prior, baseline, latest, saves, first, last = db.execute('SELECT prior_segments_diff,baseline_text,latest_text,save_count,first_at,last_at FROM doc_feedback_edit_items').fetchone()
    assert 'early history' in prior and 'other history' in prior and '-x\n+y' in prior
    assert (baseline, latest, saves, first, last) == ('a\n', 'b\n', 5, '1', '9')


@pytest.mark.parametrize(('path', 'external_id', 'title', 'header'), [
    ('/workspace/shared/Review notes.md', '/workspace/shared/Old.md', 'Old title.md', '/workspace/shared/Review notes.md'),
    (None, '/workspace/shared/External notes.md', 'Old title.md', '/workspace/shared/External notes.md'),
    (None, '', 'Review notes.md', 'Review notes.md'),
    (None, '', None, 'document'),
])
def test_discontinuous_diff_headers_use_identity_path_or_display_name(db, path, external_id, title, header):
    artifact_id = '4af73c85-8c5e-4ede-a9fa-79c9c54e61be'
    db.execute('INSERT INTO artifact_identities VALUES (?,?,?,?)', (artifact_id, path, title, external_id))
    _batch(db, 'old', 'project:p', artifact=artifact_id)
    _batch(db, 'target', 'conductor', artifact=artifact_id)
    _edit(db, 'old', 'before\n', 'edited\n', '1', '2')
    _edit(db, 'target', 'external\n', 'latest\n', '3', '4')
    migrate_202_to_203(db)
    (saved_diff,) = db.execute('SELECT prior_segments_diff FROM doc_feedback_edit_items').fetchone()
    assert saved_diff.startswith(f'--- {header}\n+++ {header}\n')
    assert artifact_id not in saved_diff
