"""Native batching at the post-tool provider boundary."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, call

import pytest


def _seed(thread_id: str):
    from shared_send_fixtures import _insert_native_model
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    _insert_native_model()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (thread_id, "u-test", "native/test", "t0", "t0"),
        )
        db.commit()
        accept_thread_input(
            thread_id,
            role="user",
            content="running",
            intent="apply_now",
            message_id=f"initial-{thread_id}",
            db=db,
        )
        batch = start_idle_turn(
            thread_id,
            turn_id=f"turn-{thread_id}",
            boot_epoch="boot-1",
            started_at="t1",
            db=db,
        )
        assert batch is not None
        begin_batch_write(
            batch,
            attempt_id=f"write-{thread_id}",
            started_at="t1",
            db=db,
        )
        settle_batch_write(f"write-{thread_id}", outcome="accepted", db=db)
        return batch.identity
    finally:
        db.close()


def _enqueue(
    thread_id: str,
    message_id: str,
    content: str,
    *,
    intent: str = "apply_now",
):
    from infra.input_admission import accept_thread_input

    return accept_thread_input(
        thread_id,
        role="user",
        content=content,
        intent=intent,
        message_id=message_id,
        created_at=message_id,
    )


def _thread_row(thread_id: str) -> dict:
    return {"id": thread_id, "user_id": "u-test", "model": "native/test"}


@pytest.mark.asyncio
async def test_native_send_claims_earlier_queue_in_one_atomic_batch(
    isolated_db,
    monkeypatch,
) -> None:
    from infra.database import get_db
    from ws.native_inbox_batch import native_safe_boundary_input

    thread_id = "native-two-row-batch"
    identity = _seed(thread_id)
    first = _enqueue(
        thread_id,
        "native-batch-1",
        "first correction",
        intent="queue_after_turn",
    )
    second = _enqueue(thread_id, "native-batch-2", "second correction")
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows",
        broadcast,
    )

    candidate = await native_safe_boundary_input(
        thread_id=thread_id,
        thread_row=_thread_row(thread_id),
        owner_turn_id=identity.turn_id,
        max_input_tokens=100_000,
    )

    assert candidate is not None
    assert candidate.content.index("first correction") < candidate.content.index(
        "second correction"
    )
    assert candidate.input_identity.startswith("input-projection:")
    await candidate.before_provider_write()
    await candidate.settle_delivery()

    db = get_db()
    try:
        rows = db.execute(
            "SELECT id,active_turn_id,delivery_state,write_outcome,seq "
            "FROM messages WHERE id IN (?,?) ORDER BY input_order",
            (first.message_id, second.message_id),
        ).fetchall()
    finally:
        db.close()
    assert [tuple(row) for row in rows] == [
        (first.message_id, identity.turn_id, "delivered", "accepted", 2),
        (second.message_id, identity.turn_id, "delivered", "accepted", 3),
    ]
    assert broadcast.await_args_list == [
        call(thread_id, (first.message_id, second.message_id)),
        call(thread_id, (first.message_id, second.message_id)),
    ]


@pytest.mark.asyncio
async def test_native_safe_boundary_leaves_explicit_queue_unclaimed(
    isolated_db, caplog,
) -> None:
    from infra.database import get_db
    from ws.native_inbox_batch import native_safe_boundary_input

    thread_id = "native-explicit-queue-gate"
    identity = _seed(thread_id)
    receipt = _enqueue(
        thread_id,
        "native-queue-head",
        "wait until the current Turn ends",
        intent="queue_after_turn",
    )

    with caplog.at_level(logging.DEBUG, logger="infra.thread_inputs"):
        candidate = await native_safe_boundary_input(
            thread_id=thread_id,
            thread_row=_thread_row(thread_id),
            owner_turn_id=identity.turn_id,
            max_input_tokens=100_000,
        )

    assert candidate is None
    assert f"thread_id={thread_id}" in caplog.text
    assert f"turn_id={identity.turn_id}" in caplog.text
    assert "reason=no_apply_now_row" in caplog.text
    db = get_db()
    try:
        row = db.execute(
            "SELECT active_turn_id,delivery_state,delivery_attempt_id "
            "FROM messages WHERE id=?",
            (receipt.message_id,),
        ).fetchone()
    finally:
        db.close()
    assert tuple(row) == (None, "queued", None)
