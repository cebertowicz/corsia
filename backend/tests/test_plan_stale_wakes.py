"""A plan wake whose work moved on never starts a Turn.

A wake is appended while its source still describes work that can move. It then
waits behind whatever Turn is running, and by the time a Turn would claim it the
work can be finished, released, or waiting on something. Starting a Turn for it
then costs a model call and produces nothing.

The condition the wake was collected for travels with the queued input, and both
claim paths judge it again inside their own write: the idle start, and the safe
boundary of a running Turn. A wake that no longer holds is cancelled in place,
so it stays visible as what it was rather than disappearing.

What the claim asks is narrower than what the append asked. Delivering a wake
settles its source — the T0 row re-arms at its next cycle — so a claim that
compared that bookkeeping again would cancel a wake for having been delivered,
and the Turn it waited for would never start. The claim asks only whether the
work the wake described can still move.
"""

from __future__ import annotations

import sqlite3
import uuid
import pytest
from datetime import datetime, timedelta, timezone

from automation import plan_liveness
from automation.continuation.execution import event_for
from automation.continuation.sources.liveness import LivenessSource
from automation.continuation.validity import condition_for_event
from infra.schema import SCHEMA_SQL
from infra.thread_inputs import claim_safe_boundary, start_idle_turn
from plans import admission, service

TURN = "turn-running"


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    db.commit()
    return db


def _armed_wake_condition(db) -> dict:
    """Arm this thread's T0 wake and return the condition its delivery carries."""
    plan_liveness.on_turn_complete("owner", db=db)
    db.commit()
    due = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    request = LivenessSource().due(due, db=db)[0]
    _event, _summary, details = event_for(request)
    condition = condition_for_event("plan.liveness_wake", details)
    assert condition is not None
    return condition


