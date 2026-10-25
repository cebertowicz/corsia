"""Schema v17 gives a queued input the continuation condition it was collected for.

A plan wake is appended while its source still describes work that can move, and
the condition proving that was checked once and thrown away. v17 stores it on
the input itself, so the Turn that would claim the wake can judge it again. The
column sits outside the payload FIFO admission compares for duplicate identity,
so a redelivery of the same input is still the same input.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

_QUEUED = (
    "INSERT INTO messages "
    "(id,thread_id,role,content,created_at,delivery_state,input_order,"
    " delivery_intent,target_execution_epoch,target_model) "
    "VALUES (?,'owner','system','wake','t','queued',?,'apply_now',0,'m')"
)


def _schema_sixteen_database() -> sqlite3.Connection:
    """A v16 database: queued inputs carry no collected condition."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('owner','user-1','t','t')"
    )
    db.execute("ALTER TABLE messages DROP COLUMN continuation_condition")
    db.execute(_QUEUED, ("input-1", 1))
    db.execute("PRAGMA user_version=16")
    db.commit()
    return db


def _migrated() -> sqlite3.Connection:
    from infra.database_migrations import migrate_to_current

    db = _schema_sixteen_database()
    result = migrate_to_current(db, 17)
    assert (result.migrated, result.from_version, result.to_version) == (True, 16, 17)
    return db


def test_fresh_schema_is_current_version() -> None:
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 146


def test_an_already_queued_input_keeps_its_row_and_gains_no_condition() -> None:
    db = _migrated()
    try:
        row = db.execute(
            "SELECT delivery_state,continuation_condition FROM messages WHERE id='input-1'",
        ).fetchone()

        assert (row["delivery_state"], row["continuation_condition"]) == ("queued", None)
    finally:
        db.close()


def test_the_column_holds_a_condition_object_and_refuses_anything_else() -> None:
    db = _migrated()
    try:
        db.execute(_QUEUED, ("input-2", 2))
        db.execute(
            "UPDATE messages SET continuation_condition=? WHERE id='input-2'",
            (json.dumps({"kind": "t0_liveness", "subject_id": "wake-1"}),),
        )

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE messages SET continuation_condition='[]' WHERE id='input-2'",
            )
    finally:
        db.close()
