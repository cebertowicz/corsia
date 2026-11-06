"""Schema 177 -> 178: a clock schedule stored with an offset moves to its UTC instant."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_177_to_178_clock_utc import migrate_177_to_178
from infra.schema import SCHEMA_VERSION


def _at(version: int, *rows: tuple[str, str, str, str | None]) -> sqlite3.Connection:
    """A database at schema ``version`` holding ``(id, kind, state, next_run_at)`` schedules."""
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute(f"PRAGMA user_version={version}")
    db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('t','user','t','t')")
    db.executemany(
        """INSERT INTO automation_schedules
           (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,
            created_by_actor,created_at,updated_at)
           VALUES (?,'t','s',?,'spec',?,1,?,'agent','now','now')""",
        [(row_id, kind, next_run_at, state) for row_id, kind, state, next_run_at in rows],
    )
    db.commit()
    return db


def _next_run(db: sqlite3.Connection, row_id: str) -> str | None:
    return db.execute(
        "SELECT next_run_at FROM automation_schedules WHERE id=?", (row_id,),
    ).fetchone()[0]


def test_the_step_is_registered_at_177() -> None:
    assert SCHEMA_VERSION == 181
    assert MIGRATIONS[177] is migrate_177_to_178


def test_an_offset_moves_to_its_utc_instant_and_a_utc_row_stays_byte_identical() -> None:
    with closing(_at(
        177,
        ("west", "clock", "active", "2026-11-05T09:00:00-06:00"),
        ("east", "clock", "active", "2026-11-05T09:00:00+09:00"),
        ("utc", "clock", "active", "2026-11-05T15:00:00+00:00"),
        ("zulu", "clock", "active", "2026-11-05T15:00:00Z"),
        ("no-offset", "clock", "active", "2026-11-05T15:00:00"),
    )) as db:
        migrate_177_to_178(db)

        assert _next_run(db, "west") == "2026-11-05T15:00:00+00:00"
        assert _next_run(db, "east") == "2026-11-05T00:00:00+00:00"
        assert _next_run(db, "utc") == "2026-11-05T15:00:00+00:00"
        assert _next_run(db, "zulu") == "2026-11-05T15:00:00Z"
        assert _next_run(db, "no-offset") == "2026-11-05T15:00:00"


def test_every_other_row_stays_as_it_is() -> None:
    offset = "2026-11-05T09:00:00-06:00"
    with closing(_at(
        177,
        ("cadence", "cadence", "active", offset),
        ("script", "script", "active", None),
        ("finished", "clock", "exhausted", offset),
        ("cancelled", "clock", "cancelled", offset),
    )) as db:
        migrate_177_to_178(db)

        assert _next_run(db, "cadence") == offset
        assert _next_run(db, "script") is None
        assert _next_run(db, "finished") == offset
        assert _next_run(db, "cancelled") == offset


def test_a_database_at_176_or_177_reaches_the_current_schema_with_the_row_moved() -> None:
    for version in (176, 177):
        with closing(_at(version, ("west", "clock", "active", "2026-11-05T09:00:00-06:00"))) as db:
            migrate_to_current(db, SCHEMA_VERSION)

            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert _next_run(db, "west") == "2026-11-05T15:00:00+00:00"


def test_the_step_is_safe_to_run_again() -> None:
    with closing(_at(177, ("west", "clock", "active", "2026-11-05T09:00:00-06:00"))) as db:
        migrate_177_to_178(db)
        migrate_177_to_178(db)

        assert _next_run(db, "west") == "2026-11-05T15:00:00+00:00"
