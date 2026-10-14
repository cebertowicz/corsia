"""A restart hold stops every path that would start or extend a Turn with new work."""

from __future__ import annotations

from automation import self_restart


def _running_turn_with_hold(db) -> tuple[str, str]:
    """A running Turn on a thread whose restart operation has entered steering."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    thread_id, turn_id = "held-claims-thread", "held-claims-turn"
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        (thread_id, "user-1", "oMLX/qwen", "t0", "t0"),
    )
    db.commit()
    admission = admit_input(
        thread_id, role="user", content="running work", intent="apply_now",
        message_id="running-input", turn_id=turn_id, boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="running-write", started_at="t1", db=db)
    settle_batch_write("running-write", outcome="accepted", db=db)
    db.commit()
    operation_id = self_restart.try_acquire(None, None, None, None, "test update", db=db)
    assert operation_id is not None
    assert self_restart.transition_operation(
        operation_id, expected="holding", target="steering", db=db,
    )
    db.commit()
    assert self_restart.restart_holds_new_work(db=db) is True
    return thread_id, turn_id


def _queue_input_during_hold(db, thread_id: str) -> None:
    from infra.thread_driver import admit_input

    admission = admit_input(
        thread_id, role="user", content="sent during the update", intent="apply_now",
        message_id="held-input", turn_id="must-not-start", boot_epoch="boot-1",
        admitted_at="t2", db=db,
    )
    assert admission.held_for_restart is True
    assert admission.batch is None


def _queued_state(db) -> tuple:
    row = db.execute(
        "SELECT delivery_state,active_turn_id FROM messages WHERE id='held-input'"
    ).fetchone()
    return tuple(row)


def test_turn_end_during_hold_starts_no_successor_for_a_queued_input(isolated_db) -> None:
    """A Turn that ends mid-drain leaves the held input queued instead of starting its successor."""
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.thread_driver import finish_turn

    db = get_db()
    try:
        thread_id, turn_id = _running_turn_with_hold(db)
        _queue_input_during_hold(db, thread_id)
        finish_turn(
            turn_id, outcome="completed", ended_at="t3",
            successor_turn_id="must-not-start-successor", boot_epoch=BOOT_EPOCH, db=db,
        )
        assert _queued_state(db) == ("queued", None)
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE id='must-not-start-successor'"
        ).fetchone()[0] == 0
        # The held input runs right after the update: no idle fold is armed for this thread.
        assert db.execute(
            "SELECT idle_checkpoint_pending FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_safe_boundary_during_hold_claims_no_new_input_into_the_running_turn(isolated_db) -> None:
    """A running Turn reaching a safe boundary mid-drain does not absorb input sent after the cutoff."""
    from infra.database import get_db
    from infra.thread_inputs import claim_safe_boundary

    db = get_db()
    try:
        thread_id, turn_id = _running_turn_with_hold(db)
        _queue_input_during_hold(db, thread_id)
        assert claim_safe_boundary(thread_id, turn_id, db=db) is None
        assert _queued_state(db) == ("queued", None)
    finally:
        db.close()


def test_abort_wakes_an_idle_thread_whose_input_waited_out_the_hold(isolated_db) -> None:
    """Aborting the restart resumes a thread whose Turn ended mid-drain with input still queued."""
    from core.turn_core import BOOT_EPOCH
    from automation.restart import _release_restart_control
    from infra.database import get_db
    from infra.thread_driver import finish_turn

    db = get_db()
    try:
        thread_id, turn_id = _running_turn_with_hold(db)
        _queue_input_during_hold(db, thread_id)
        finish_turn(
            turn_id, outcome="completed", ended_at="t3",
            successor_turn_id="must-not-start-successor", boot_epoch=BOOT_EPOCH, db=db,
        )
        db.commit()
        operation_id = self_restart.in_flight_request()["id"]
    finally:
        db.close()

    woken = _release_restart_control(
        str(operation_id), "restart cancelled", True, None, None,
    )

    assert woken == (thread_id,)
