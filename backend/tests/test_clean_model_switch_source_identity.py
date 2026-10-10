"""Private source-identity contract for clean model-switch preparation."""

from __future__ import annotations

import sqlite3

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/source','t0','t0')"
    )
    return db


def test_owned_model_switch_thread_read_preserves_ownership_and_active_turns() -> None:
    from thread import model_source
    from thread.model_state import ModelSwitchPreparationError, ModelSwitchSequencingRequired

    read_owned = getattr(model_source, "_read_owned_model_switch_thread")
    db = _db()
    try:
        row, active_turn_id = read_owned(
            db, "thread-1", user_id="user-1", allow_active_turn=False,
        )
        assert (row["user_id"], row["model"], active_turn_id) == (
            "user-1", "native/source", None,
        )

        with pytest.raises(ModelSwitchPreparationError, match="does not exist"):
            read_owned(db, "missing", user_id="user-1", allow_active_turn=False)
        with pytest.raises(ModelSwitchPreparationError, match="is not owned"):
            read_owned(db, "thread-1", user_id="user-2", allow_active_turn=False)

        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('turn-1','thread-1',0,'native/source','running','t1','boot-1')"
        )
        with pytest.raises(ModelSwitchSequencingRequired, match="blocked by active Turn"):
            read_owned(db, "thread-1", user_id="user-1", allow_active_turn=False)
        _, active_turn_id = read_owned(
            db, "thread-1", user_id="user-1", allow_active_turn=True,
        )
        assert active_turn_id == "turn-1"
    finally:
        db.close()
