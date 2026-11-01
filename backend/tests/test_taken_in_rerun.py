"""A Turn that fails after taking in a person's input it never answered runs exactly one successor.

The successor's only new input is the model-only re-run row: the person's
message is never re-written. A re-run never chains, and Stop, usage-limit,
account, checkpoint and can't-fit terminals start none.
"""

from __future__ import annotations

import json

import pytest

from core.checkpoint_types import CheckpointContinuationError
from infra.taken_in_rerun import rerun_message_id
from infra.thread_input_types import ClaimedBatch
from llm.error_cause import ProviderTurnFailure

_THREAD = "rerun-thread"


def _thread(db) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
        (_THREAD, "user-1", "oMLX/qwen", "t0", "t0"),
    )
    db.commit()


def _take_in(batch, attempt: str, db) -> None:
    """The provider accepted the batch: its inputs are delivered."""
    from infra.input_writes import begin_batch_write, settle_batch_write

    begin_batch_write(batch, attempt_id=attempt, started_at="t1", db=db)
    settle_batch_write(attempt, outcome="accepted", db=db)
    db.commit()


def _running_turn(db, turn="t1", message="m1"):
    from infra.thread_driver import admit_input

    admission = admit_input(
        _THREAD, role="user", content="please answer this", intent="apply_now",
        message_id=message, turn_id=turn, boot_epoch="boot-1", admitted_at="t1", db=db,
    )
    assert isinstance(admission.batch, ClaimedBatch)
    _take_in(admission.batch, f"write-{turn}", db)
    return admission.batch


def _finish(db, turn="t1", *, outcome="error", error="provider exploded"):
    from infra.thread_driver import finish_turn

    return finish_turn(
        turn, outcome=outcome, ended_at="t2", successor_turn_id=f"next-{turn}",
        boot_epoch="boot-1", error=error, db=db,
    )


def _reruns(db) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT id,role,content,metadata,active_turn_id FROM messages "
        "WHERE thread_id=? AND id LIKE 'turn-rerun:%' ORDER BY id", (_THREAD,))]


def _person_rows(db) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        "SELECT id,content,delivery_state FROM messages WHERE thread_id=? AND role='user' ORDER BY seq",
        (_THREAD,))]


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    _thread(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_one_successor_runs_with_only_the_rerun_row(db) -> None:
    _running_turn(db)

    terminal = _finish(db)

    assert isinstance(terminal.successor, ClaimedBatch)
    assert [item.message_id for item in terminal.successor.items] == [rerun_message_id("t1")]
    (row,) = _reruns(db)
    assert row["role"] == "system"
    assert json.loads(row["metadata"])["type"] == "turn_rerun"
    assert json.loads(row["metadata"])["failed_turn_id"] == "t1"
    # The person's message is never re-written or re-queued.
    assert _person_rows(db) == [("m1", "please answer this", "delivered")]


def test_a_typed_provider_failure_reruns(db) -> None:
    _running_turn(db)

    terminal = _finish(db, error=ProviderTurnFailure("HTTP 500", "server_error"))

    assert [item.message_id for item in terminal.successor.items] == [rerun_message_id("t1")]


def test_a_failed_rerun_never_chains(db) -> None:
    """The successor took in a boundary person input too, then failed: no third run."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import claim_safe_boundary

    _running_turn(db)
    successor = _finish(db).successor
    _take_in(successor, "write-next", db)
    accept_thread_input(_THREAD, role="user", content="also this", intent="apply_now",
                        message_id="m2", db=db)
    db.commit()
    boundary = claim_safe_boundary(_THREAD, successor.turn_id, db=db)
    assert boundary is not None and [item.message_id for item in boundary.items] == ["m2"]
    _take_in(boundary, "write-boundary", db)

    terminal = _finish(db, successor.turn_id)

    assert terminal.successor is None
    assert len(_reruns(db)) == 1
    assert ("m2", "also this", "delivered") in _person_rows(db)


@pytest.mark.parametrize("error", [
    ProviderTurnFailure("limit", "usage_limit"),
    ProviderTurnFailure("too long", "context_overflow"),
    ProviderTurnFailure("signed out", "sign_in_expired"),
    ProviderTurnFailure("bad key", "auth_rejected"),
    CheckpointContinuationError("checkpoint failed"),
])
def test_excluded_failures_start_no_successor(db, error) -> None:
    _running_turn(db)

    terminal = _finish(db, error=error)

    assert terminal.successor is None
    assert _reruns(db) == []


def test_stop_starts_no_successor(db) -> None:
    _running_turn(db)

    terminal = _finish(db, outcome="cancelled", error=None)

    assert terminal.successor is None
    assert _reruns(db) == []


def test_an_answered_input_starts_no_successor(db) -> None:
    from infra.queries import insert_message

    _running_turn(db)
    insert_message(_THREAD, "assistant", "here is your answer", metadata={"turn_id": "t1"}, db=db)
    db.commit()

    assert _finish(db).successor is None
    assert _reruns(db) == []


def test_an_input_never_taken_in_starts_no_rerun(db) -> None:
    from infra.thread_driver import admit_input

    admit_input(_THREAD, role="user", content="unsent", intent="apply_now", message_id="m1",
                turn_id="t1", boot_epoch="boot-1", admitted_at="t1", db=db)
    db.commit()

    _finish(db)

    assert _reruns(db) == []


def test_queued_person_input_answers_without_a_rerun(db) -> None:
    from infra.input_admission import accept_thread_input

    _running_turn(db)
    accept_thread_input(_THREAD, role="user", content="next", intent="apply_now",
                        message_id="m-next", db=db)
    db.commit()

    terminal = _finish(db)

    assert [item.message_id for item in terminal.successor.items] == ["m-next"]
    assert _reruns(db) == []


def test_a_requeued_unwritten_input_answers_without_a_rerun(db) -> None:
    """A boundary input the failed Turn claimed but never wrote goes back to the FIFO and answers alone."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import claim_safe_boundary

    _running_turn(db)
    accept_thread_input(_THREAD, role="user", content="also this", intent="apply_now",
                        message_id="m2", db=db)
    db.commit()
    boundary = claim_safe_boundary(_THREAD, "t1", db=db)
    assert boundary is not None and [item.message_id for item in boundary.items] == ["m2"]

    terminal = _finish(db)

    assert [item.message_id for item in terminal.successor.items] == ["m2"]
    assert _reruns(db) == []


def _recover(db):
    from automation.restart_wakes import materialize_pending_registrations
    from infra.thread_recovery import recover_threads

    materialize_pending_registrations(db=db)
    return recover_threads(
        boot_epoch="boot-2", recovered_at="t3", successor_id=lambda _sid: "after-restart", db=db,
    )


def test_a_restart_ended_turn_reruns_through_its_one_settlement(db) -> None:
    _running_turn(db)

    recovery = _recover(db)

    (settlement,) = recovery.settlements
    assert [item.message_id for item in settlement.successor.items] == [rerun_message_id("t1")]
    assert recovery.batches == ()


def test_a_registered_restart_wake_answers_without_a_rerun(db) -> None:
    """Restart recovery and the re-run never both start a successor for one Turn."""
    from automation.restart_wakes import register_hard_recovery

    _running_turn(db)
    register_hard_recovery(_THREAD, "t1", db=db)

    recovery = _recover(db)

    (settlement,) = recovery.settlements
    (item,) = settlement.successor.items
    assert json.loads(db.execute(
        "SELECT metadata FROM messages WHERE id=?", (item.message_id,),
    ).fetchone()[0])["kind"] == "restart_recovery"
    assert recovery.batches == ()
    assert _reruns(db) == []
