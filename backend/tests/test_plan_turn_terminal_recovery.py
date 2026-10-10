"""Boot recovery and existing hold rows under Turn-terminal plan settlement.

The one boot recovery pass ends a dead Turn through the same terminal owner, so
an orphaned open dispatch settles as ``unknown`` with the same deterministic
selection result and no hold. ``uncertain_effect`` hold rows already in a
database, with their released attempts and settled dispatches, stay readable
and are never swept by terminal settlement.
"""

from __future__ import annotations

import pytest

from infra.thread_inputs import ClaimedBatch, FailedInputBatch
from infra.thread_driver import (
    TerminalResult,
    TerminalSettlement,
    finish_turn,
)
from infra.thread_recovery import recover_threads
from plans import admission, public, service, turn_admission
from tests.test_clean_turn_terminal import _db, _running_turn, _session
from tests.test_plan_turn_terminal_settlement import (
    THREAD,
    _attempt,
    _dispatch,
    _open_dispatch,
    _queue_next_input,
    _selected_task,
    _task,
)

_EXISTING_HOLD = "existing-uncertain-hold"
_EXISTING_ATTEMPT = "existing-attempt"
_EXISTING_DISPATCH = "existing-dispatch"


def _insert_existing_uncertain_effect(db, plan_id: str, node_id: str) -> None:
    """Write an uncleared ``uncertain_effect`` hold with its attempt and dispatch.

    A dead Turn's call settled ``unknown``, its attempt was released as
    ``blocked``, and the hold stayed uncleared.
    """
    db.execute(
        """INSERT INTO plan_node_attempts
           (id,plan_id,node_id,executor_thread_id,requirements_revision,
            attempt_ordinal,selected_by_actor_id,provenance,selected_at,
            released_at,release_reason)
           VALUES (?,?,?,?,1,1,?,'existing_data','t0','t1','blocked')""",
        (_EXISTING_ATTEMPT, plan_id, node_id, THREAD, THREAD),
    )
    db.execute(
        """INSERT INTO plan_tool_dispatches
           (id,attempt_id,turn_id,tool_name,started_at,settled_at,outcome)
           VALUES (?,?,'existing-turn','bash','t0','t1','unknown')""",
        (_EXISTING_DISPATCH, _EXISTING_ATTEMPT),
    )
    db.execute(
        """INSERT INTO plan_node_holds
           (id,node_id,kind,reason_key,reason,owner_kind,owner_id,
            provenance,created_at)
           VALUES (?,?,'blocked','uncertain_effect',
                   'A tool effect ended without a settled result',
                   'thread',?,'turn_terminal','t1')""",
        (_EXISTING_HOLD, node_id, THREAD),
    )


def _existing_rows(db) -> list[tuple]:
    """Return every column of the three existing rows, for exact comparison."""
    hold = db.execute("SELECT * FROM plan_node_holds WHERE id=?", (_EXISTING_HOLD,))
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (_EXISTING_ATTEMPT,),
    )
    dispatch = db.execute(
        "SELECT * FROM plan_tool_dispatches WHERE id=?", (_EXISTING_DISPATCH,),
    )
    return [tuple(hold.fetchone()), tuple(attempt.fetchone()), tuple(dispatch.fetchone())]


def test_boot_recovery_settles_an_orphaned_dispatch_without_a_hold() -> None:
    """A Turn from a dead boot ends as error and its open call becomes unknown.

    The selection it ran under is still valid, so recovery keeps it, the
    successor Turn claimed by the same pass is bound to it, and repeating the
    pass changes nothing.
    """
    db = _db()
    try:
        _session(db, THREAD)
        _plan, task, attempt_id = _selected_task(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, attempt_id, "turn-1")
        _queue_next_input(db, THREAD)
        db.commit()

        recovery = recover_threads(
            boot_epoch="boot-2", recovered_at="t9",
            successor_id=lambda _thread_id: "turn-2", db=db,
        )
        settled = tuple(_dispatch(db))
        repeated = recover_threads(
            boot_epoch="boot-2", recovered_at="t10",
            successor_id=lambda _thread_id: "turn-3", db=db,
        )

        assert [item.result for item in recovery.settlements] == [TerminalResult.APPLIED]
        assert repeated.settlements == ()
        assert settled[0] == "unknown"
        assert tuple(_dispatch(db)) == settled
        assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
        assert _attempt(db, attempt_id)["released_at"] is None
        projected = admission.project(task.id, db=db)
        assert projected["condition"] == "running"
        assert projected["selection"]["attempt_id"] == attempt_id
        assert [tuple(row) for row in db.execute(
            "SELECT id,status,plan_attempt_id FROM active_turns ORDER BY turn_order"
        )] == [("turn-1", "error", attempt_id), ("turn-2", "running", attempt_id)]
    finally:
        db.close()


