"""Commit-order contracts for the clean model-switch boundary."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

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


def test_input_committed_before_switch_drains_to_source() -> None:
    """The database commit order, not request arrival time, chooses the model."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.model_switches import request_switch
    from infra.thread_driver import finish_turn
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        before = accept_thread_input(
            "thread-1", role="user", content="before", intent="apply_now",
            message_id="before", db=db,
        )
        request = request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        after = accept_thread_input(
            "thread-1", role="user", content="after", intent="apply_now",
            message_id="after", db=db,
        )
        assert request.switch_after_input_order == before.input_order == 1
        assert (before.target_execution_epoch, before.target_model) == (
            0, "native/source",
        )
        assert (after.target_execution_epoch, after.target_model) == (
            1, "native/target",
        )

        db.commit()
        first = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t2", db=db,
        )
        assert first is not None
        assert [item.message_id for item in first.items] == ["before"]
        begin_batch_write(first, attempt_id="write-1", started_at="t2", db=db)
        settle_batch_write("write-1", outcome="accepted", db=db)
        db.execute(
            "UPDATE threads SET switch_status='ready' WHERE switch_operation_id='switch-1'",
        )
        db.commit()

        settled = finish_turn(
            "turn-1", outcome="completed", ended_at="t4",
            successor_turn_id="turn-2", boot_epoch="boot-1", db=db,
        )
        assert settled.successor is not None
        assert settled.successor.target_model == "native/target"
        assert [item.message_id for item in settled.successor.items] == ["after"]
        session = db.execute(
            "SELECT model,execution_epoch,pending_model FROM threads "
            "WHERE id='thread-1'"
        ).fetchone()
        assert tuple(session) == ("native/target", 1, None)
    finally:
        db.close()


def test_switch_committed_before_input_targets_destination() -> None:
    """A message sent after the switch request waits for that destination."""
    from infra.database import write_transaction
    from infra.model_switches import activate_ready_switch, request_switch
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        queued = accept_thread_input(
            "thread-1", role="user", content="destination", intent="apply_now",
            message_id="after", db=db,
        )
        assert queued.target_model == "native/target"
        assert start_idle_turn(
            "thread-1", turn_id="too-early", boot_epoch="boot-1",
            started_at="t2", db=db,
        ) is None

        with write_transaction(db, scope="test_switch_commit") as connection:
            connection.execute(
                "UPDATE threads SET switch_status='ready' "
                "WHERE switch_operation_id='switch-1'",
            )
            ready = activate_ready_switch(
                connection, "thread-1", committed_at="t3",
            )
        assert ready is not None and ready.committed is True
        db.commit()
        successor = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t3", db=db,
        )
        assert successor is not None
        assert successor.target_model == "native/target"
        assert [item.message_id for item in successor.items] == ["after"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_committed_switch_destination_plan_failure_fails_exact_prefix(
    monkeypatch,
) -> None:
    """The live post-commit driver preserves the switch and exposes Plans failure."""
    from infra import thread_execution, turn_admission_driver
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.model_switches import activate_ready_switch, request_switch
    from plans import public, service, turn_admission
    from thread import model_pending

    db = _db()
    broadcasts = []
    try:
        request_switch(
            "thread-1",
            "native/target",
            operation_id="switch-plan-error",
            requested_at="t1",
            db=db,
        )
        accept_thread_input(
            "thread-1",
            role="user",
            content="destination",
            intent="apply_now",
            message_id="destination-input",
            db=db,
        )
        with write_transaction(db, scope="test_switch_commit") as connection:
            connection.execute(
                "UPDATE threads SET switch_status='ready' WHERE id='thread-1'",
            )
            assert activate_ready_switch(
                connection, "thread-1", committed_at="t3",
            ) is not None

        def snapshot(_thread_id, **_kwargs):
            raise service.PlanError("injected plan failure")

        async def run_with_db(function, *args, **kwargs):
            return function(*args, db=db, **kwargs)

        async def broadcast(thread_id, message_ids):
            broadcasts.append((thread_id, message_ids))

        monkeypatch.setattr(public, "snapshot_after_admission", snapshot)
        monkeypatch.setattr(thread_execution, "run_in_db", run_with_db)
        monkeypatch.setattr(turn_admission_driver, "broadcast_message_rows", broadcast)
        await model_pending._drive_destination("thread-1")

        session = db.execute(
            "SELECT model,execution_epoch,pending_model,switch_status "
            "FROM threads WHERE id='thread-1'",
        ).fetchone()
        assert tuple(session) == ("native/target", 1, None, None)
        failed = db.execute(
            "SELECT delivery_state,delivery_error,seq,active_turn_id "
            "FROM messages WHERE id='destination-input'",
        ).fetchone()
        assert failed["delivery_state"] == "failed"
        assert "Pause the active plan" in failed["delivery_error"]
        assert (failed["seq"], failed["active_turn_id"]) == (1, None)
        assert broadcasts == [("thread-1", ("destination-input",))]
        assert db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id='thread-1'",
        ).fetchone() is None
    finally:
        db.close()


