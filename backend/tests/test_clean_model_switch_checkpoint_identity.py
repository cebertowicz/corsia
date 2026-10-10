"""Private thread-row identity contract for model-switch checkpoints."""

from __future__ import annotations

import sqlite3

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,pending_model,switch_operation_id,project_id,"
        "reasoning_effort,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/source','native/target','switch-1',"
        "'project-1','high','t0','t0')"
    )
    return db


def test_model_switch_checkpoint_thread_row_keeps_pending_identity() -> None:
    from infra.model_switch_types import SwitchRequest
    from thread import model_checkpoint

    request = SwitchRequest(
        "switch-1", "thread-1", "native/source", "native/target",
        0, 1, 0, "user", "user-1", "model_switch", "current",
        "2026-09-04T00:00:00+00:00", 0,
    )
    read_thread_row = getattr(model_checkpoint, "_switch_thread_row")
    db = _db()
    try:
        row = read_thread_row(db, request)
        assert (row["project_id"], row["reasoning_effort"]) == ("project-1", "high")

        missing = SwitchRequest(
            "switch-1", "missing", "native/source", "native/target",
            0, 1, 0, "user", "user-1", "model_switch", "current",
            "2026-09-04T00:00:00+00:00", 0,
        )
        with pytest.raises(RuntimeError, match="pending model switch changed"):
            read_thread_row(db, missing)

        db.execute("UPDATE threads SET model='native/other'")
        with pytest.raises(RuntimeError, match="pending model switch changed"):
            read_thread_row(db, request)

        db.execute("UPDATE threads SET model='native/source',pending_model='native/other'")
        with pytest.raises(RuntimeError, match="pending model switch changed"):
            read_thread_row(db, request)

        db.execute(
            "UPDATE threads SET pending_model='native/target',switch_operation_id='switch-2'"
        )
        with pytest.raises(RuntimeError, match="pending model switch changed"):
            read_thread_row(db, request)
    finally:
        db.close()