def test_idle_input_plan_failure_ends_the_exact_accepted_prefix(monkeypatch) -> None:
    """Ordinary admission commits one visible failure instead of rolling the send back."""
    from infra.input_admission import accept_thread_input
    from infra.thread_driver import admit_input

    db = _db()
    try:
        _session(db, THREAD)
        accept_thread_input(
            THREAD,
            role="system",
            content="earlier wake",
            intent="apply_now",
            message_id=f"{THREAD}-ahead",
            db=db,
        )

        def snapshot(_thread_id, **_kwargs):
            raise service.PlanError("injected plan failure")

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        admitted = admit_input(
            THREAD,
            role="user",
            content="continue",
            intent="apply_now",
            message_id=f"{THREAD}-direct",
            turn_id="turn-direct",
            boot_epoch="boot-2",
            admitted_at="t9",
            db=db,
        )

        assert admitted.batch is not None
        message_ids = admitted.batch.message_ids
        assert len(message_ids) == 3
        assert message_ids[0] == f"{THREAD}-ahead"
        assert message_ids[1].startswith(f"context-digest:{THREAD}:")
        assert message_ids[2] == f"{THREAD}-direct"
        rows = db.execute(
            "SELECT id,delivery_state,delivery_error,seq,active_turn_id "
            "FROM messages WHERE thread_id=? AND role IN ('user','system') ORDER BY seq",
            (THREAD,),
        ).fetchall()
        assert tuple(row["id"] for row in rows) == message_ids
        assert all(row["delivery_state"] == "failed" for row in rows)
        assert all("Pause the active plan" in row["delivery_error"] for row in rows)
        assert all(row["active_turn_id"] is None for row in rows)
        assert db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=?",
            (THREAD,),
        ).fetchone() is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_send_broadcasts_its_committed_plan_failure(monkeypatch) -> None:
    """The ordinary Send path publishes the failed row after admission commits."""
    from infra import input_preparation, send_service, turn_admission_driver
    from infra.send_types import SendRejected

    db = _db()
    broadcasts = []
    receipts = []
    try:
        _session(db, THREAD)
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id=?", (THREAD,),
        ).fetchone()

        def snapshot(_thread_id, **_kwargs):
            raise service.PlanError("injected plan failure")

        async def run_with_db(function, *args, **kwargs):
            return function(*args, **{**kwargs, "db": db})

        async def broadcast(thread_id, message_ids):
            broadcasts.append((thread_id, message_ids))

        async def on_admitted(receipt):
            receipts.append(receipt)

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        monkeypatch.setattr(send_service, "run_in_db", run_with_db)
        # Admission runs through the input-preparation entry point.
        monkeypatch.setattr(input_preparation, "run_in_db", run_with_db)
        monkeypatch.setattr(turn_admission_driver, "broadcast_message_rows", broadcast)
        with pytest.raises(SendRejected) as caught:
            await send_service.send_thread_input(
                THREAD,
                thread_row,
                "continue",
                input_id=f"{THREAD}-direct-send",
                native_mode="delegation",
                broadcast_input=False,
                on_admitted=on_admitted,
            )

        assert caught.value.input_undeliverable
        assert "Pause the active plan" in caught.value.detail
        assert receipts == []
        assert len(broadcasts) == 1 and broadcasts[0][0] == THREAD
        message_ids = broadcasts[0][1]
        assert len(message_ids) == 2
        assert message_ids[0].startswith(f"context-digest:{THREAD}:")
        assert message_ids[1] == f"{THREAD}-direct-send"
        failed = db.execute(
            "SELECT delivery_state,delivery_error,active_turn_id "
            f"FROM messages WHERE id IN ({','.join('?' for _ in message_ids)})",
            message_ids,
        ).fetchall()
        assert len(failed) == len(message_ids)
        assert all(row["delivery_state"] == "failed" for row in failed)
        assert all("Pause the active plan" in row["delivery_error"] for row in failed)
        assert all(row["active_turn_id"] is None for row in failed)
        with pytest.raises(SendRejected) as duplicate:
            await send_service.send_thread_input(
                THREAD, thread_row, "continue",
                input_id=f"{THREAD}-direct-send", native_mode="delegation",
                broadcast_input=False, on_admitted=on_admitted,
            )
        assert duplicate.value.input_undeliverable
        assert receipts == []
        assert len(broadcasts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_send_does_not_report_an_earlier_epoch_failure_as_its_own(
    monkeypatch,
) -> None:
    """A destination input stays queued while the failed source drains its switch."""
    from infra import input_preparation, send_service, turn_admission_driver
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch
    from infra.send_types import InboxQueuedInput
    from thread import model_pending

    db = _db()
    broadcasts = []
    switch_drives = []
    try:
        _session(db, THREAD)
        accept_thread_input(
            THREAD, role="user", content="source", intent="apply_now",
            message_id="source-input", db=db,
        )
        request_switch(
            THREAD, "native/destination", operation_id="switch-after-failure",
            requested_at="t1", db=db,
        )
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id=?", (THREAD,),
        ).fetchone()

        def snapshot(_thread_id, **_kwargs):
            raise service.PlanError("injected plan failure")

        async def run_with_db(function, *args, **kwargs):
            return function(*args, **{**kwargs, "db": db})

        async def broadcast(thread_id, message_ids):
            broadcasts.append((thread_id, message_ids))

        async def drive_switch(thread_id):
            switch_drives.append(thread_id)
            return object()

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        monkeypatch.setattr(send_service, "run_in_db", run_with_db)
        # Admission runs through the input-preparation entry point.
        monkeypatch.setattr(input_preparation, "run_in_db", run_with_db)
        monkeypatch.setattr(turn_admission_driver, "broadcast_message_rows", broadcast)
        monkeypatch.setattr(model_pending, "drive_pending_model_switch", drive_switch)

        result = await send_service.send_thread_input(
            THREAD, thread_row, "destination", input_id="destination-input",
            native_mode="delegation", broadcast_input=False,
        )

        assert isinstance(result, InboxQueuedInput)
        assert broadcasts == [(THREAD, ("source-input",))]
        assert switch_drives == [THREAD]
        assert db.execute(
            "SELECT delivery_state FROM messages WHERE id='source-input'",
        ).fetchone()[0] == "failed"
        assert db.execute(
            "SELECT delivery_state FROM messages WHERE id='destination-input'",
        ).fetchone()[0] == "queued"
    finally:
        db.close()


