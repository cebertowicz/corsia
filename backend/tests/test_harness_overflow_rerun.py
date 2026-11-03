"""A harness Turn its provider refused for size runs once more instead of ending on the error.

Claude Code and Codex drop the refused provider session, so the successor is
prepared cold from CORSIA's history and its boundary folds what no longer fits.
One re-run per refused Turn: a refused re-run ends visibly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.stop_causes import CONTEXT_OVERFLOW_REASON, ContextOverflowEnd
from infra.database import get_db
from infra.input_admission import accept_thread_input
from infra import thread_execution  # noqa: F401  # imported before the driver, so it binds the real one
from infra.plan_limit_continue import overflow_continue_message_id, overflow_rerun_queued
from infra.thread_driver import finish_turn
from infra.thread_input_types import ClaimedBatch
from infra.thread_inputs import start_idle_turn

_REFUSAL = "Prompt is too long"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn(db, thread: str = "thread", turn: str = "t1", model: str = "native/test") -> None:
    # The terminal seam is the same for every harness; a plain model needs no account fixture.
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,'user',?,'t','t')",
               (thread, model))
    db.commit()
    _accept(db, thread, f"m1-{thread}")
    start_idle_turn(thread, turn_id=turn, boot_epoch="boot", started_at=_now(), db=db)
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id=?", (f"m1-{thread}",))
    db.commit()


def _accept(db, thread: str, message: str) -> None:
    accept_thread_input(thread, role="user", content="work", intent="apply_now",
                        message_id=message, created_at=_now(), db=db)


def _refuse(db, turn: str = "t1", error: object = None):
    return finish_turn(turn, outcome="error", ended_at=_now(), successor_turn_id=f"next-{turn}",
                       boot_epoch="boot", error=ContextOverflowEnd(_REFUSAL) if error is None else error, db=db)


def _reruns(db, thread: str = "thread") -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT id FROM messages WHERE thread_id=? AND id LIKE 'overflow-continue:%' ORDER BY id", (thread,))]


def test_a_size_refusal_starts_its_one_rerun_at_once(isolated_db) -> None:
    db = get_db()
    _turn(db)

    terminal = _refuse(db)

    assert isinstance(terminal.successor, ClaimedBatch)
    assert [item.message_id for item in terminal.successor.items] == [overflow_continue_message_id("t1")]
    event_type, cause, sender, stopped = db.execute(
        "SELECT json_extract(metadata,'$.event_type'),json_extract(metadata,'$.cause'),"
        "json_extract(metadata,'$.sender_kind'),json_extract(metadata,'$.stopped_turn_id') "
        "FROM messages WHERE id=?", (overflow_continue_message_id("t1"),)).fetchone()
    assert (event_type, cause, sender, stopped) == (
        CONTEXT_OVERFLOW_REASON, CONTEXT_OVERFLOW_REASON, "system", "t1")
    assert overflow_rerun_queued("t1", db=db)
    assert db.execute("SELECT error FROM active_turns WHERE id='t1'").fetchone()[0] == _REFUSAL


def test_a_refused_rerun_ends_visibly_and_starts_nothing(isolated_db) -> None:
    db = get_db()
    _turn(db)
    rerun = _refuse(db).successor
    assert isinstance(rerun, ClaimedBatch)

    terminal = _refuse(db, rerun.turn_id)

    assert terminal.successor is None or not isinstance(terminal.successor, ClaimedBatch)
    assert _reruns(db) == [overflow_continue_message_id("t1")]
    assert not overflow_rerun_queued(rerun.turn_id, db=db)


def test_real_queued_input_gets_no_rerun(isolated_db) -> None:
    db = get_db()
    _turn(db)
    _accept(db, "thread", "real")
    db.commit()

    terminal = _refuse(db)

    assert isinstance(terminal.successor, ClaimedBatch)
    assert [item.message_id for item in terminal.successor.items] == ["real"]
    assert _reruns(db) == []
    assert not overflow_rerun_queued("t1", db=db)


def test_any_other_error_still_ends_with_no_rerun(isolated_db) -> None:
    db = get_db()
    _turn(db)

    terminal = _refuse(db, error=_REFUSAL)

    assert terminal.successor is None or not isinstance(terminal.successor, ClaimedBatch)
    assert _reruns(db) == []
