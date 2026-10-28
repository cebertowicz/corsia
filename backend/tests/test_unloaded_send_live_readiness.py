"""Live readiness and post-acknowledgement local model loads."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_stale_loaded_cache_explicitly_loads_person_and_refuses_background(isolated_db, monkeypatch):
    from infra.thread_execution import execute_claimed_batch
    from llm.resolution import ModelUnavailableError
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded
    from ws import turn

    batch, thread, target, _ = _claimed(monkeypatch)
    target = replace(target, availability_reason=None)
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock())
    mark_model_loaded(monkeypatch, "omlx@srv/m")
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=[]))
    load = AsyncMock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)

    async def run(request, prepared):
        await request.settle_delivery()
        prepared.unbind(prepared.identity)
        return False

    monkeypatch.setattr(turn, "_run_prepared_turn", run)
    monkeypatch.setattr(turn, "run_post_turn_tasks", AsyncMock())
    result = await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    await result.turn_task
    load.assert_awaited_once_with(target)

    bind = AsyncMock()
    monkeypatch.setattr(turn, "persist_turn_start_bound", bind)
    request = SimpleNamespace(load_for=())
    with pytest.raises(ModelUnavailableError):
        await turn._setup_turn("t", "work", [], target=target, turn_id="background", ingress="ws", request=request)
    bind.assert_not_awaited()
    load.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_acknowledged_before_load_finishes(isolated_db, monkeypatch):
    from infra.thread_execution import execute_claimed_batch
    from tests.test_unloaded_send_owned_turn import _claimed
    from ws import turn

    batch, thread, target, _ = _claimed(monkeypatch)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def load(_target):
        started.set()
        await finish.wait()

    async def run(request, prepared):
        await request.settle_delivery()
        prepared.unbind(prepared.identity)
        return False

    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock())
    monkeypatch.setattr(turn, "_run_prepared_turn", run)
    monkeypatch.setattr(turn, "run_post_turn_tasks", AsyncMock())
    send = asyncio.create_task(execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"}))
    await asyncio.wait_for(started.wait(), 5)
    done, _ = await asyncio.wait({send}, timeout=0.1)
    finish.set()
    result = await send
    await result.turn_task
    assert done, "send waited for model load before acknowledging"


@pytest.mark.asyncio
async def test_post_ack_load_failure_records_event_code_and_cause(isolated_db, monkeypatch):
    from infra.database import get_db
    from infra.thread_execution import execute_claimed_batch
    from llm.local_model_load import ModelLoadFailed
    from tests.test_unloaded_send_owned_turn import _claimed
    from ws import turn

    batch, thread, target, provider = _claimed(monkeypatch)
    finish = asyncio.Event()
    started = asyncio.Event()

    async def load(_target):
        started.set()
        await finish.wait()
        raise ModelLoadFailed("no_memory")

    events = Mock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr(turn, "emit_product_event", events)
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock())
    send = asyncio.create_task(execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"}))
    await asyncio.wait_for(started.wait(), 5)
    finish.set()
    result = (await asyncio.gather(send, return_exceptions=True))[0]
    if not isinstance(result, BaseException):
        await asyncio.gather(result.turn_task, return_exceptions=True)
    db = get_db()
    try:
        row = db.execute("SELECT delivery_error, json_extract(metadata, '$.model_load_cause') FROM messages WHERE id='m'").fetchone()
        assert tuple(row) == ("model_load_failed", "no_memory")
    finally:
        db.close()
    assert any(call.args[:2] == ("turn", "errored") for call in events.call_args_list)
    provider.assert_not_awaited()
