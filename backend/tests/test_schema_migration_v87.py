"""Schema 87 moves document-level reply views onto each comment thread."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from doceditor.feedback_state import annotate_reply_waiting_threads, reply_waiting_ids
from infra.migration_86_to_87_doc_comment_thread_views import migrate_86_to_87

_V86 = """
    CREATE TABLE doc_comments (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, parent_id TEXT,
        author_kind TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE doc_comment_views (
        user_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
        viewed_at TEXT NOT NULL, PRIMARY KEY (user_id, artifact_id)
    );
"""
SEEN = '2026-10-02T00:00:00+00:00'


def _v86() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(_V86)
    db.executemany(
        "INSERT INTO doc_comments VALUES (?,?,?,?,0,?)",
        [
            ('root-a', 'doc-1', None, 'user', '2026-10-01T00:00:00+00:00'),
            ('a-seen', 'doc-1', 'root-a', 'agent', SEEN),
            ('root-b', 'doc-1', None, 'user', '2026-10-01T00:00:00+00:00'),
            ('b-new', 'doc-1', 'root-b', 'agent', '2026-10-02T01:00:00+00:00'),
            ('root-c', 'doc-2', None, 'user', '2026-10-01T00:00:00+00:00'),
            ('c-new', 'doc-2', 'root-c', 'agent', '2026-10-02T01:00:00+00:00'),
        ],
    )
    db.execute("INSERT INTO doc_comment_views VALUES ('ui', 'doc-1', ?)", (SEEN,))
    return db


def test_v87_carries_each_document_stamp_to_its_threads_and_drops_the_old_table():
    with closing(_v86()) as db:
        before = reply_waiting_ids_before_thread_views(db)
        migrate_86_to_87(db)

        stamps = {
            row["root_id"]: (row["seen_at"], row["seen_reply_id"])
            for row in db.execute("SELECT * FROM doc_comment_thread_views WHERE user_id='ui'")
        }
        assert stamps == {'root-a': (SEEN, 'a-seen'), 'root-b': (SEEN, '')}
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='doc_comment_views'",
        ).fetchone() is None
        assert reply_waiting_ids(db, 'ui') == before == {'doc-1', 'doc-2'}
        threads = [{'id': 'root-a'}, {'id': 'root-b'}]
        annotate_reply_waiting_threads(db, 'ui', 'doc-1', threads)
        assert [t['reply_waiting'] for t in threads] == [False, True]


def test_v87_is_idempotent_without_the_old_table():
    with closing(_v86()) as db:
        migrate_86_to_87(db)
        migrate_86_to_87(db)
        assert db.execute("SELECT COUNT(*) FROM doc_comment_thread_views").fetchone()[0] == 2


def reply_waiting_ids_before_thread_views(db) -> set[str]:
    """The prior rule: a non-user reply after the document stamp is waiting."""
    rows = db.execute(
        """SELECT DISTINCT root.artifact_id FROM doc_comments reply
           JOIN doc_comments root ON root.id=reply.parent_id
           LEFT JOIN doc_comment_views view
             ON view.user_id='ui' AND view.artifact_id=root.artifact_id
           WHERE reply.author_kind<>'user' AND root.resolved=0
             AND reply.created_at>COALESCE(view.viewed_at, '')""",
    ).fetchall()
    return {row[0] for row in rows}
