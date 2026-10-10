"""Contract tests for the clean Turn & Delivery state model."""

from __future__ import annotations

import logging
import sqlite3

import pytest


def _fresh_db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}


def _seed_thread(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/opus',7,'t0','t0')"
    )


def test_messages_are_the_only_durable_input_queue() -> None:
    """A queued input cannot drift across duplicate delivery records.

    Removing any one of the three exclusions below would recreate a second
    lifecycle for an input that is already represented by ``messages``.
    """
    db = _fresh_db()
    try:
        tables = {
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        duplicate_owners = {
            "thread_inbox",
            "message_delivery_contracts",
            "thread_parent_completions",
        }
        assert tables.isdisjoint(duplicate_owners), (
            f"legacy delivery tables remain: {sorted(tables & duplicate_owners)}"
        )

        assert {
            "input_order",
            "delivery_intent",
            "target_execution_epoch",
            "target_model",
            "delivery_attempt_id",
            "write_started_at",
            "write_outcome",
            "active_turn_id",
            "next_attempt_at",
            "delivery_options",
        } <= _columns(db, "messages")
        assert {
            "delivering_reason",
            "delivery_blocked_since",
        }.isdisjoint(_columns(db, "messages"))
        assert {
            "next_input_order",
            "execution_epoch",
        } <= _columns(db, "threads")
    finally:
        db.close()


def test_admission_assigns_fifo_order_and_freezes_execution_epoch() -> None:
    """Concurrent control changes cannot retarget an already accepted input."""
    from infra.input_admission import accept_thread_input

    db = _fresh_db()
    try:
        _seed_thread(db)
        queued = accept_thread_input(
            "thread-1",
            role="user",
            content="wait for the current answer",
            intent="queue_after_turn",
            message_id="input-1",
            created_at="t1",
            db=db,
        )
        send = accept_thread_input(
            "thread-1",
            role="user",
            content="apply this at the next boundary",
            intent="apply_now",
            message_id="input-2",
            created_at="t2",
            db=db,
        )

        assert (queued.input_order, send.input_order) == (1, 2)
        assert (queued.target_execution_epoch, send.target_execution_epoch) == (7, 7)
        assert (queued.target_model, send.target_model) == (
            "claude-code/opus",
            "claude-code/opus",
        )
        assert db.execute(
            "SELECT next_input_order FROM threads WHERE id='thread-1'"
        ).fetchone()[0] == 3
        rows = db.execute(
            "SELECT id,input_order,delivery_intent,delivery_state,seq "
            "FROM messages ORDER BY input_order"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("input-1", 1, "queue_after_turn", "queued", None),
            ("input-2", 2, "apply_now", "queued", None),
        ]
    finally:
        db.close()


def test_admission_idempotency_never_creates_or_retargets_a_second_input() -> None:
    """A retried client identity returns its first immutable FIFO admission."""
    from infra.input_admission import accept_thread_input

    db = _fresh_db()
    try:
        _seed_thread(db)
        first = accept_thread_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", metadata={"source": "ws"}, db=db,
        )
        duplicate = accept_thread_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", metadata={"source": "ws"}, db=db,
        )
        assert first.duplicate is False
        assert duplicate.duplicate is True
        assert duplicate.input_order == first.input_order
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='thread-1'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT next_input_order FROM threads WHERE id='thread-1'"
        ).fetchone()[0] == 2

        with pytest.raises(ValueError, match="different payload"):
            accept_thread_input(
                "thread-1", role="user", content="changed", intent="apply_now",
                message_id="input-1", metadata={"source": "ws"}, db=db,
            )
    finally:
        db.close()


