"""A Stop receipt describes its terminal, independently of successor work."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from test_ownerless_stop import _stop
from test_stop_receipts import _active_turn, _clear_live_turn_state  # noqa: F401


@pytest.mark.parametrize("surface", ["main", "delegated"])
@pytest.mark.parametrize("effect", ["blocked", "failed"])
@pytest.mark.asyncio
async def test_ownerless_stop_receipt_does_not_depend_on_successor(
    main_db, monkeypatch, surface, effect,
):
    """Awaiting successor dispatch in Stop delays or falsifies its receipt."""
    import ws
    from infra import state, thread_execution
    from infra.input_admission import accept_thread_input
    from ws.ingress import background_tasks
    from ws.subagent_control import handle_subagent_stop

    identity, _event = _active_turn(main_db, "orphan")
    state._active_turns.pop("main-1")
    accept_thread_input(
        "main-1", role="user", content="next", intent="queue_after_turn",
        message_id="next", db=main_db,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    frames, errors, batches = [], [], []

    async def dispatch(batch):
        batches.append(batch)
        entered.set()
        if effect == "failed":
            raise RuntimeError("successor failure")
        await release.wait()

    async def send(_ws, frame):
        frames.append(frame)

    monkeypatch.setattr(thread_execution, "drive_claimed_batch", dispatch)
    monkeypatch.setattr(state, "send_to_client", send)
    sink = logger.add(lambda message: errors.append(message.record), level="ERROR")
    owner_id = main_db.execute(
        "SELECT user_id FROM threads WHERE id='main-1'",
    ).fetchone()[0]
    if surface == "main":
        action = ws._send_stop_receipt(
            object(), "main-1", _stop(main_db, identity.turn_id), identity.turn_id,
        )
    else:
        action = handle_subagent_stop(
            object(), {"id": owner_id}, "main-1", identity.turn_id,
            owned_thread_fn=AsyncMock(return_value=True),
        )
    before = set(background_tasks)
    task = asyncio.create_task(action)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        done, _pending = await asyncio.wait({task}, timeout=1)
        assert task in done, "Stop waited for the next Turn's dispatch"
        await task
        assert frames == [{
            "type": "stop_receipt", "thread_id": "main-1",
            "turn_id": identity.turn_id, "outcome": "stopped",
        }]
        assert len(batches) == 1
        assert [tuple(row) for row in main_db.execute(
            "SELECT id,status FROM active_turns ORDER BY turn_order",
        )] == [("orphan", "cancelled"), (batches[0].turn_id, "running")]
        if effect == "failed":
            assert any(
                record["exception"] is not None
                and "successor failure" in str(record["exception"].value)
                for record in errors
            )
        else:
            assert len(background_tasks - before) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*(background_tasks - before), return_exceptions=True)
        logger.remove(sink)
