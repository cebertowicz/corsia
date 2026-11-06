"""Migration puts an agent's ask for the person on the comment, where the unread read can see it."""

import sqlite3

from infra.migration_181_to_182_comment_tags_person import migrate_181_to_182


def test_existing_comments_gain_an_unset_person_mark_and_keep_their_text():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments(id TEXT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO doc_comments VALUES ('comment', 'kept')")
    migrate_181_to_182(db)
    migrate_181_to_182(db)
    assert db.execute("SELECT body,tags_person FROM doc_comments").fetchone() == ("kept", 0)
    db.close()


def test_a_database_without_the_comments_table_is_left_alone():
    db = sqlite3.connect(":memory:")
    migrate_181_to_182(db)
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    db.close()
