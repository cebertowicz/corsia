"""Schema 127 -> 128: declared measure registry, per-agent per-day intake counters, a schedule's last measures."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_127_to_128_measure_registry import migrate_127_to_128
from infra.schema import SCHEMA_VERSION

_OLD_COUNTERS = """
    CREATE TABLE outcome_event_counters (
        counter    TEXT NOT NULL CHECK (counter IN (
                     'duplicate_ignored', 'attribute_dropped', 'mapping_failed')),
        event_name TEXT NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (counter, event_name)
    )
"""


def _columns(db: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute(f"PRAGMA table_info({table})")]


def _key(db: sqlite3.Connection) -> list[str]:
    """The counters' primary key columns in key order."""
    return [row[1] for row in sorted(db.execute("PRAGMA table_info(outcome_event_counters)"), key=lambda row: row[5]) if row[5]]


def _index_sql(db: sqlite3.Connection, name: str = "idx_outcome_events_agent_observed") -> str | None:
    row = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return " ".join(row[0].split()) if row else None


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v127(*, registry_present: bool = False) -> sqlite3.Connection:
    """A fresh current database wound back to schema 127, with two old counter totals."""
    db = _fresh()
    db.execute("DROP TABLE outcome_event_counters")
    db.execute(_OLD_COUNTERS)
    db.execute("INSERT INTO outcome_event_counters VALUES ('duplicate_ignored','github.pr.opened',7)")
    db.execute("INSERT INTO outcome_event_counters VALUES ('attribute_dropped','github.pr.opened',3)")
    if not registry_present:
        db.execute("DROP TABLE measure_definitions")
        db.execute("ALTER TABLE automation_schedules DROP COLUMN last_measures")
    db.execute("DROP INDEX idx_outcome_events_agent_observed")
    # Step 127's comments index is part of a 127 database.
    assert _index_sql(db, "idx_doc_comments_user_created") is not None
    db.execute("PRAGMA user_version=127")
    db.commit()
    return db


def test_the_step_is_registered_at_127() -> None:
    assert SCHEMA_VERSION == 145
    assert MIGRATIONS[127] is migrate_127_to_128


def test_the_step_matches_a_fresh_schema_and_keeps_old_totals_before_per_day() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v127()) as db:
        migrate_127_to_128(db)
        for table in ("outcome_event_counters", "measure_definitions", "automation_schedules"):
            assert _columns(db, table) == _columns(fresh, table)
        assert ("last_measures", "TEXT") in _columns(db, "automation_schedules")
        assert _key(db) == _key(fresh) == ["agent_thread_id", "day", "event_name", "counter"]
        for index in ("idx_outcome_events_agent_observed", "idx_measure_definitions_kind_name"):
            assert _index_sql(db, index) == _index_sql(fresh, index) and _index_sql(db, index) is not None
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('measure_namespaces','measure_workflows')").fetchone()
        assert db.execute("SELECT counter,event_name,agent_thread_id,day,n FROM outcome_event_counters ORDER BY counter").fetchall() == [
            ("attribute_dropped", "github.pr.opened", "", "0000-00-00", 3),
            ("duplicate_ignored", "github.pr.opened", "", "0000-00-00", 7),
        ]


def test_the_step_runs_after_the_startup_schema_pass_and_again() -> None:
    with closing(_wound_back_to_v127(registry_present=True)) as db:
        migrate_127_to_128(db)
        migrate_127_to_128(db)
        assert {name for name, _ in _columns(db, "outcome_event_counters")} >= {"agent_thread_id", "day"}
        assert db.execute("SELECT sum(n) FROM outcome_event_counters").fetchone()[0] == 10


def test_the_counter_check_admits_the_four_new_counters_and_nothing_unwritten() -> None:
    with closing(_wound_back_to_v127()) as db:
        migrate_127_to_128(db)
        for counter in ("name_undeclared", "namespace_not_owned", "record_refused", "unversioned"):
            db.execute("INSERT INTO outcome_event_counters(counter,event_name,agent_thread_id,day) VALUES (?,?,?,?)", (counter, "mail.triaged", "a", "2026-01-01"))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO outcome_event_counters(counter,event_name,agent_thread_id,day) VALUES ('rate_limited','mail.triaged','a','2026-01-01')")


def test_the_runner_takes_a_127_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v127()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 127, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_measure_definitions_carries_a_removed_flag_that_admits_only_zero_or_one() -> None:
    with closing(_wound_back_to_v127()) as db:
        migrate_127_to_128(db)
        assert ("removed", "INTEGER") in _columns(db, "measure_definitions")
        row = ("a", "event", "mail.x", 1, "{}", "/p", "sha", "t")
        db.execute("INSERT INTO measure_definitions VALUES (?,?,?,?,?,?,?,?,1)", row)
        db.execute("INSERT INTO measure_definitions(owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at) VALUES (?,?,?,?,?,?,?,?)", (*row[:2], "mail.y", *row[3:]))
        assert db.execute("SELECT removed FROM measure_definitions ORDER BY name").fetchall() == [(1,), (0,)]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO measure_definitions VALUES (?,?,?,?,?,?,?,?,2)", (*row[:2], "mail.z", *row[3:]))
