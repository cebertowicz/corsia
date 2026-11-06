"""Migration puts an agent's ask for the person on the comment, where the unread read can see it."""

import sqlite3

from infra.migration_181_to_182_comment_tags_person import migrate_181_to_182


def _old_shape(db):
    """A database as the shape before this step left it, including one tag of each kind."""
    db.execute("""CREATE TABLE doc_comments (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, author_kind TEXT NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE doc_comment_tags (
        comment_id TEXT NOT NULL, agent_id TEXT NOT NULL, agent_name TEXT NOT NULL,
        PRIMARY KEY (comment_id, agent_id))""")
    db.executemany(
        "INSERT INTO doc_comments(id,artifact_id,author_kind,body,created_at) VALUES (?,?,?,?,?)",
        [('tagged', 'doc', 'agent', 'Needs your call', '2026-11-01T00:00:00+00:00'),
         ('plain', 'doc', 'agent', 'An ordinary note', '2026-11-01T00:00:00+00:00')],
    )
    db.execute("INSERT INTO doc_comment_tags VALUES ('tagged','ui','steve')")
    db.execute("INSERT INTO doc_comment_tags VALUES ('tagged','agent-1','Reviewer')")


def test_a_person_tag_row_becomes_the_comments_own_mark_and_the_agent_row_stays():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _old_shape(db)
    migrate_181_to_182(db)
    assert [dict(row) for row in db.execute(
        "SELECT id,tags_person,body FROM doc_comments ORDER BY id"
    )] == [
        {"id": "plain", "tags_person": 0, "body": "An ordinary note"},
        {"id": "tagged", "tags_person": 1, "body": "Needs your call"},
    ]
    assert [row["agent_id"] for row in db.execute(
        "SELECT agent_id FROM doc_comment_tags ORDER BY agent_id"
    )] == ["agent-1"]
    db.close()


def test_the_step_needs_no_tag_table_and_holds_when_run_twice():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments(id TEXT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO doc_comments VALUES ('comment', 'kept')")
    migrate_181_to_182(db)
    migrate_181_to_182(db)
    assert db.execute("SELECT body,tags_person FROM doc_comments").fetchone() == ("kept", 0)
    db.close()
