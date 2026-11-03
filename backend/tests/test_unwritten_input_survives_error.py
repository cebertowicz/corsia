"""An input no provider ever saw outlives the Turn it was claimed into.

A Turn that already wrote input, or that a restart ended, returns the inputs
it never wrote to the FIFO in its terminal transaction, so the successor
answers them. A Turn that ends before writing anything ends its own inputs
visibly, which is what bounds a pre-write failure that recurs.
"""

from __future__ import annotations

import sqlite3

from tests.test_clean_turn_terminal import _db, _running_turn, _session


def _accept(db: sqlite3.Connection, message_id: str, intent: str) -> None:
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        "thread-1", role="user", content=message_id, intent=intent,
        message_id=message_id, db=db,
    )
    db.commit()


def _claim_at_boundary(db: sqlite3.Connection, turn_id: str) -> None:
    """The live harness claims the prefix through the last Send, then never fences it."""
    from infra.thread_inputs import claim_safe_boundary

    assert claim_safe_boundary("thread-1", turn_id, db=db) is not None


def _input(db: sqlite3.Connection, message_id: str) -> tuple:
    return tuple(db.execute(
        "SELECT delivery_state,active_turn_id,write_started_at,write_outcome "
        "FROM messages WHERE id=?", (message_id,),
    ).fetchone())


def _turns(db: sqlite3.Connection) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT id,status FROM active_turns ORDER BY turn_order"
    )]


def test_restart_returns_a_mid_turn_claim_to_the_successor() -> None:
    """P&I clone 3: a digest and the wake after it, claimed unwritten into the
    Turn the restart killed, are answered by the successor, not ended failed."""
    from infra.thread_recovery import recover_threads

    db = _db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        _accept(db, "digest", "queue_after_turn")
        _accept(db, "wake", "apply_now")
        _claim_at_boundary(db, "turn-1")

        recovered = recover_threads(
            boot_epoch="boot-2", recovered_at="t5", successor_id=lambda _: "turn-2", db=db,
        )

        assert _turns(db) == [("turn-1", "error"), ("turn-2", "running")]
        assert recovered.settlements[0].failed_message_ids == ()
        assert _input(db, "digest") == ("queued", "turn-2", None, None)
        assert _input(db, "wake") == ("queued", "turn-2", None, None)
        assert db.execute("SELECT COUNT(*) FROM dead_letter_events").fetchone()[0] == 0
    finally:
        db.close()


def test_restart_before_the_first_write_returns_the_input() -> None:
    """A Turn the restart ended before it wrote anything still owes its input an answer."""
    from infra.thread_inputs import start_idle_turn
    from infra.thread_recovery import recover_threads

    db = _db()
    try:
        _session(db, "thread-1")
        _accept(db, "hello", "apply_now")
        assert start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1", started_at="t1", db=db,
        ) is not None

        recover_threads(
            boot_epoch="boot-2", recovered_at="t5", successor_id=lambda _: "turn-2", db=db,
        )

        assert _turns(db) == [("turn-1", "error"), ("turn-2", "running")]
        assert _input(db, "hello") == ("queued", "turn-2", None, None)
    finally:
        db.close()


def test_provider_error_returns_a_persons_unwritten_steer_to_the_successor() -> None:
    """Normal use: a person's Send claimed at a safe boundary, then the provider fails."""
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        _accept(db, "steer", "apply_now")
        _claim_at_boundary(db, "turn-1")

        terminal = finish_turn(
            "turn-1", outcome="error", ended_at="t3", successor_turn_id="turn-2",
            boot_epoch="boot-1", error=RuntimeError("provider stream reset"), db=db,
        )

        assert terminal.failed_message_ids == ()
        assert terminal.successor is not None and terminal.successor.turn_id == "turn-2"
        assert _input(db, "steer") == ("queued", "turn-2", None, None)
    finally:
        db.close()


def test_a_successor_that_fails_before_writing_ends_the_input_once() -> None:
    """No chaining: the successor that never wrote ends the input visibly, and
    the thread goes idle rather than claiming it into a third Turn."""
    from infra.thread_driver import finish_turn

    db = _db()
    try:
        _session(db, "thread-1")
        db.commit()
        _running_turn(db, "thread-1", "turn-1")
        _accept(db, "steer", "apply_now")
        _claim_at_boundary(db, "turn-1")
        finish_turn(
            "turn-1", outcome="error", ended_at="t3", successor_turn_id="turn-2",
            boot_epoch="boot-1", error=RuntimeError("provider stream reset"), db=db,
        )

        terminal = finish_turn(
            "turn-2", outcome="error", ended_at="t4", successor_turn_id="turn-3",
            boot_epoch="boot-1", error=RuntimeError("provider stream reset"), db=db,
        )

        assert terminal.failed_message_ids == ("steer",)
        assert terminal.successor is None
        assert _turns(db) == [("turn-1", "error"), ("turn-2", "error")]
        assert _input(db, "steer") == ("failed", "turn-2", None, "proved_no_write")
    finally:
        db.close()
