"""Schema v18 keys an armed watch on the artifact's full identity and its owner.

Under the old key -- external id, condition, event -- one integration's pull
request deduplicated against another's, and a second thread's wait on the same
event was refused as another actor's. A watch is one thread's wait on one
artifact's event, and the key now says so: the same shape resolution matches.
"""

from __future__ import annotations

import sqlite3

import pytest

_NARROW_KEY = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_dedupe ON expected_event_watches"
    "(artifact_external_id, condition_type, condition_value, expected_event) "
    "WHERE status='armed'"
)
_ARMED = (
    "INSERT INTO expected_event_watches "
    "(id,artifact_integration,artifact_type,artifact_external_id,owner_thread_id,"
    " expected_event,condition_type,condition_value,status,armed_at,created_at,updated_at) "
    "VALUES (?,?,?,'owner/repo#7',?,'pr.opened','event','ci.complete','armed','t','t','t')"
)


def _schema_seventeen_database() -> sqlite3.Connection:
    """A v17 database: one armed watch, keyed without identity or owner."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("DROP INDEX IF EXISTS idx_watches_dedupe")
    db.execute(_NARROW_KEY)
    db.execute(_ARMED, ("w-1", "github", "pull_request", "thread-a"))
    db.execute("PRAGMA user_version=17")
    db.commit()
    return db


def _migrated() -> sqlite3.Connection:
    from infra.database_migrations import migrate_to_current

    db = _schema_seventeen_database()
    result = migrate_to_current(db, 18)
    assert (result.migrated, result.from_version, result.to_version) == (True, 17, 18)
    return db


def test_fresh_schema_includes_version_eighteen() -> None:
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION >= 18


def test_the_old_key_refused_a_second_holder_and_a_twin_in_another_integration() -> None:
    db = _schema_seventeen_database()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_ARMED, ("w-2", "github", "pull_request", "thread-b"))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_ARMED, ("w-3", "gitlab", "merge_request", "thread-a"))
    finally:
        db.close()


def test_the_new_key_keeps_the_watch_and_admits_both_while_one_thread_still_waits_once() -> None:
    db = _migrated()
    try:
        kept = db.execute(
            "SELECT COUNT(*) FROM expected_event_watches WHERE id='w-1' AND status='armed'",
        ).fetchone()[0]
        assert kept == 1

        # Another thread's wait on the same artifact and event is its own.
        db.execute(_ARMED, ("w-2", "github", "pull_request", "thread-b"))
        # The same external id in another integration is another artifact.
        db.execute(_ARMED, ("w-3", "gitlab", "merge_request", "thread-a"))
        # The same thread waiting on the same event of the same artifact: one watch.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_ARMED, ("w-4", "github", "pull_request", "thread-a"))
    finally:
        db.close()
