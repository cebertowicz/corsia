"""The person-step migration preserves existing updates and schema objects."""

import sqlite3

from infra.database_migrations import migrate_to_current
from infra.migration_62_to_63_thread_updates import migrate_62_to_63
from infra.migration_150_to_151_person_step import migrate_150_to_151


def test_update_kinds_migrate_without_losing_rows_or_objects():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO threads VALUES ('thread')")
    migrate_62_to_63(db)
    db.execute("CREATE TABLE audit(value TEXT)")
    db.execute("CREATE TRIGGER updates_audit AFTER INSERT ON thread_updates BEGIN INSERT INTO audit VALUES (NEW.kind); END")
    for i, kind in enumerate(("marked", "done", "done_follow_up", "failed", "closed", "blocked")):
        db.execute(
            "INSERT INTO thread_updates(thread_id,kind,source_kind,source_id,node_id,title,after_seq,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("thread", kind, "message" if kind == "marked" else "plan_event", str(i),
             None if kind == "marked" else str(i), None if kind == "marked" else kind, i, "2026-01-01"),
        )
    before = db.execute("SELECT * FROM thread_updates ORDER BY id").fetchall()
    db.execute("PRAGMA user_version=150")
    db.commit()
    assert migrate_to_current(db, 151).migrated is True
    assert db.execute("PRAGMA user_version").fetchone()[0] == 151
    assert migrate_to_current(db, 151).migrated is False
    migrate_150_to_151(db)
    assert db.execute("SELECT * FROM thread_updates ORDER BY id").fetchall() == before
    indexes = {row[1] for row in db.execute("PRAGMA index_list(thread_updates)")}
    assert {"idx_thread_updates_thread_time", "idx_thread_updates_thread_seq", "idx_thread_updates_node"} <= indexes
    db.execute(
        "INSERT INTO thread_updates(thread_id,kind,source_kind,source_id,node_id,title,after_seq,created_at) "
        "VALUES ('thread','person_step','plan_event','new','node','A step',0,'2026-01-01')"
    )
    assert db.execute("SELECT value FROM audit ORDER BY rowid DESC LIMIT 1").fetchone() == ("person_step",)


def test_rebuild_rolls_back_with_the_startup_transaction():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")
    migrate_62_to_63(db)
    db.commit()
    old_schema = db.execute("SELECT sql FROM sqlite_master WHERE name='thread_updates'").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    migrate_150_to_151(db)
    db.rollback()
    assert db.execute("SELECT sql FROM sqlite_master WHERE name='thread_updates'").fetchone()[0] == old_schema
    assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'idx_thread_updates_%'").fetchone()[0] == 3
