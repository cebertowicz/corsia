"""Schema v28 retires any leftover waiting_retry Turn into a dead-boot orphan."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _legacy_database(tmp_path):
    path = tmp_path / "legacy-waiting-retry.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads(id,user_id,model,created_at,updated_at) "
            "VALUES('thread-legacy','user-1','claude-code/opus','t0','t0')"
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,retry_at) "
            "VALUES('legacy-turn','thread-legacy',1,'claude-code/opus',"
            "'waiting_retry','t0','ancient-boot','2099')"
        )
        # A queued input already proved not to have reached the provider --
        # the only shape the retired recovery branch ever accepted.
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,created_at,delivery_state,input_order,"
            "delivery_intent,active_turn_id,delivery_attempt_id,write_started_at,"
            "write_outcome,next_attempt_at) "
            "VALUES('legacy-input','thread-legacy','user','stuck','t0','queued',1,"
            "'apply_now','legacy-turn','dead-attempt','t0','proved_no_write','t1')"
        )
        db.execute("PRAGMA user_version=27")
        db.commit()
    return path


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 169


def test_migration_retires_lingering_waiting_retry_turns(tmp_path):
    path = _legacy_database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, 28)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 27, 28,
        )

        turn = db.execute(
            "SELECT status,boot_epoch,retry_at FROM active_turns WHERE id='legacy-turn'"
        ).fetchone()
        assert tuple(turn) == ("running", "ancient-boot", None)

        message = db.execute(
            "SELECT delivery_attempt_id,write_started_at,write_outcome,next_attempt_at "
            "FROM messages WHERE id='legacy-input'"
        ).fetchone()
        assert tuple(message) == (None, None, None, None)

        assert not migrate_to_current(db, 28).migrated


def test_migration_leaves_an_unresolved_write_for_the_dead_boot_path():
    """A message mid-write (not proved_no_write) is left for ambiguous_stale_writes."""
    from infra.migration_27_to_28_retire_waiting_retry import migrate_27_to_28

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads(id,user_id,model,created_at,updated_at) "
            "VALUES('thread-legacy','user-1','claude-code/opus','t0','t0')"
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,retry_at) "
            "VALUES('legacy-turn','thread-legacy',1,'claude-code/opus',"
            "'waiting_retry','t0','ancient-boot','2099')"
        )
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,created_at,delivery_state,input_order,"
            "delivery_intent,active_turn_id,delivery_attempt_id,write_started_at,"
            "write_outcome) "
            "VALUES('mid-write','thread-legacy','user','pending','t0','queued',1,"
            "'apply_now','legacy-turn','live-attempt','t0',NULL)"
        )
        db.commit()

        migrate_27_to_28(db)

        row = db.execute(
            "SELECT write_started_at,write_outcome FROM messages WHERE id='mid-write'"
        ).fetchone()
        assert tuple(row) == ("t0", None)
        assert db.execute(
            "SELECT status,retry_at FROM active_turns WHERE id='legacy-turn'"
        ).fetchone()["status"] == "running"
    finally:
        db.close()
