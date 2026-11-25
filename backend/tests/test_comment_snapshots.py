"""Snapshot identity and lifecycle in a disposable database."""
import sqlite3

import pytest

from doceditor import comment_snapshots


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE doc_comment_snapshots (id INTEGER PRIMARY KEY, digest BLOB NOT NULL, text BLOB NOT NULL);
        CREATE INDEX snapshots_digest ON doc_comment_snapshots(digest);
        CREATE TABLE doc_comments (id TEXT PRIMARY KEY, snapshot_id INTEGER REFERENCES doc_comment_snapshots(id));
    ''')
    yield connection
    connection.close()


def test_exact_bytes_share_and_digest_collision_does_not(db, monkeypatch):
    class Digest:
        def digest(self):
            return b'collision'

    monkeypatch.setattr(comment_snapshots.hashlib, 'sha256', lambda _data: Digest())
    db.execute('BEGIN IMMEDIATE')
    first = comment_snapshots.intern(db, 'one 🐈')
    assert comment_snapshots.intern(db, 'one 🐈') == first
    second = comment_snapshots.intern(db, 'two')
    assert second != first
    assert comment_snapshots.intern(db, 'two') == second
    db.commit()
    assert comment_snapshots.read(db, first) == 'one 🐈'
    with pytest.raises(LookupError, match='missing'):
        comment_snapshots.read(db, second + 1)


def test_read_many_deduplicates_ids_and_rejects_a_broken_reference(db):
    db.execute('BEGIN IMMEDIATE')
    first = comment_snapshots.intern(db, 'first')
    second = comment_snapshots.intern(db, 'second')
    assert comment_snapshots.read_many(db, [first, first, second]) == {first: 'first', second: 'second'}
    with pytest.raises(LookupError, match='missing'):
        comment_snapshots.read_many(db, [first, second + 1])


def test_collection_and_reference_changes_roll_back_together(db):
    db.execute('BEGIN IMMEDIATE')
    first = comment_snapshots.intern(db, 'first')
    second = comment_snapshots.intern(db, 'second')
    db.execute('INSERT INTO doc_comments VALUES (?, ?)', ('c', first))
    db.commit()
    db.execute('BEGIN IMMEDIATE')
    db.execute('UPDATE doc_comments SET snapshot_id=? WHERE id=?', (second, 'c'))
    assert comment_snapshots.collect_candidates(db, [first, second]) == 1
    db.rollback()
    assert db.execute('SELECT snapshot_id FROM doc_comments').fetchone()[0] == first
    assert db.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 2
    db.execute('BEGIN IMMEDIATE')
    assert comment_snapshots.collect_candidates(db, [first, second]) == 1
    db.commit()
    assert db.execute('SELECT id FROM doc_comment_snapshots').fetchall() == [(first,)]


def test_collect_candidates_only_removes_unreferenced_prior_versions(db):
    db.execute('BEGIN IMMEDIATE')
    shared = comment_snapshots.intern(db, 'shared')
    old = comment_snapshots.intern(db, 'old')
    untouched = comment_snapshots.intern(db, 'untouched')
    db.execute('INSERT INTO doc_comments VALUES (?, ?)', ('c', shared))
    assert comment_snapshots.collect_candidates(db, [shared, old, old]) == 1
    assert db.execute('SELECT id FROM doc_comment_snapshots ORDER BY id').fetchall() == [(shared,), (untouched,)]
    db.rollback()


def test_deferred_snapshot_intern_requires_retry_after_competing_commit(tmp_path):
    path = tmp_path / 'snapshots.sqlite'
    first = sqlite3.connect(path, timeout=0)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute('PRAGMA journal_mode=WAL')
        first.executescript('''
            CREATE TABLE doc_comment_snapshots (id INTEGER PRIMARY KEY, digest BLOB NOT NULL, text BLOB NOT NULL);
            CREATE INDEX snapshots_digest ON doc_comment_snapshots(digest);
        ''')
        first.execute('BEGIN')
        assert first.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 0
        second.execute('BEGIN IMMEDIATE')
        winner = comment_snapshots.intern(second, 'same')
        second.commit()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            comment_snapshots.intern(first, 'same')
        first.rollback()
        first.execute('BEGIN IMMEDIATE')
        assert comment_snapshots.intern(first, 'same') == winner
        first.commit()
        assert first.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 1
    finally:
        first.close()
        second.close()


def test_caller_must_own_transaction(db):
    with pytest.raises(ValueError, match='owning transaction'):
        comment_snapshots.intern(db, 'text')
    with pytest.raises(ValueError, match='owning transaction'):
        comment_snapshots.collect_candidates(db, [1])
