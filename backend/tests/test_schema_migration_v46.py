"""Schema v46: every selected placement mark carries the fork's answer."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.migration_45_to_46_placement_answers import migrate_45_to_46
from infra.schema import SCHEMA_VERSION

_ANSWER = json.dumps({"checkpoint": "checkpoint:cp-1",
                      "answer": {"placement": {"q1": "agent"}, "room": None}})


def _entry(db, entry_id: int, state: str | None, detail: str | None) -> None:
    db.execute(
        "INSERT INTO continuity_entries (thread_id,history_epoch,entry_id,field,body,"
        "revision,created_at,updated_at,placement_state,placement_revision,"
        "placement_detail) VALUES ('t',0,?,'decisions','{}',1,'x','x',?,?,?)",
        (entry_id, state, 1 if state else None, detail),
    )


def _staged(db, order: int, body: dict) -> None:
    db.execute(
        "INSERT INTO checkpoint_staged_edits (checkpoint_id,edit_order,operation,"
        "entry_id,field,body,base_entry_revision) VALUES ('cp-1',?,'review',?,"
        "'decisions',?,1)",
        (order, order, json.dumps(body)),
    )


def _mark(db, entry_id: int) -> tuple:
    return db.execute(
        "SELECT placement_state,placement_revision,placement_detail "
        "FROM continuity_entries WHERE entry_id=?", (entry_id,),
    ).fetchone()


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 120


def test_answerless_selections_become_unmarked_and_answered_ones_stay():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _entry(db, 1, "selected", "checkpoint:cp-0")
        _entry(db, 2, "selected", json.dumps({"checkpoint": "checkpoint:cp-0",
                                              "answer": None}))
        _entry(db, 3, "selected", _ANSWER)
        _entry(db, 4, "reaffirmed", "checkpoint:cp-0")

        migrate_45_to_46(db)

        assert _mark(db, 1) == (None, None, None)
        assert _mark(db, 2) == (None, None, None)
        assert _mark(db, 3) == ("selected", 1, _ANSWER)
        assert _mark(db, 4) == ("reaffirmed", 1, "checkpoint:cp-0")


def test_answerless_staged_selections_resolve_as_conflicts():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _staged(db, 1, {"verdict": "select", "destination": None})
        _staged(db, 2, {"verdict": "select", "destination": None,
                        "answer": {"placement": {"q1": "agent"}, "room": None}})
        _staged(db, 3, {"verdict": "reaffirm", "destination": None, "answer": None})

        migrate_45_to_46(db)

        rows = db.execute(
            "SELECT edit_order,outcome,outcome_detail FROM checkpoint_staged_edits "
            "ORDER BY edit_order"
        ).fetchall()
        assert rows == [
            (1, "conflict", "select verdict without a placement answer"),
            (2, "pending", None),
            (3, "pending", None),
        ]


def test_retired_worker_model_setting_is_dropped_and_others_kept():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES ('agents',?,'x')",
            (json.dumps({"defaults": {"model": "m", "placement_worker_model": "w"},
                         "other": 1}),),
        )

        migrate_45_to_46(db)

        value = db.execute("SELECT value FROM settings WHERE key='agents'").fetchone()[0]
        assert json.loads(value) == {"defaults": {"model": "m"}, "other": 1}


def test_startup_migration_runs_from_45(tmp_path):
    path = tmp_path / "placement-answers.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO threads (id, user_id, created_at, updated_at) VALUES ('t', 'u', 'x', 'x')"
        )
        _entry(db, 1, "selected", "checkpoint:cp-0")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=45")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 45, SCHEMA_VERSION)
        assert _mark(db, 1) == (None, None, None)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
