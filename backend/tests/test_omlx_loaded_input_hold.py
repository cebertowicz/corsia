"""Disk availability cannot release an input the oMLX provider cannot run."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('supplied_target', [False, True], ids=['resolved', 'supplied'])
async def test_unloaded_input_holds_until_catalog_load_notification(isolated_db, monkeypatch, supplied_target):
    from infra import app_loop, thread_execution
    from infra import unavailable_model_recovery as recovery
    from infra.send_types import InboxQueuedInput
    from infra.send_service import send_thread_input
    from llm.resolution import resolve_execution_target
    from infra.send_target import resolve_target
    from llm import model_status, network_discovery
    from tests.unavailable_model_support import message_row, seed_model

    import asyncio
    model = 'omlx@loaded-test-server/model'
    seed_model(model, available=1)
    from infra.database import get_db
    db = get_db()
    db.execute("INSERT INTO network_servers (id,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at) "
               "VALUES ('loaded-test-server','loaded-test-server','Test',8210,'online','t0','t0','t0')")
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) "
               "VALUES ('loaded-thread','user-1',?,'t0','t0')", (model,))
    db.commit()
    thread = dict(db.execute("SELECT * FROM threads WHERE id='loaded-thread'").fetchone())
    db.close()
    monkeypatch.setattr(network_discovery, '_LOADED_BY_SERVER', {})
    monkeypatch.setattr(model_status, '_network_server_status', lambda *_args: {'loaded-test-server': 'online'})
    monkeypatch.setattr(model_status, '_loaded_model_ids_fn', lambda: network_discovery.get_loaded_model_ids)
    monkeypatch.setattr(app_loop, 'submit', lambda coro: asyncio.create_task(coro))
    driven = AsyncMock()
    monkeypatch.setattr(thread_execution, 'execute_claimed_batch', driven)
    result = await send_thread_input(
        'loaded-thread', thread, 'hello', user={'id': 'user-1'},
        input_id='loaded-input', native_mode='event', broadcast_input=False,
        target=resolve_execution_target(model) if supplied_target else None,
    )
    try:
        assert isinstance(result, InboxQueuedInput)
        assert message_row('loaded-input') == ('queued', 'proved_no_write', 'selected_model_unavailable', None)
        held_target = await resolve_target(model)
        assert held_target.availability_reason == 'model_not_loaded'
        error = recovery._held_error('loaded-thread', held_target)
        assert model in error and 'model_not_loaded' in error
        await recovery._resume_held_heads()
        driven.assert_not_awaited()
        network_discovery.notify_cached_model_loaded('loaded-test-server', 'model')
        # Join the existing coalescing mechanism; no provider or timer is involved.
        await asyncio.sleep(0)
        if recovery._TASK is not None:
            await recovery._TASK
        await recovery._resume_held_heads()
        network_discovery.notify_cached_model_loaded('loaded-test-server', 'model')
        await asyncio.sleep(0)
        if recovery._TASK is not None:
            await recovery._TASK
        driven.assert_awaited_once()
        claimed = driven.await_args.args[0]
        assert [item.message_id for item in claimed.items][-1] == 'loaded-input'
    finally:
        if recovery._TASK is not None:
            await recovery._TASK