def test_idle_admission_creates_one_turn_and_claims_the_fifo() -> None:
    """An idle task has one atomic admission winner and no ownerless head."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _fresh_db()
    try:
        _seed_thread(db)
        for index, intent in enumerate(
            ("queue_after_turn", "apply_now", "queue_after_turn"),
            start=1,
        ):
            accept_thread_input(
                "thread-1",
                role="user",
                content=f"input {index}",
                intent=intent,
                message_id=f"input-{index}",
                created_at=f"t{index}",
                metadata={"member": index},
                delivery_options={"native_mode": "ws"},
                db=db,
            )

        db.commit()
        batch = start_idle_turn(
            "thread-1",
            turn_id="turn-1",
            boot_epoch="boot-1",
            started_at="t4",
            db=db,
        )
        assert batch is not None
        assert [item.message_id for item in batch.items] == [
            "input-1",
            "input-2",
            "input-3",
        ]
        assert [item.metadata for item in batch.items] == [
            {"member": 1},
            {"member": 2},
            {"member": 3},
        ]
        assert [item.delivery_options for item in batch.items] == [
            {"native_mode": "ws"},
            {"native_mode": "ws"},
            {"native_mode": "ws"},
        ]
        assert batch.turn_order == 1
        assert start_idle_turn(
            "thread-1",
            turn_id="turn-2",
            boot_epoch="boot-1",
            started_at="t5",
            db=db,
        ) is None
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT id,status FROM active_turns WHERE thread_id='thread-1'"
            )
        ] == [("turn-1", "running")]
        assert {
            str(row[0])
            for row in db.execute(
                "SELECT active_turn_id FROM messages WHERE input_order IS NOT NULL"
            )
        } == {"turn-1"}
    finally:
        db.close()


def test_idle_passive_inputs_wait_for_independent_activity() -> None:
    """A digest cannot wake a task merely because the task is idle."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _fresh_db()
    try:
        _seed_thread(db)
        accept_thread_input(
            "thread-1",
            role="system",
            content="passive context",
            intent="queue_after_turn",
            message_id="digest-1",
            created_at="t1",
            metadata={"type": "context_event", "event_disposition": "passive"},
            db=db,
        )

        assert start_idle_turn(
            "thread-1",
            turn_id="turn-1",
            boot_epoch="boot-1",
            started_at="t2",
            db=db,
        ) is None
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns"
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_safe_boundary_claims_through_the_last_send_without_reordering_queue() -> None:
    """A later Send opens one boundary through earlier Queue inputs only."""
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import (
        claim_safe_boundary,
        start_idle_turn,
    )

    db = _fresh_db()
    try:
        _seed_thread(db)
        accept_thread_input(
            "thread-1",
            role="user",
            content="active input",
            intent="apply_now",
            message_id="input-a",
            db=db,
        )
        db.commit()
        start_idle_turn(
            "thread-1",
            turn_id="turn-1",
            boot_epoch="boot-1",
            started_at="t1",
            db=db,
        )
        db.execute(
            "UPDATE messages SET delivery_state='delivered',seq=1 "
            "WHERE id='input-a'"
        )
        db.execute(
            "UPDATE threads SET next_message_seq=2 WHERE id='thread-1'"
        )

        for message_id, intent in (
            ("input-b", "queue_after_turn"),
            ("input-c", "apply_now"),
            ("input-d", "queue_after_turn"),
        ):
            accept_thread_input(
                "thread-1",
                role="user",
                content=message_id,
                intent=intent,
                message_id=message_id,
                db=db,
            )

        batch = claim_safe_boundary("thread-1", "turn-1", db=db)
        assert batch is not None
        assert [item.message_id for item in batch.items] == ["input-b", "input-c"]
        assert db.execute(
            "SELECT active_turn_id FROM messages WHERE id='input-d'"
        ).fetchone()[0] is None
        assert claim_safe_boundary("thread-1", "turn-1", db=db) is None
    finally:
        db.close()


def test_safe_boundary_logs_no_running_turn_without_claiming(caplog) -> None:
    """The canonical transaction logs a missing running owner without writes."""
    from infra.thread_inputs import claim_safe_boundary

    db = _fresh_db()
    try:
        _seed_thread(db)
        with caplog.at_level(logging.DEBUG, logger="infra.thread_inputs"):
            assert claim_safe_boundary("thread-1", "missing-turn", db=db) is None
        assert "thread_id=thread-1" in caplog.text
        assert "turn_id=missing-turn" in caplog.text
        assert "reason=no_running_turn" in caplog.text
        assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    finally:
        db.close()


def test_unwritten_safe_boundary_batch_returns_to_fifo() -> None:
    """A boundary that cannot make the next provider call releases its exact claim."""
    from infra.input_admission import accept_thread_input
    from infra.input_writes import release_batch_before_write
    from infra.thread_inputs import (
        claim_safe_boundary,
        start_idle_turn,
    )

    db = _fresh_db()
    try:
        _seed_thread(db)
        accept_thread_input(
            "thread-1", role="user", content="active", intent="apply_now",
            message_id="active", db=db,
        )
        db.commit()
        current = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        )
        assert current is not None
        db.execute(
            "UPDATE messages SET delivery_state='delivered',seq=1 WHERE id='active'"
        )
        db.execute("UPDATE threads SET next_message_seq=2 WHERE id='thread-1'")
        accept_thread_input(
            "thread-1", role="user", content="steer", intent="apply_now",
            message_id="steer", db=db,
        )
        batch = claim_safe_boundary("thread-1", "turn-1", db=db)
        assert batch is not None

        release_batch_before_write(batch, db=db)

        assert tuple(db.execute(
            "SELECT delivery_state,active_turn_id,write_started_at "
            "FROM messages WHERE id='steer'"
        ).fetchone()) == ("queued", None, None)
    finally:
        db.close()


def test_proved_no_write_safe_boundary_batch_returns_to_fifo() -> None:
    """A provider refusal before bytes were written can retry without duplication."""
    from infra.input_writes import (
        begin_batch_write,
        release_batch_after_proved_no_write,
    )
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _fresh_db()
    try:
        _seed_thread(db)
        accept_thread_input(
            "thread-1", role="user", content="retry", intent="apply_now",
            message_id="retry", db=db,
        )
        db.commit()
        batch = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        )
        assert batch is not None
        begin_batch_write(batch, attempt_id="attempt", started_at="t2", db=db)

        release_batch_after_proved_no_write(
            batch, attempt_id="attempt", error="provider unavailable", db=db,
        )

        assert tuple(db.execute(
            "SELECT delivery_state,active_turn_id,delivery_attempt_id,"
            "write_started_at,write_outcome,delivery_error FROM messages"
        ).fetchone()) == ("queued", None, None, None, None, "provider unavailable")
    finally:
        db.close()
