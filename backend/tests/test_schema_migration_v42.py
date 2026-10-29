"""Schema v42 adds what each user has seen of a thread's plan in the pane."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy

# The table as the current schema leaves it; v49 dropped ``folded_through``.
_COLUMNS = {"user_id", "thread_id", "seen_through", "updated_at"}


def _columns(db) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(plan_pane_views)")}


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 163


def test_fresh_schema_creates_the_pane_view_table():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert _columns(db) == _COLUMNS


def test_startup_migration_adds_the_table_to_an_existing_database(tmp_path):
    path = tmp_path / "legacy-pane-views.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE plan_pane_views")
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=41")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 41, SCHEMA_VERSION)
        assert _columns(db) == _COLUMNS
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_migration_carries_each_readers_last_exit_over_as_seen(tmp_path):
    """What the previous build folded (settled before the last exit) stays
    folded; a thread the reader never left has seen nothing yet."""
    path = tmp_path / "seeded-pane-views.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE plan_pane_views")
        for thread in ("left", "offset", "still-here"):
            db.execute(
                "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES (?, 'u', 'x', 'x')",
                (thread,),
            )
        db.executemany(
            "INSERT INTO thread_presence (user_id, thread_id, present, last_entered_at,"
            " last_exited_at, last_confirmed_at, updated_at)"
            " VALUES ('u', ?, ?, '2026-09-24T09:00:00.000000', ?, 'x', 'x')",
            [
                ("left", 0, "2026-09-24T10:00:00.000000"),
                ("offset", 0, "2026-09-24T03:00:00-07:00"),
                ("still-here", 1, None),
            ],
        )
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=41")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        rows = db.execute(
            "SELECT thread_id, seen_through FROM plan_pane_views ORDER BY thread_id"
        ).fetchall()
    assert rows == [
        ("left", "2026-09-24T10:00:00.000000+00:00"),
        ("offset", "2026-09-24T10:00:00.000000+00:00"),
    ]
