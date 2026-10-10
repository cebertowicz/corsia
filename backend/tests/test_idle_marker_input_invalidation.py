"""What clearing an idle marker records, and the inputs that start no Turn.

Marker survival across the ordinary waking, passive and duplicate inputs is
owned by ``test_idle_checkpoint_due_state.py``. This file owns the two things
that file does not reach: an input the restart hold parks, and the reason a
cleared marker is counted under.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

CC_MODEL = "claude-code/test"


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) "
        "VALUES (?,'claude-code','anthropic',?,?,?,200000,'t0','t0')",
        (CC_MODEL, CC_MODEL, CC_MODEL, CC_MODEL),
    )
    return db


def _idle_thread_with_marker(db: sqlite3.Connection) -> str:
    """One root thread whose last Turn completed and left a warm marker."""
    thread_id = "t-root"
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at,"
        "lifecycle_state,next_input_order) VALUES (?,?,?,'t0','t0','active',1)",
        (thread_id, "user", CC_MODEL),
    )
    ended = datetime.now(UTC).replace(tzinfo=None).isoformat()
    db.execute(
        "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
        "started_at,ended_at,boot_epoch,idle_checkpoint_pending) "
        "VALUES ('turn-1',?,0,?,'completed','t0',?,'boot',1)",
        (thread_id, CC_MODEL, ended),
    )
    db.commit()
    return thread_id


def _marker_pending(db: sqlite3.Connection, thread_id: str) -> int:
    return db.execute(
        "SELECT count(*) FROM active_turns "
        "WHERE thread_id=? AND idle_checkpoint_pending=1",
        (thread_id,),
    ).fetchone()[0]


def _admit(db: sqlite3.Connection, thread_id: str, message_id: str):
    from infra.thread_driver import admit_input

    return admit_input(
        thread_id,
        role="user",
        content="an event delivered to this thread",
        intent="apply_now",
        message_id=message_id,
        turn_id=f"turn-for-{message_id}",
        boot_epoch="boot",
        admitted_at=datetime.now(UTC).replace(tzinfo=None).isoformat(),
        db=db,
    )


def test_input_held_for_restart_keeps_the_marker(monkeypatch):
    """While a restart hold is in force no Turn starts, so the fold is kept."""
    import automation.self_restart as self_restart

    db = _db()
    thread_id = _idle_thread_with_marker(db)
    monkeypatch.setattr(self_restart, "restart_holds_new_work", lambda db=None: True)
    assert _marker_pending(db, thread_id) == 1

    admission = _admit(db, thread_id, "m-held")

    assert admission.held_for_restart is True
    assert admission.batch is None, "no Turn was started while held"
    assert _marker_pending(db, thread_id) == 1


def test_a_turn_taking_over_records_why_the_marker_was_cleared(monkeypatch):
    """No marker is cleared silently: superseding work is counted."""
    import infra.product_events as product_events

    emitted: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        product_events,
        "emit_product_event",
        lambda category, action, **kw: emitted.append(
            (category, action, kw.get("properties") or {})
        ),
    )

    db = _db()
    thread_id = _idle_thread_with_marker(db)
    admission = _admit(db, thread_id, "m-real")

    assert admission.batch is not None
    assert _marker_pending(db, thread_id) == 0
    assert (
        "idle_checkpoint",
        "invalidated",
        {"reason": "superseded_by_turn"},
    ) in emitted
