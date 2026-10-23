"""Schema 115 -> 116 removes the retired provider-checker correction settings."""

from __future__ import annotations

import json
import sqlite3

from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_115_to_116_corrections_checker_cleanup import migrate_115_to_116
from infra.schema import SCHEMA_VERSION


def _settings(db: sqlite3.Connection) -> dict[str, object]:
    return {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM settings")}


def _historical_c1_prerequisites(db: sqlite3.Connection) -> None:
    """Add tables a real v115 database acquired at v47 and v77."""
    from infra.migration_46_to_47_guidance_manifests import _GUIDANCE_47_DDL
    from infra.migration_76_to_77_outcome_events import _OUTCOME_EVENTS_77_DDL

    for statement in (*_GUIDANCE_47_DDL, *_OUTCOME_EVENTS_77_DDL):
        db.execute(statement)


def _seed(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    rows = {
        "agents": {"defaults": {"corrections_checker_model": "model-a", "host_access_auto_approve": True}},
        "corrections_recording_manual_request": {"pending": True},
        "corrections_recording_status": {"stop_reason": "empty"},
        "corrections_classifier_active_version": {"key": "kept"},
    }
    for key, value in rows.items():
        db.execute("INSERT INTO settings VALUES (?,?,'t')", (key, json.dumps(value)))


def test_registered_as_the_step_from_115():
    assert SCHEMA_VERSION >= 116
    assert MIGRATIONS[115] is migrate_115_to_116


def test_removes_only_the_retired_settings_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    _seed(db)

    migrate_115_to_116(db)
    migrate_115_to_116(db)

    assert _settings(db) == {
        "agents": {"defaults": {"host_access_auto_approve": True}},
        "corrections_classifier_active_version": {"key": "kept"},
    }


def test_database_without_settings_or_agents_is_left_alone():
    db = sqlite3.connect(":memory:")
    migrate_115_to_116(db)
    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.execute("INSERT INTO settings VALUES ('agents','not json','t')")

    migrate_115_to_116(db)

    assert db.execute("SELECT value FROM settings WHERE key='agents'").fetchone()[0] == "not json"


def test_runs_when_migrating_an_existing_115_database(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        _seed(connection)
        _historical_c1_prerequisites(connection)
        connection.execute("PRAGMA user_version=115")
    with sqlite3.connect(path) as connection:
        migrate_to_current(connection, SCHEMA_VERSION)
        assert "corrections_recording_status" not in _settings(connection)
        assert _settings(connection)["agents"] == {"defaults": {"host_access_auto_approve": True}}
