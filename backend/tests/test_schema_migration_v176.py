"""Migration adds comment tags and per-recipient state without changing comments."""

import sqlite3

from infra.migration_175_to_176_comment_recipients import migrate_175_to_176


def test_migration_adds_comment_recipient_tables():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments(id TEXT PRIMARY KEY, body TEXT)")
    db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO doc_comments VALUES ('comment', 'kept')")
    migrate_175_to_176(db)
    db.execute("INSERT INTO doc_comment_tags VALUES ('comment', 'agent', 'Agent')")
    db.execute("INSERT INTO doc_comment_recipient_state(root_id, agent_id) VALUES ('comment', 'agent')")
    assert db.execute("SELECT body FROM doc_comments").fetchone() == ("kept",)
    assert db.execute("SELECT agent_name FROM doc_comment_tags").fetchone() == ("Agent",)
    assert db.execute("SELECT left_thread, notifications FROM doc_comment_recipient_state").fetchone() == (0, 0)
    db.close()