def test_reselect_destination_keeps_the_same_fifo_boundary() -> None:
    """A newer selection retargets only the still-pending destination epoch."""
    from infra.database import write_transaction
    from infra.input_admission import accept_thread_input
    from infra.model_switches import activate_ready_switch, request_switch
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        first = request_switch(
            "thread-1", "native/small", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        queued = accept_thread_input(
            "thread-1", role="user", content="after", intent="apply_now",
            message_id="after", db=db,
        )
        second = request_switch(
            "thread-1", "native/large", operation_id="switch-2",
            requested_at="t2", db=db,
        )

        assert second.operation_id == "switch-2"
        assert second.switch_after_input_order == first.switch_after_input_order
        assert second.destination_execution_epoch == first.destination_execution_epoch
        retargeted = db.execute(
            "SELECT target_model,target_execution_epoch FROM messages WHERE id='after'",
        ).fetchone()
        assert tuple(retargeted) == ("native/large", queued.target_execution_epoch)

        with write_transaction(db, scope="test_reselected_switch_commit") as connection:
            connection.execute(
                "UPDATE threads SET switch_status='ready' "
                "WHERE switch_operation_id='switch-2'",
            )
            transition = activate_ready_switch(
                connection, "thread-1", committed_at="t3",
            )
        assert transition is not None
        db.commit()
        successor = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t3", db=db,
        )
        assert successor is not None
        assert successor.target_model == "native/large"
    finally:
        db.close()


def test_failed_switch_returns_its_pending_input_and_starts_the_source() -> None:
    """The switch alone fails; an input it never wrote returns to the source.

    A failed destination cannot answer anything, so an input accepted while
    the switch was still preparing must not stay stranded there: it returns
    to the source in the same transaction that fails the switch (D4).
    """
    from infra.model_switches import fail_switch, request_switch
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        request_switch(
            "thread-1", "native/tiny", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        accepted = accept_thread_input(
            "thread-1", role="user", content="wait", intent="apply_now",
            message_id="after", db=db,
        )
        assert accepted.target_model == "native/tiny"  # Still preparing when accepted.
        fail_switch("switch-1", error="minimum replay does not fit", db=db)
        row = db.execute(
            "SELECT model,execution_epoch,pending_model,switch_status,switch_error "
            "FROM threads WHERE id='thread-1'"
        ).fetchone()
        assert tuple(row) == (
            "native/source", 0, "native/tiny", "failed",
            "minimum replay does not fit",
        )
        db.commit()
        batch = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t2", db=db,
        )
        assert batch is not None
        assert batch.target_model == "native/source"
        assert [item.message_id for item in batch.items] == ["after"]
    finally:
        db.close()


