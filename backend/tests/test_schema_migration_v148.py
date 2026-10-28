"""Schema 147 -> 148: one triage fact and one terminal outcome per correction subject."""
from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_147_to_148_correction_learning import migrate_147_to_148
from infra.schema import SCHEMA_VERSION

_INDEXES = ("idx_learning_loop_outcome_subject", "idx_learning_loop_triage_subject")


def _indexes(db) -> list[str]:
    return sorted(
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_learning_loop_%'"
        )
    )


def _legacy_db() -> sqlite3.Connection:
    # Startup migrations receive sqlite's default positional rows.
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    for name in _INDEXES:
        db.execute(f"DROP INDEX {name}")
    db.execute("PRAGMA user_version=147")
    db.commit()
    return db


def _fact(db, event_name: str, subject: str, key: str) -> None:
    db.execute(
        "INSERT INTO outcome_events (id,dedupe_key,event_name,source,provenance,observed_ts,ts,subject_key,attributes) "
        "VALUES (?,?,?,'corsia','agent_recorded','t','t',?,'{}')",
        (key, key, event_name, subject),
    )


def test_v148_fresh_schema_has_both_indexes() -> None:
    assert SCHEMA_VERSION == 158
    assert MIGRATIONS[147] is migrate_147_to_148
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert _indexes(db) == sorted(_INDEXES)


def test_v148_failure_after_the_step_rolls_back_the_indexes_and_version(monkeypatch) -> None:
    def failing_step(db):
        migrate_147_to_148(db)
        raise RuntimeError("synthetic failure after the v148 step")

    monkeypatch.setitem(MIGRATIONS, 147, failing_step)
    db = _legacy_db()
    try:
        _fact(db, "learning_loop.correction_triage", "message:one", "a")
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="synthetic failure after the v148 step"):
            migrate_to_current(db, SCHEMA_VERSION)
        assert not db.in_transaction
        assert db.execute("PRAGMA user_version").fetchone()[0] == 147
        assert list(db.iterdump()) == before
        assert _indexes(db) == []
    finally:
        db.close()


def test_v148_upgrade_matches_a_fresh_install_and_enforces_one_fact_per_subject() -> None:
    db = _legacy_db()
    try:
        _fact(db, "learning_loop.correction_triage", "message:one", "a")
        _fact(db, "learning_loop.correction_outcome", "message:one", "b")
        _fact(db, "measurement.correction", "message:one", "c")
        db.commit()
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 158
        assert _indexes(db) == sorted(_INDEXES)
        with pytest.raises(sqlite3.IntegrityError):
            _fact(db, "learning_loop.correction_triage", "message:one", "d")
        with pytest.raises(sqlite3.IntegrityError):
            _fact(db, "learning_loop.correction_outcome", "message:one", "e")
        _fact(db, "measurement.correction", "message:one", "f")  # other events are not constrained
        assert migrate_to_current(db, SCHEMA_VERSION).migrated is False
    finally:
        db.close()


def test_v148_skips_a_database_without_outcome_events() -> None:
    db = sqlite3.connect(":memory:")
    migrate_147_to_148(db)
    assert _indexes(db) == []
