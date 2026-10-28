"""Schema 120 -> 121: action drafts are additive and idempotent."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_120_to_121_action_drafts import migrate_120_to_121
from infra.schema import SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(action_drafts)")]


def test_fresh_schema_has_the_table_and_the_step_is_registered() -> None:
    assert SCHEMA_VERSION == 161
    assert MIGRATIONS[120] is migrate_120_to_121
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert _columns(db) == [
            "id", "thread_id", "kind", "path", "target_json", "state",
            "recorded_revision", "recorded_at", "created_at", "sent_at", "external_id",
        ]


def test_startup_migration_creates_the_table_and_keeps_threads(tmp_path) -> None:
    path = tmp_path / "v118.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE action_drafts")
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('kept','user','Kept','t','t')"
        )
        db.execute("PRAGMA user_version=118")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 118, 161)
        assert "recorded_revision" in _columns(db)
        assert db.execute("SELECT id FROM threads").fetchall() == [("kept",)]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_kind_and_state_are_constrained() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO threads VALUES ('t')")
        migrate_120_to_121(db)
        db.execute(
            "INSERT INTO action_drafts (id,thread_id,kind,path,state,"
            "recorded_revision,recorded_at,created_at) VALUES ('ok','t','email','p','sending','r','t','t')"
        )
        for kind, state in (("sms", "open"), ("email", "queued")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO action_drafts (id,thread_id,kind,path,state,"
                    "recorded_revision,recorded_at,created_at) VALUES ('d','t',?,'p',?,'r','t','t')",
                    (kind, state),
                )
