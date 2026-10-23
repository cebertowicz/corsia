"""Schema v30 adds the user presence tables (#2004)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_PRESENCE_COLUMNS = {
    "user_id", "thread_id", "present", "last_entered_at", "last_exited_at",
    "previous_entered_at", "exit_pending_at", "exit_pending_since",
    "exit_pending_reason", "last_confirmed_at", "turn_order_at_exit",
    "away_seconds", "turns_while_away", "updated_at",
}
# v64 adds the seen-state marks to the same table.
_SEEN_COLUMNS = {"marked_read_at", "marked_unread_at"}
# v126 adds the focus spans to both tables.
_FOCUS_COLUMNS = {"focus_spans", "focus_since"}
_VISIT_COLUMNS = {
    "id", "user_id", "surface", "thread_id", "project_id", "object_id",
    "entered_at", "exited_at", "seconds", "sessions", "exit_reason",
}


def _columns(db, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 143


def test_fresh_schema_creates_the_presence_tables():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert _columns(db, "thread_presence") == _PRESENCE_COLUMNS | _SEEN_COLUMNS | _FOCUS_COLUMNS
        assert _columns(db, "presence_visits") == _VISIT_COLUMNS | {"focus_spans"}


def test_startup_migration_adds_the_tables_to_an_existing_database(tmp_path):
    path = tmp_path / "legacy-presence.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE thread_presence")
        db.execute("DROP TABLE presence_visits")
        db.execute("PRAGMA user_version=29")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 30)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 29, 30,
        )
        assert _columns(db, "thread_presence") == _PRESENCE_COLUMNS
        assert _columns(db, "presence_visits") == _VISIT_COLUMNS
        indexes = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_%presence%' AND name NOT LIKE 'idx_presence_focus%'"  # the 125->126 rollup is not this step's
            )
        }
        assert indexes == {
            "idx_thread_presence_thread",
            "idx_presence_visits_user_time",
            "idx_presence_visits_thread",
        }
        assert not migrate_to_current(db, 30).migrated
