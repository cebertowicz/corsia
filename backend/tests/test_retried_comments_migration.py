"""Schema 166 drops a retried send's unanswered comments, as a new retry does."""
import json
import sqlite3
from contextlib import closing

from infra.database_migrations import migrate_to_current


def old_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages(id TEXT, thread_id TEXT, role TEXT, metadata TEXT)")
    db.execute("CREATE TABLE chat_comments(thread_id TEXT, target_message_id TEXT, user_message_id TEXT, cid TEXT, created_at TEXT)")
    db.execute("CREATE TABLE chat_comment_answers(thread_id TEXT, user_message_id TEXT, cid TEXT, assistant_message_id TEXT, answered_at TEXT)")
    db.execute("PRAGMA user_version=165")
    return db


def test_drops_only_unanswered_rows_of_a_retried_send():
    with closing(old_database()) as db:
        db.executemany("INSERT INTO messages VALUES (?,?,?,?)", [
            ('failed', 'thread', 'user', '{}'),
            ('retry', 'thread', 'user', json.dumps({'retried_from_message_id': 'failed'})),
            ('kept', 'thread', 'user', '{}'),
            ('broken', 'thread', 'user', 'retried_from_message_id but not json'),
            ('elsewhere', 'other', 'user', json.dumps({'retried_from_message_id': 'kept'})),
        ])
        db.executemany("INSERT INTO chat_comments VALUES ('thread','target',?,?,'t')", [
            ('failed', 'c1.1'), ('failed', 'c1.2'), ('retry', 'c1.1'), ('retry', 'c1.2'), ('kept', 'c2.1')])
        db.execute("INSERT INTO chat_comment_answers VALUES ('thread','failed','c1.2','answer','u')")
        db.commit()
        migrate_to_current(db, 166)
        rows = db.execute("SELECT user_message_id,cid FROM chat_comments ORDER BY user_message_id,cid").fetchall()
        assert rows == [('failed', 'c1.2'), ('kept', 'c2.1'), ('retry', 'c1.1'), ('retry', 'c1.2')]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 166


def test_a_database_without_chat_comments_migrates():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("PRAGMA user_version=165")
        migrate_to_current(db, 166)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 166
