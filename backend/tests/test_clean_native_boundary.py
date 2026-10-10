"""Native safe-boundary contracts over the clean message FIFO."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_native_boundary_uses_the_durable_turn_model_not_catalog_alias(
    isolated_db, monkeypatch,
) -> None:
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn
    from ws.native_inbox_batch import native_safe_boundary_input
    from shared_send_fixtures import _insert_native_model

    _insert_native_model()
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    accept_thread_input(
        "thread-1", role="user", content="first", intent="apply_now",
        message_id="first", db=db,
    )
    current = start_idle_turn(
        "thread-1", turn_id="turn-1", boot_epoch="boot-1",
        started_at="t1", db=db,
    )
    assert current is not None
    begin_batch_write(current, attempt_id="first-write", started_at="t1", db=db)
    settle_batch_write("first-write", outcome="accepted", db=db)
    accept_thread_input(
        "thread-1", role="user", content="steer now", intent="apply_now",
        message_id="steer", db=db,
    )
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", AsyncMock(),
    )

    candidate = await native_safe_boundary_input(
        thread_id="thread-1",
        thread_row=thread_row,
        owner_turn_id="turn-1",
        max_input_tokens=1000,
    )
    assert candidate is not None and candidate.content == "steer now"
    await candidate.before_provider_write()
    await candidate.settle_delivery()

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT active_turn_id,delivery_state,write_outcome "
            "FROM messages WHERE id='steer'"
        ).fetchone()) == ("turn-1", "delivered", "accepted")
    finally:
        db.close()