def test_a_later_failure_cannot_overwrite_the_first_cause() -> None:
    """The first cause is the diagnosable one; a consequence must not erase it."""
    from infra.model_switches import fail_switch, request_switch

    db = _db()
    try:
        request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        first = fail_switch(
            "switch-1", error="cold fold input does not fit", db=db,
        )
        second = fail_switch(
            "switch-1",
            error="model switch pending boundary changed before commit",
            db=db,
        )

        row = db.execute(
            "SELECT switch_status,switch_error FROM threads WHERE id='thread-1'"
        ).fetchone()
        assert tuple(row) == ("failed", "cold fold input does not fit")
        # Every caller is told the cause the row holds, not the one it passed.
        assert first.error == "cold fold input does not fit"
        assert second.error == "cold fold input does not fit"
    finally:
        db.close()


def test_failed_switch_can_retry_the_same_destination() -> None:
    """Retry replaces the failed operation without moving its FIFO boundary."""
    from infra.model_switches import fail_switch, request_switch

    db = _db()
    try:
        first = request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        fail_switch("switch-1", error="checkpoint cancelled", db=db)

        retry = request_switch(
            "thread-1", "native/target", operation_id="switch-2",
            requested_at="t2", db=db,
        )

        assert retry.operation_id == "switch-2"
        assert retry.duplicate is False
        assert retry.switch_after_input_order == first.switch_after_input_order
        row = db.execute(
            "SELECT switch_status,switch_error FROM threads WHERE id='thread-1'"
        ).fetchone()
        assert tuple(row) == ("preparing", None)
    finally:
        db.close()


def test_destination_fit_projects_the_clean_pending_fifo() -> None:
    """Switch preparation reads messages directly; no legacy inbox exists."""
    from infra.model_switches import request_switch
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from thread.model_projection import read_model_switch_fit_input

    db = _db()
    try:
        request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t1", db=db,
        )
        accept_thread_input(
            "thread-1", role="user", content="first", intent="queue_after_turn",
            message_id="after-1", db=db,
        )
        accept_thread_input(
            "thread-1", role="user", content="second", intent="apply_now",
            message_id="after-2", db=db,
        )

        projected = read_model_switch_fit_input(
            db,
            "thread-1",
            destination_model="native/target",
            destination_target=SimpleNamespace(
                catalog_id="native/target",
                harness="native",
                supports_vision=True,
            ),
        )

        assert "[1/2]\nfirst" in projected.content
        assert "[2/2]\nsecond" in projected.content
        assert "[1/2]\nfirst" in projected.source_content
        assert projected.identity.startswith("input-projection:")
        assert [receipt.message_id for receipt in projected.receipts] == [
            "after-1", "after-2",
        ]
    finally:
        db.close()


def test_terminal_exposes_pending_switch_before_destination_input_runs() -> None:
    """The terminal winner hands the pending switch to the external driver."""
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.model_switches import request_switch
    from infra.thread_driver import admit_input, finish_turn
    from infra.input_admission import accept_thread_input

    db = _db()
    try:
        db.commit()
        source = admit_input(
            "thread-1",
            role="user",
            content="finish this first",
            intent="apply_now",
            message_id="source-input",
            turn_id="source-turn",
            boot_epoch="boot-1",
            admitted_at="t1",
            db=db,
        )
        assert source.batch is not None
        begin_batch_write(
            source.batch, attempt_id="source-write", started_at="t1", db=db,
        )
        settle_batch_write("source-write", outcome="accepted", db=db)
        request_switch(
            "thread-1", "native/target", operation_id="switch-1",
            requested_at="t2", db=db,
        )
        accept_thread_input(
            "thread-1", role="user", content="send after switch",
            intent="apply_now", message_id="target-input", db=db,
        )

        settled = finish_turn(
            "source-turn",
            outcome="completed",
            ended_at="t3",
            successor_turn_id="must-not-start",
            boot_epoch="boot-1",
            db=db,
        )

        assert settled.successor is None
        assert settled.switch_request is not None
        assert settled.switch_request.operation_id == "switch-1"
        assert settled.switch_request.destination_model == "native/target"
        target = db.execute(
            "SELECT active_turn_id FROM messages WHERE id='target-input'",
        ).fetchone()
        assert target["active_turn_id"] is None
    finally:
        db.close()
