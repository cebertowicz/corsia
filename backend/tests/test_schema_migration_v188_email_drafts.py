"""Existing and fresh databases index row-owned draft identity without losing settled history."""
import sqlite3

from infra.action_drafts_schema import ACTION_DRAFTS_SCHEMA_SQL
from infra.migration_187_to_188_email_draft_lookup import migrate_187_to_188


def test_upgrade_adds_kind_path_index_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    db.executescript(ACTION_DRAFTS_SCHEMA_SQL)
    db.execute("DROP INDEX idx_action_drafts_kind_path")
    for state in ("open", "sent", "discarded"):
        db.execute("INSERT INTO action_drafts(id,thread_id,kind,path,state,recorded_revision,recorded_at,created_at) "
                   "VALUES (?, 't','email',?,?, 'r','now','now')", (state, state + ".md", state))
    before = db.execute("SELECT * FROM action_drafts ORDER BY id").fetchall()
    migrate_187_to_188(db)
    migrate_187_to_188(db)
    assert db.execute("SELECT * FROM action_drafts ORDER BY id").fetchall() == before
    assert [row[2] for row in db.execute("PRAGMA index_info(idx_action_drafts_kind_path)")] == ["kind", "path"]
    plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM action_drafts WHERE kind=? AND path=? "
                      "ORDER BY created_at DESC LIMIT 1", ("email", "sent.md")).fetchall()
    assert any("USING INDEX idx_action_drafts_kind_path" in row[3] for row in plan)


def test_fresh_schema_has_the_same_index():
    db = sqlite3.connect(":memory:")
    db.executescript(ACTION_DRAFTS_SCHEMA_SQL)
    assert [row[2] for row in db.execute("PRAGMA index_info(idx_action_drafts_kind_path)")] == ["kind", "path"]