def test_finish_turn_settles_before_a_plan_admission_failure(monkeypatch, caplog) -> None:
    """A failed successor admission rolls back alone; the finished Turn stays settled."""
    from infra import idle_checkpoints
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        _session(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _queue_next_input(db, THREAD)
        accept_thread_input(
            THREAD, role="system", content="context", intent="queue_after_turn",
            message_id=f"{THREAD}-next-context", db=db,
        )
        monkeypatch.setattr(
            idle_checkpoints,
            "idle_checkpoint_exclusion_reason",
            lambda *_args, **_kwargs: None,
        )

        def snapshot(_thread_id, **_kwargs):
            raise service.PlanError("injected plan failure")

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        settlement = finish_turn(
            "turn-1",
            outcome="completed",
            ended_at="t9",
            successor_turn_id="turn-2",
            boot_epoch="boot-2",
            db=db,
        )

        assert settlement.result is TerminalResult.APPLIED
        assert isinstance(settlement.successor, FailedInputBatch)
        assert settlement.idle_checkpoint is not None
        assert settlement.successor.thread_id == THREAD
        assert settlement.successor.message_ids == (
            f"{THREAD}-next", f"{THREAD}-next-context",
        )
        turn = db.execute(
            "SELECT status,idle_checkpoint_pending FROM active_turns WHERE id='turn-1'",
        ).fetchone()
        assert (turn["status"], turn["idle_checkpoint_pending"]) == ("completed", 1)
        failed = db.execute(
            "SELECT id,delivery_state,delivery_error,seq,active_turn_id "
            "FROM messages WHERE id IN (?,?) ORDER BY seq",
            (f"{THREAD}-next", f"{THREAD}-next-context"),
        ).fetchall()
        assert [row["id"] for row in failed] == [
            f"{THREAD}-next", f"{THREAD}-next-context",
        ]
        assert all(row["delivery_state"] == "failed" for row in failed)
        assert all("Pause the active plan" in row["delivery_error"] for row in failed)
        assert [row["seq"] for row in failed] == [2, 3]
        assert all(row["active_turn_id"] is None for row in failed)
        assert "injected plan failure" in caplog.text
        assert "Turn plan admission failed" in caplog.text
    finally:
        db.close()


def test_startup_recovery_isolates_plan_admission_failure(monkeypatch, caplog) -> None:
    """One bad idle thread fails visibly without losing a good sibling."""
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        for thread_id in ("good-plan-thread", "bad-plan-thread"):
            _session(db, thread_id)
            accept_thread_input(
                thread_id,
                role="user",
                content="continue",
                intent="apply_now",
                message_id=f"{thread_id}-input",
                db=db,
            )
        real_snapshot = turn_admission.snapshot_after_admission

        def snapshot(thread_id, **kwargs):
            if thread_id == "bad-plan-thread":
                raise service.PlanError("injected plan failure")
            return real_snapshot(thread_id, **kwargs)

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        db.commit()
        recovery = recover_threads(
            boot_epoch="boot-2",
            recovered_at="t9",
            successor_id=lambda thread_id: f"recovered-{thread_id}",
            db=db,
        )

        assert [batch.thread_id for batch in recovery.batches] == [
            "bad-plan-thread", "good-plan-thread",
        ]
        assert isinstance(recovery.batches[0], FailedInputBatch)
        assert isinstance(recovery.batches[1], ClaimedBatch)
        assert db.execute(
            "SELECT 1 FROM active_turns WHERE id='recovered-good-plan-thread'",
        ).fetchone() is not None
        assert db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id='bad-plan-thread'",
        ).fetchone() is None
        failed = db.execute(
            "SELECT delivery_state,delivery_error,seq,active_turn_id "
            "FROM messages WHERE id='bad-plan-thread-input'",
        ).fetchone()
        assert failed["delivery_state"] == "failed"
        assert "Pause the active plan" in failed["delivery_error"]
        assert (failed["seq"], failed["active_turn_id"]) == (1, None)
        assert "injected plan failure" in caplog.text
        assert "Turn plan admission failed" in caplog.text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_terminal_driver_broadcasts_a_committed_admission_failure(monkeypatch):
    """The delivery error reaches the existing message surface after commit."""
    from infra import thread_execution, turn_admission_driver

    broadcasts = []
    switch_drives = []

    async def broadcast(thread_id, message_ids):
        broadcasts.append((thread_id, message_ids))

    async def drive_switch(thread_id):
        switch_drives.append(thread_id)
        return None

    async def report(_settlement):
        return None

    from thread import model_pending

    monkeypatch.setattr(turn_admission_driver, "broadcast_message_rows", broadcast)
    monkeypatch.setattr(model_pending, "drive_pending_model_switch", drive_switch)
    monkeypatch.setattr(thread_execution, "report_terminal_unknown", report)
    failure = FailedInputBatch(THREAD, ("failed-input",), "plan admission failed")
    settlement = TerminalSettlement(
        TerminalResult.APPLIED,
        THREAD,
        successor=failure,
    )

    await thread_execution.drive_terminal_settlement(settlement)

    assert broadcasts == [(THREAD, ("failed-input",))]
    assert switch_drives == [THREAD]


