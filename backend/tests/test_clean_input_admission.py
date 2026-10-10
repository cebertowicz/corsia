"""Composed input admission contracts for the clean thread driver."""

from __future__ import annotations

import sqlite3


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    return db


def test_admission_atomically_creates_only_the_idle_winner() -> None:
    """Concurrent callers can accept inputs, but only one claims the Turn."""
    from infra.thread_driver import admit_input

    db = _db()
    try:
        first = admit_input(
            "thread-1", role="user", content="first", intent="apply_now",
            message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
            admitted_at="t1", db=db,
        )
        second = admit_input(
            "thread-1", role="user", content="second", intent="apply_now",
            message_id="input-2", turn_id="turn-2", boot_epoch="boot-1",
            admitted_at="t2", db=db,
        )

        assert first.batch is not None
        assert first.batch.turn_id == "turn-1"
        assert second.batch is None
        assert (first.accepted.input_order, second.accepted.input_order) == (2, 3)
        rows = db.execute(
            "SELECT id,role,content,active_turn_id FROM messages ORDER BY input_order"
        ).fetchall()
        assert rows[0]["role"] == "system"
        assert "[PASSIVE CONTEXT DIGEST]" in rows[0]["content"]
        assert [(row["id"], row["active_turn_id"]) for row in rows[1:]] == [
            ("input-1", "turn-1"), ("input-2", None),
        ]
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE status='running'"
        ).fetchone()[0] == 1
    finally:
        db.close()
