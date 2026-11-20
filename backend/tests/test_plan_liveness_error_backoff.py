"""A liveness reminder queued during a Turn that errors does not restart it at once.

The reminder steered the running Turn (the flat in-Turn interval, cycle held),
but a Turn blocked in one provider call never read it. Its terminal withdraws
it before the successor claim, and the idle backoff applies instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from automation import plan_liveness

THREAD = "main-1"


def _iso(seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _running_turn(main_db, turn_id: str) -> str:
    from core.turn_core import persist_turn_start
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        THREAD, role="user", content="Work", intent="apply_now",
        message_id=f"{turn_id}:input", turn_id=turn_id, boot_epoch="test-boot",
        admitted_at="t", db=main_db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id=f"{turn_id}:write", started_at="t", db=main_db)
    settle_batch_write(f"{turn_id}:write", outcome="accepted", db=main_db)
    model = main_db.execute("SELECT model FROM threads WHERE id=?", (THREAD,)).fetchone()["model"]
    persist_turn_start(turn_id, THREAD, model, "Work", db=main_db)
    # The Turn started well before the reminder was collected.
    main_db.execute("UPDATE active_turns SET started_at=? WHERE id=?", (_iso(-900), turn_id))
    main_db.commit()
    return turn_id


def _queue(main_db, content: str, *, role="user", continuation=None) -> str:
    from infra.thread_driver import admit_input

    message_id = uuid.uuid4().hex
    admit_input(
        THREAD, role=role, content=content, intent="apply_now",
        message_id=message_id, turn_id=uuid.uuid4().hex, boot_epoch="test-boot",
        admitted_at="t", continuation=continuation, db=main_db,
    )
    main_db.commit()
    return message_id


def _deliver_reminder_into_running_turn(main_db) -> str:
    """Open plan work; a due reminder is queued and settled while the Turn runs."""
    from automation.continuation.execution import event_for
    from automation.continuation.sources.liveness import LivenessSource
    from automation.continuation.validity import condition_for_event
    from plans import boundary, service

    plan = service.ensure_plan("thread", THREAD, db=main_db)
    node = service.add_node(
        plan.id, kind="task", title="Continue the selected work",
        acceptance_criteria="The selected work finishes", db=main_db,
    )
    boundary.mutate(
        plan.id, "activate", {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor(THREAD, "steve", None),
        provenance="test.error_backoff", mutation_id="activate", db=main_db,
    )
    plan_liveness.on_turn_complete(THREAD, db=main_db)
    main_db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?", (_iso(-1), THREAD),
    )
    main_db.commit()
    source = LivenessSource()
    request = source.due(_iso(), db=main_db)[0]
    _event, _summary, details = event_for(request)
    condition = condition_for_event("plan.liveness_wake", details)
    wake_id = _queue(
        main_db, "Actionable structured plan work remains.", role="system",
        continuation=condition,
    )
    # Delivery into a running Turn: the flat in-Turn interval, cycle held.
    wake = plan_liveness.row_to_wake(dict(plan_liveness.wake_for(THREAD, main_db)))
    source._settle_delivery(wake, _iso(), main_db)
    main_db.commit()
    return wake_id


def _wake_row(main_db):
    return main_db.execute(
        "SELECT status,cycle,deadline FROM plan_liveness_wakes WHERE thread_id=?", (THREAD,),
    ).fetchone()


def _message(main_db, message_id: str):
    return main_db.execute(
        "SELECT delivery_state,delivery_error,active_turn_id FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()


def _seconds_until(deadline: str) -> float:
    return (datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds()


def _error_terminal(main_db, turn_id: str, error: str = "provider sent no data for 600s"):
    from core.turn_core import persist_turn_settlement

    settlement = persist_turn_settlement(turn_id, "error", error, db=main_db)
    main_db.commit()
    return settlement


def test_reminder_queued_during_an_erroring_turn_does_not_restart_it(main_db):
    """The reproduction: today the successor claims the reminder at cycle 0."""
    turn_id = _running_turn(main_db, "turn-error-wake")
    wake_id = _deliver_reminder_into_running_turn(main_db)
    assert _wake_row(main_db)["cycle"] == 0

    settlement = _error_terminal(main_db, turn_id)

    assert settlement.successor is None
    message = _message(main_db, wake_id)
    assert (message["delivery_state"], message["delivery_error"]) == ("cancelled", "stale_plan_wake")
    row = _wake_row(main_db)
    assert row["status"] == "armed" and row["cycle"] == 1
    spacing = plan_liveness.PLAN_WAKE_IDLE_SECONDS * plan_liveness.PLAN_WAKE_BACKOFF
    assert spacing - 5 < _seconds_until(row["deadline"]) <= spacing


def test_the_backoff_keeps_doubling_across_repeated_errors(main_db):
    from automation.plan_liveness_terminal import on_thread_turn_complete
    from automation.turn_end_effects import PendingTurnEndEffects

    turn_id = _running_turn(main_db, "turn-error-1")
    _deliver_reminder_into_running_turn(main_db)
    _error_terminal(main_db, turn_id)
    on_thread_turn_complete(
        THREAD, outcome="error", error="provider sent no data", turn_id=turn_id,
        db=main_db, effects=PendingTurnEndEffects(),
    )
    main_db.commit()
    assert _wake_row(main_db)["cycle"] == 1

    # The next reminder comes due and starts its own Turn, which errors too.
    from automation.continuation.execution import event_for
    from automation.continuation.sources.liveness import LivenessSource
    from automation.continuation.validity import condition_for_event

    main_db.execute("UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?", (_iso(-1), THREAD))
    main_db.commit()
    source = LivenessSource()
    _event, _summary, details = event_for(source.due(_iso(), db=main_db)[0])
    _queue(
        main_db, "Actionable structured plan work remains.", role="system",
        continuation=condition_for_event("plan.liveness_wake", details),
    )
    wake = plan_liveness.row_to_wake(dict(plan_liveness.wake_for(THREAD, main_db)))
    source._settle_delivery(wake, _iso(), main_db)  # idle: started its own Turn
    main_db.commit()
    row = _wake_row(main_db)
    assert row["cycle"] == 2
    spacing = plan_liveness.PLAN_WAKE_IDLE_SECONDS * plan_liveness.PLAN_WAKE_BACKOFF ** 2
    assert spacing - 5 < _seconds_until(row["deadline"]) <= spacing
    second = main_db.execute(
        "SELECT id FROM active_turns WHERE thread_id=? AND status='running'", (THREAD,),
    ).fetchone()["id"]
    _error_terminal(main_db, second)
    assert _wake_row(main_db)["cycle"] == 2


def test_a_persons_message_behind_the_reminder_still_starts_a_turn(main_db):
    turn_id = _running_turn(main_db, "turn-error-fifo")
    wake_id = _deliver_reminder_into_running_turn(main_db)
    person_id = _queue(main_db, "Please carry on")

    settlement = _error_terminal(main_db, turn_id)

    assert settlement.successor is not None
    assert _message(main_db, wake_id)["delivery_state"] == "cancelled"
    person = _message(main_db, person_id)
    assert person["delivery_state"] == "queued" and person["active_turn_id"] is not None


def test_a_persons_message_alone_still_starts_a_turn_after_an_error(main_db):
    turn_id = _running_turn(main_db, "turn-error-person")
    person_id = _queue(main_db, "Next thing")

    settlement = _error_terminal(main_db, turn_id)

    assert settlement.successor is not None
    assert _message(main_db, person_id)["active_turn_id"] is not None


def test_a_reminder_the_turn_claimed_but_never_wrote_is_withdrawn_too(main_db):
    """Released back to the queue by the terminal, it is withdrawn, not re-run."""
    turn_id = _running_turn(main_db, "turn-error-claimed")
    wake_id = _deliver_reminder_into_running_turn(main_db)
    main_db.execute("UPDATE messages SET active_turn_id=? WHERE id=?", (turn_id, wake_id))
    main_db.commit()

    settlement = _error_terminal(main_db, turn_id)

    assert settlement.successor is None
    assert _message(main_db, wake_id)["delivery_state"] == "cancelled"
    assert _wake_row(main_db)["cycle"] == 1


def test_a_restart_error_keeps_its_own_recovery(main_db):
    turn_id = _running_turn(main_db, "turn-error-restart")
    wake_id = _deliver_reminder_into_running_turn(main_db)

    _error_terminal(main_db, turn_id, "backend restarted while Turn was running")

    assert _message(main_db, wake_id)["delivery_state"] == "queued"
    assert _wake_row(main_db)["cycle"] == 0


def test_a_completed_turn_leaves_the_queued_reminder_to_run(main_db):
    from core.turn_core import persist_turn_settlement

    turn_id = _running_turn(main_db, "turn-complete-wake")
    wake_id = _deliver_reminder_into_running_turn(main_db)

    settlement = persist_turn_settlement(turn_id, "completed", None, db=main_db)
    main_db.commit()

    assert settlement.successor is not None
    assert _message(main_db, wake_id)["active_turn_id"] is not None