def test_startup_recovery_propagates_non_plan_admission_failures(monkeypatch) -> None:
    """Unexpected admission failure rolls back the whole startup recovery pass."""
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        _session(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "stale-turn")
        _session(db, "bad-runtime-thread")
        accept_thread_input(
            "bad-runtime-thread",
            role="user",
            content="continue",
            intent="apply_now",
            message_id="bad-runtime-input",
            db=db,
        )
        monkeypatch.setattr(
            public,
            "snapshot_after_admission",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("unexpected admission failure")
            ),
        )

        with pytest.raises(ValueError, match="unexpected admission failure"):
            recover_threads(
                boot_epoch="boot-2",
                recovered_at="t9",
                successor_id=lambda thread_id: f"recovered-{thread_id}",
                db=db,
            )

        stale = db.execute(
            "SELECT status,ended_at FROM active_turns WHERE id='stale-turn'",
        ).fetchone()
        assert (stale["status"], stale["ended_at"]) == ("running", None)
        assert db.execute(
            "SELECT delivery_state FROM messages WHERE id='bad-runtime-input'",
        ).fetchone()["delivery_state"] == "queued"
        assert db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id='bad-runtime-thread'",
        ).fetchone() is None
    finally:
        db.close()


def test_finish_turn_propagates_non_plan_admission_failures(monkeypatch) -> None:
    """Only PlanError is recoverable; other successor-admission errors stay loud."""
    db = _db()
    try:
        _session(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _queue_next_input(db, THREAD)
        monkeypatch.setattr(
            public,
            "snapshot_after_admission",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad admission")),
        )

        with pytest.raises(ValueError, match="bad admission"):
            finish_turn(
                "turn-1",
                outcome="completed",
                ended_at="t9",
                successor_turn_id="turn-2",
                boot_epoch="boot-2",
                db=db,
            )

        assert db.execute(
            "SELECT status FROM active_turns WHERE id='turn-1'",
        ).fetchone()["status"] == "running"
    finally:
        db.close()


def test_terminal_settlement_leaves_existing_uncertain_effect_rows_untouched() -> None:
    """Old hold, attempt and dispatch rows survive a terminal byte for byte.

    The Turn ending here owns an open call on other, valid work, so settlement
    really runs. It neither clears nor rewrites the existing hold, the held
    task still reads as blocked by it, and settlement adds no hold of its own.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan = service.ensure_plan("thread", THREAD, db=db)
        held = _task(db, plan.id, "Uncertain effect blocked work", 0)
        current = _task(db, plan.id, "Current work", 1)
        service.transition_node(held.id, "active", db=db)
        _insert_existing_uncertain_effect(db, plan.id, held.id)
        selected = admission.activate(
            current.id,
            expected_revision=current.revision,
            actor_id=THREAD,
            provenance="test",
            db=db,
        )
        attempt_id = selected["admission"]["selection"]["attempt_id"]
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, attempt_id, "turn-1")
        before = _existing_rows(db)

        result = finish_turn(
            "turn-1",
            outcome="error",
            ended_at="t4",
            successor_turn_id="turn-2",
            boot_epoch="boot-1",
            error="backend restarted",
            db=db,
        )

        held_view = admission.project(held.id, db=db)
        assert result.result is TerminalResult.APPLIED
        assert _existing_rows(db) == before
        assert held_view["condition"] == "blocked"
        assert [cause["reason_key"] for cause in held_view["causes"]] == [
            "uncertain_effect",
        ]
        assert held_view["selection"] is None
        assert _dispatch(db)["outcome"] == "unknown"
        assert [row["id"] for row in db.execute("SELECT id FROM plan_node_holds")] == [
            _EXISTING_HOLD,
        ]
        assert _attempt(db, attempt_id)["released_at"] is None
        assert admission.project(current.id, db=db)["condition"] == "running"
    finally:
        db.close()
