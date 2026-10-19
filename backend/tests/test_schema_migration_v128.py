"""Schema 126 -> 127: declared measure registry and per-agent, per-day intake counters."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_126_to_127_measure_registry import migrate_126_to_127
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


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v126(*, registry_present: bool = False) -> sqlite3.Connection:
    """A fresh current database wound back to schema 126, with two old counter totals."""
    db = _fresh()
    db.execute("DROP TABLE outcome_event_counters")
    db.execute(_OLD_COUNTERS)
    db.execute("INSERT INTO outcome_event_counters VALUES ('duplicate_ignored','github.pr.opened',7)")
    db.execute("INSERT INTO outcome_event_counters VALUES ('attribute_dropped','github.pr.opened',3)")
    if not registry_present:
        db.execute("DROP TABLE measure_definitions")
        db.execute("DROP TABLE measure_namespaces")
    db.execute("PRAGMA user_version=126")
    db.commit()
    return db


def test_the_step_is_registered_at_126() -> None:
    assert SCHEMA_VERSION == 127
    assert MIGRATIONS[126] is migrate_126_to_127


def test_the_step_matches_a_fresh_schema_and_keeps_old_totals_before_per_day() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v126()) as db:
        migrate_126_to_127(db)
        for table in ("outcome_event_counters", "measure_definitions", "measure_namespaces"):
            assert _columns(db, table) == _columns(fresh, table)
        assert db.execute("SELECT counter,event_name,agent_thread_id,day,n FROM outcome_event_counters ORDER BY counter").fetchall() == [
            ("attribute_dropped", "github.pr.opened", "", "0000-00-00", 3),
            ("duplicate_ignored", "github.pr.opened", "", "0000-00-00", 7),
        ]


def test_the_step_runs_after_the_startup_schema_pass_and_again() -> None:
    with closing(_wound_back_to_v126(registry_present=True)) as db:
        migrate_126_to_127(db)
        migrate_126_to_127(db)
        assert {name for name, _ in _columns(db, "outcome_event_counters")} >= {"agent_thread_id", "day"}
        assert db.execute("SELECT sum(n) FROM outcome_event_counters").fetchone()[0] == 10


def test_the_counter_check_admits_the_five_new_counters() -> None:
    with closing(_wound_back_to_v126()) as db:
        migrate_126_to_127(db)
        for counter in ("name_undeclared", "namespace_not_owned", "identity_mismatch", "rate_limited", "record_refused", "unversioned"):
            db.execute("INSERT INTO outcome_event_counters(counter,event_name,agent_thread_id,day) VALUES (?,?,?,?)", (counter, "mail.triaged", "a", "2026-01-01"))


def test_the_runner_takes_a_126_database_to_current_and_stays_there() -> None:
    with closing(_wound_back_to_v126()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 126, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
