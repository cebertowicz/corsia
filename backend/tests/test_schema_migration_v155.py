"""Schema 154 -> 155: each thread's write-once launch confinement."""
from __future__ import annotations

import sqlite3

import pytest

from infra import thread_confinement
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_154_to_155_thread_confinement import migrate_154_to_155
from infra.schema import SCHEMA_VERSION


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("ALTER TABLE threads DROP COLUMN confinement")
    db.execute("ALTER TABLE threads DROP COLUMN confined_read_dirs")
    db.execute("PRAGMA user_version=154")
    db.execute("INSERT INTO users (id,password_hash,created_at,password_changed_at) VALUES ('u','x','t','t')")
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('old','u','t','t','t')")
    db.commit()
    return db


def _columns(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute("PRAGMA table_info(threads)")]


def test_v155_is_registered() -> None:
    assert SCHEMA_VERSION == 164
    assert MIGRATIONS[154] is migrate_154_to_155


def test_v155_upgrade_leaves_existing_threads_unconfined_and_matches_fresh() -> None:
    fresh = sqlite3.connect(":memory:")
    db = _legacy_db()
    try:
        initialize_schema(fresh)
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 164
        assert sorted(_columns(db)) == sorted(_columns(fresh))
        assert thread_confinement.read_dirs("old", db) is None
        assert {r[1] for r in db.execute("PRAGMA index_list(event_route_decisions)")} == {
            r[1] for r in fresh.execute("PRAGMA index_list(event_route_decisions)")}
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    finally:
        db.close()
        fresh.close()


def _cc_session(db, thread_id: str, record_id: str, invocation: dict, updated: str) -> None:
    import json

    db.execute("INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
               "VALUES (?,'claude-code','session',?,?,'t',?)",
               (record_id, record_id, json.dumps({"claude_invocation": invocation}), updated))
    db.execute("INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,created_at,updated_at) "
               "VALUES (?,?,?,'t','t')", (f"reg-{record_id}", record_id, thread_id))


def test_v155_carries_an_older_claude_code_review_childs_confinement_onto_its_row() -> None:
    db = _legacy_db()
    try:
        for thread_id in ("review", "plain", "empty"):
            db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,'u','t','t','t')",
                       (thread_id,))
        db.execute("UPDATE threads SET spawned_by='old' WHERE id IN ('review','plain','empty')")
        _cc_session(db, "review", "old", {"confinement": "review", "add_dirs": ["/old"]}, "1")
        _cc_session(db, "review", "new", {"confinement": "review", "add_dirs": ["/src", "/head"]}, "2")
        _cc_session(db, "plain", "p", {"confinement": "none", "add_dirs": []}, "1")
        _cc_session(db, "empty", "e", {"confinement": "review"}, "1")
        db.commit()
        migrate_to_current(db, SCHEMA_VERSION)
        assert thread_confinement.read_dirs("review", db) == ("/src", "/head")
        assert thread_confinement.read_dirs("plain", db) is None
        assert thread_confinement.read_dirs("old", db) is None
        with pytest.raises(thread_confinement.ConfinementRecordDamaged):
            thread_confinement.read_dirs("empty", db)  # fails closed, never unconfined
    finally:
        db.close()


def test_damaged_top_level_session_stays_unconfined() -> None:
    db = _legacy_db()
    try:
        _cc_session(db, "old", "broken-top", {}, "1")
        db.execute("UPDATE artifact_identities SET metadata='not json' WHERE id='broken-top'")
        db.commit()
        migrate_to_current(db, SCHEMA_VERSION)
        assert thread_confinement.read_dirs("old", db) is None
    finally:
        db.close()


def test_damaged_legacy_session_refuses_launch() -> None:
    from subagents.cc_confinement import ConfinementRefused, launch_confinement

    db = _legacy_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('parent','u','t','t','t')")
        db.execute("UPDATE threads SET spawned_by='parent' WHERE id='old'")
        _cc_session(db, "old", "broken", {}, "1")
        db.execute("UPDATE artifact_identities SET metadata='not json' WHERE id='broken'")
        db.commit()
        migrate_to_current(db, SCHEMA_VERSION)
        db.row_factory = sqlite3.Row
        with pytest.raises(ConfinementRefused):
            launch_confinement("old", None, db)
    finally:
        db.close()


@pytest.mark.parametrize("invocation", [
    {"confinement": "admin"},
    {"confinement": 1},
    {"confinement": "none", "add_dirs": ["/x"]},
    {"confinement": "none", "add_dirs": None},
])
def test_unknown_or_malformed_legacy_confinement_refuses_launch(invocation) -> None:
    from subagents.cc_confinement import ConfinementRefused, launch_confinement

    db = _legacy_db()
    try:
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('parent','u','t','t','t')")
        db.execute("UPDATE threads SET spawned_by='parent' WHERE id='old'")
        _cc_session(db, "old", "odd", invocation, "2")
        _cc_session(db, "old", "plain", {"confinement": "none", "add_dirs": []}, "1")
        db.commit()
        migrate_to_current(db, SCHEMA_VERSION)
        db.row_factory = sqlite3.Row
        with pytest.raises(ConfinementRefused):
            launch_confinement("old", None, db)
    finally:
        db.close()


def test_v155_step_is_idempotent_and_skips_an_absent_table() -> None:
    db = _legacy_db()
    empty = sqlite3.connect(":memory:")
    try:
        migrate_154_to_155(db)
        migrate_154_to_155(db)
        assert {"confinement", "confined_read_dirs"} <= {name for name, _ in _columns(db)}
        migrate_154_to_155(empty)
        assert empty.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        db.close()
        empty.close()


def test_confinement_is_written_once_and_a_damaged_record_fails_closed() -> None:
    db = sqlite3.connect(":memory:")
    try:
        initialize_schema(db)
        db.execute("INSERT INTO users (id,password_hash,created_at,password_changed_at) VALUES ('u','x','t','t')")
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('w','u','t','t','t')")
        thread_confinement.record(db, "w", "untrusted", ("/tmp/a",))
        assert thread_confinement.read_dirs("w", db) == ("/tmp/a",)
        with pytest.raises(ValueError, match="already has a confinement"):
            thread_confinement.record(db, "w", "untrusted", ("/",))
        with pytest.raises(ValueError, match="only an untrusted confinement"):
            thread_confinement.record(db, "w", "untrusted", ())
        db.execute("UPDATE threads SET confined_read_dirs='not json' WHERE id='w'")
        with pytest.raises(thread_confinement.ConfinementRecordDamaged):
            thread_confinement.read_dirs("w", db)
        db.execute("UPDATE threads SET confinement='other', confined_read_dirs='[\"/tmp/a\"]' WHERE id='w'")
        with pytest.raises(thread_confinement.ConfinementRecordDamaged):
            thread_confinement.read_dirs("w", db)
    finally:
        db.close()
