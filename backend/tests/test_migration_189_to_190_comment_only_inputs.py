"""Schema 190: comment-only inputs stored before 190 draw nothing in chat, as later ones do."""
import json
import sqlite3
from contextlib import closing

from infra.migration_189_to_190_comment_only_inputs import migrate_189_to_190
from infra.database import initialize_schema

COMMENTS = {'reply_context': {'comments': [{'cid': 'c1.1', 'seq': 1}]}}


def _message(db, message_id, role, content, metadata):
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,'thread',?,?,?,'t')",
        (message_id, role, content, json.dumps(metadata) if metadata is not None else None),
    )


def test_marks_only_comment_only_inputs_and_rewrites_nothing():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _message(db, 'comment-only', 'user', '', COMMENTS)
        _message(db, 'blank-text', 'user', '  \n', COMMENTS)
        _message(db, 'with-text', 'user', 'also this', COMMENTS)
        _message(db, 'with-file', 'user', '', {**COMMENTS, 'attachments': [{'name': 'a.png'}]})
        _message(db, 'no-comments', 'user', '', {'reply_context': {'comments': []}})
        _message(db, 'plain', 'user', '', None)
        _message(db, 'answer', 'assistant', '', COMMENTS)
        db.execute("INSERT INTO chat_visible_content VALUES ('saved','thread','kept text')")
        migrate_189_to_190(db)
        migrate_189_to_190(db)
        rows = db.execute("SELECT message_id,content FROM chat_visible_content ORDER BY message_id").fetchall()
        assert rows == [('blank-text', ''), ('comment-only', ''), ('saved', 'kept text')]
