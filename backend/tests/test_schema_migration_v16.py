"""Schema v16: a settled watch is history, waits are indexed, stale expiries end.

Through v15 the watch dedupe index covered every status, so a condition whose
watch had already resolved could never be armed again: the lookup returned that
terminal row and the task waited on something nothing would ever resolve. The
migration replaces the index with one that holds only while a watch is armed,
adds the two thread indexes a wait reads, and cancels the expiry continuations
of watches that had already settled before every settling write began cancelling
its own.
"""

from __future__ import annotations

import sqlite3

import pytest

_CHECKS = "github.check_suite.completed"
_ARMED = (
    "INSERT INTO expected_event_watches "
    "(id,owner_thread_id,artifact_external_id,condition_type,condition_value,"
    " expected_event,status,armed_at,created_at,updated_at) "
    "VALUES (?,'owner',?,'event','ci.complete',?,?,'t','t','t')"
)
_TRIGGER = (
    "INSERT INTO continuation_triggers "
    "(id,kind,thread_id,scope_id,subject_id,dedupe_key,state,created_at,updated_at) "
    "VALUES (?,'t2_watch_expiry','owner','project',?,?,'deferred','t','t')"
)


def _schema_fifteen_database() -> sqlite3.Connection:
    """A v15 database: dedupe across every status, no wait indexes, one orphan."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    # Reverse the three v16 shapes, so the fixture is what v15 actually had.
    db.execute("DROP INDEX IF EXISTS idx_watches_dedupe")
    db.execute(
        "CREATE UNIQUE INDEX idx_watches_dedupe ON expected_event_watches"
        "(artifact_external_id, condition_type, condition_value)"
    )
    db.execute("DROP INDEX IF EXISTS idx_plan_nodes_spawned_thread")
    db.execute("DROP INDEX IF EXISTS idx_plan_nodes_executor_thread")
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('owner','user-1','t','t')"
    )
    db.execute(_ARMED, ("watch-done", "pr-7", _CHECKS, "resolved"))
    db.execute(_ARMED, ("watch-open", "pr-8", _CHECKS, "armed"))
    db.execute(_TRIGGER, ("t2-orphan", "watch-done", "dedupe-done"))
    db.execute(_TRIGGER, ("t2-live", "watch-open", "dedupe-open"))
    db.execute("PRAGMA user_version=15")
    db.commit()
    return db


def _migrated() -> sqlite3.Connection:
    from infra.database_migrations import migrate_to_current

    db = _schema_fifteen_database()
    result = migrate_to_current(db, 16)
    assert (result.migrated, result.from_version, result.to_version) == (True, 15, 16)
    return db


def test_fresh_schema_is_current_version() -> None:
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 120


def test_a_settled_condition_can_be_waited_on_again_and_a_live_one_still_cannot() -> None:
    db = _migrated()
    try:
        # The settled watch's condition is free again.
        db.execute(_ARMED, ("watch-again", "pr-7", _CHECKS, "armed"))
        # A different terminating event on the same artifact is its own wait.
        db.execute(_ARMED, ("watch-deploy", "pr-8", "github.deployment_status", "armed"))

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_ARMED, ("watch-duplicate", "pr-8", _CHECKS, "armed"))
    finally:
        db.close()


def test_the_wait_reads_have_their_indexes() -> None:
    db = _migrated()
    try:
        names = {row["name"] for row in db.execute("PRAGMA index_list('plan_nodes')")}

        assert {"idx_plan_nodes_spawned_thread", "idx_plan_nodes_executor_thread"} <= names
    finally:
        db.close()


def test_only_the_settled_watchs_expiry_is_cancelled() -> None:
    db = _migrated()
    try:
        states = {
            row["id"]: (row["state"], row["reason"])
            for row in db.execute("SELECT id,state,reason FROM continuation_triggers")
        }

        assert states["t2-orphan"] == ("cancelled", "watch_settled")
        assert states["t2-live"][0] == "deferred"
    finally:
        db.close()
