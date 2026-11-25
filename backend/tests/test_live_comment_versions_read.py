"""Reload reads comment rows and their source versions under one owner."""
import sqlite3

import pytest

from doceditor import live_comments
from infra.database import TransactionOwnershipError, get_db, write_transaction


def test_reload_reads_one_snapshot_and_leaves_connection_usable(isolated_db, monkeypatch):
    db = get_db()
    db.execute("INSERT INTO doc_comment_snapshots (id,digest,text) VALUES (1,?,?)", (b'digest', b'old text'))
    db.execute("INSERT INTO doc_comments (id,artifact_id,author,body,created_at,updated_at,snapshot_id) "
               "VALUES ('c','a','person','body','t','t',1)")
    db.commit()
    original = live_comments.comments.list_for_artifact

    def rows_with_competing_writer(artifact_id, *, db):
        rows = original(artifact_id, db=db)
        # A competing commit either waits for this owner or stays outside its snapshot.
        with sqlite3.connect(isolated_db, timeout=0) as other:
            try:
                other.execute("UPDATE doc_comment_snapshots SET text=? WHERE id=1", (b'new text',))
            except sqlite3.OperationalError as error:
                assert 'locked' in str(error)
        return rows

    monkeypatch.setattr(live_comments.comments, 'list_for_artifact', rows_with_competing_writer)
    rows, versions = live_comments._rows_and_versions('a')
    assert rows[0]['snapshot_id'] == 1
    assert versions == {1: 'old text'}
    assert not db.in_transaction
    with write_transaction(None, scope='following_write') as writer:
        writer.execute("UPDATE doc_comments SET body='after reload' WHERE id='c'")
    assert db.execute("SELECT body FROM doc_comments WHERE id='c'").fetchone()[0] == 'after reload'


def test_reload_refuses_an_existing_transaction_without_losing_it(isolated_db):
    db = get_db()
    db.execute("INSERT INTO doc_comment_snapshots (digest,text) VALUES (?,?)", (b'digest', b'owned'))
    try:
        with pytest.raises((TransactionOwnershipError, sqlite3.OperationalError)) as error:
            live_comments._rows_and_versions('a')
        assert db.in_transaction
        assert db.execute("SELECT text FROM doc_comment_snapshots WHERE digest=?", (b'digest',)).fetchone()[0] == b'owned'
        assert isinstance(error.value, TransactionOwnershipError)
    finally:
        db.rollback()