def _running_turn(db) -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,'owner',0,'test/model','running','t','boot')""",
        (TURN,),
    )
    db.commit()


def _queue_wake(db, condition: dict) -> str:
    """Queue the wake behind the running Turn, as its delivery does."""
    from infra.thread_driver import admit_input

    message_id = uuid.uuid4().hex
    admit_input(
        "owner", role="system", content="Actionable structured plan work remains.",
        intent="apply_now", message_id=message_id, turn_id=uuid.uuid4().hex,
        boot_epoch="boot", admitted_at="t", continuation=condition, db=db,
    )
    db.commit()
    return message_id


def _row(db, message_id: str) -> sqlite3.Row:
    return db.execute(
        "SELECT delivery_state,delivery_error,active_turn_id FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()


def _work(db) -> tuple:
    plan = service.ensure_plan("thread", "owner", db=db)
    task = service.add_node(
        plan.id, kind="task", title="Draft the brief",
        acceptance_criteria="The brief is written", db=db,
    )
    service.transition_node(task.id, "active", db=db)
    admission.select(task.id, actor_id="owner", provenance="test", db=db, touch=False)
    db.commit()
    return plan, task


def _finish(db, task) -> None:
    """Settle the only actionable work, which is what makes the wake stale."""
    from tests.plan_review_helpers import submit_review_evidence

    submit_review_evidence(task.id, db=db)
    service.transition_node(
        task.id, "accepted", verification_result="passed", verified_by="user", db=db,
    )
    admission.release(task.id, "accepted", db=db)
    db.commit()


def test_a_wake_whose_work_settled_is_cancelled_instead_of_starting_a_turn():
    db = _db()
    try:
        _plan, task = _work(db)
        condition = _armed_wake_condition(db)
        _running_turn(db)
        message_id = _queue_wake(db, condition)
        _finish(db, task)
        db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
        db.commit()

        started = start_idle_turn(
            "owner", turn_id="turn-next", boot_epoch="boot", started_at="t", db=db,
        )

        assert started is None
        row = _row(db, message_id)
        assert (row["delivery_state"], row["delivery_error"]) == (
            "cancelled", "stale_plan_wake",
        )
    finally:
        db.close()


def test_a_wake_whose_work_still_moves_starts_its_turn():
    db = _db()
    try:
        _work(db)
        condition = _armed_wake_condition(db)
        _running_turn(db)
        message_id = _queue_wake(db, condition)
        db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
        db.commit()

        started = start_idle_turn(
            "owner", turn_id="turn-next", boot_epoch="boot", started_at="t", db=db,
        )

        assert started is not None
        row = _row(db, message_id)
        assert (row["delivery_state"], row["active_turn_id"]) == ("queued", "turn-next")
    finally:
        db.close()


def _settle_delivered(db) -> None:
    """Settle the wake's trigger exactly as its delivery does.

    This is what re-arms the wake row at its next cycle — bookkeeping about the
    wake, not about the work — and it happens while the input sits queued.
    """
    from automation.continuation.models import Trigger
    from automation.continuation.sources.liveness import LivenessSource, _transition

    row = db.execute(
        "SELECT * FROM plan_liveness_wakes WHERE thread_id='owner'",
    ).fetchone()
    wake = plan_liveness.row_to_wake(dict(row))
    now = datetime.now(timezone.utc).isoformat()
    LivenessSource().settle(
        Trigger(
            id="t0-trigger", kind="t0_liveness", thread_id="owner",
            scope_id="project", subject_id=wake.id,
            dedupe_key=f"t0_liveness:owner:{wake.id}:{_transition(wake)}",
            idempotency_key=None, state="dispatched", reason=None, attempt=1,
            deferrals=0, cost_estimate_usd=0.0, next_attempt_at=None,
            created_at=now, updated_at=now,
        ),
        "delivered", "progress_UNCHANGED", db=db,
    )
    db.commit()


def test_a_delivered_wake_still_starts_its_turn_when_the_work_is_unchanged():
    db = _db()
    try:
        _work(db)
        condition = _armed_wake_condition(db)
        _running_turn(db)
        message_id = _queue_wake(db, condition)
        _settle_delivered(db)
        db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
        db.commit()

        started = start_idle_turn(
            "owner", turn_id="turn-next", boot_epoch="boot", started_at="t", db=db,
        )

        assert started is not None
        row = _row(db, message_id)
        assert (row["delivery_state"], row["active_turn_id"]) == ("queued", "turn-next")
    finally:
        db.close()


def test_a_stale_wake_never_reaches_a_running_turns_safe_boundary():
    db = _db()
    try:
        _plan, task = _work(db)
        condition = _armed_wake_condition(db)
        _running_turn(db)
        message_id = _queue_wake(db, condition)
        _finish(db, task)

        claimed = claim_safe_boundary("owner", TURN, db=db)

        assert claimed is None
        assert _row(db, message_id)["delivery_error"] == "stale_plan_wake"
    finally:
        db.close()


@pytest.mark.parametrize("at_safe_boundary", (False, True))
def test_checkpoint_held_stale_wake_does_not_block_later_user_input(at_safe_boundary):
    """A held wake's plan can end; the existing owner must still cancel it."""
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        _plan, task = _work(db)
        condition = _armed_wake_condition(db)
        _running_turn(db)
        wake_id = _queue_wake(db, condition)
        db.execute(
            "UPDATE messages SET write_outcome='proved_no_write',"
            "delivery_error='checkpoint unavailable' WHERE id=?", (wake_id,),
        )
        accept_thread_input(
            "owner", role="user", content="new independent input", intent="apply_now",
            message_id="next-user", db=db,
        )
        _finish(db, task)
        if at_safe_boundary:
            batch = claim_safe_boundary("owner", TURN, db=db)
        else:
            db.execute("UPDATE active_turns SET status='completed' WHERE id=?", (TURN,))
            db.commit()
            batch = start_idle_turn(
                "owner", turn_id="new-turn", boot_epoch="boot", started_at="t2", db=db,
            )
        assert batch is not None
        assert [item.message_id for item in batch.items if item.role == "user"] == ["next-user"]
        assert wake_id not in [item.message_id for item in batch.items]
        assert _row(db, wake_id)["delivery_state"] == "cancelled"
    finally:
        db.close()
