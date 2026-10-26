"""A downloaded local model that fits is admitted without waiting for a catalog wake."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('supplied_target', [False, True], ids=['resolved', 'supplied'])
async def test_unloaded_input_that_fits_dispatches_without_hold(isolated_db, monkeypatch, supplied_target):
    from infra.database import get_db
    from infra.send_service import send_thread_input
    from llm.local_model_load import Readiness
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    model = 'omlx@loaded-test-server/model'
    seed_model(model, available=1)
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) "
               "VALUES ('loaded-thread','user-1',?,'t0','t0')", (model,))
    db.commit()
    thread = dict(db.execute("SELECT * FROM threads WHERE id='loaded-thread'").fetchone())
    db.close()
    monkeypatch.setattr('llm.local_model_load.readiness', lambda *_a, **_k: Readiness('fits_now'))
    dispatched = AsyncMock(return_value=object())
    monkeypatch.setattr('infra.thread_execution.execute_claimed_batch', dispatched)
    await send_thread_input(
        'loaded-thread', thread, 'hello', user={'id': 'user-1'},
        input_id='loaded-input', native_mode='event', broadcast_input=False,
        target=resolve_execution_target(model) if supplied_target else None,
    )
    dispatched.assert_awaited_once()
    assert [item.message_id for item in dispatched.await_args.args[0].items][-1] == 'loaded-input'


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata', [{'sender_kind': 'eval'}, {'sender_kind': 'agent', 'from_thread': 'parent'}])
async def test_nonperson_input_holds_until_catalog_load_notification(isolated_db, monkeypatch, metadata):
    import asyncio
    from infra import app_loop, thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from llm import network_discovery
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    # A person's send would load it (readiness reads fits_now); other work waits for the catalog's loaded fact.
    batch, _thread, _target, _provider = _claimed(monkeypatch, metadata=metadata)
    monkeypatch.setattr(app_loop, 'submit', lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr(network_discovery, '_LOADED_BY_SERVER', {})
    mark_model_loaded(monkeypatch, 'omlx@srv/m', loaded=False)
    dispatched = AsyncMock(return_value=object())
    monkeypatch.setattr(thread_execution, 'execute_claimed_batch', dispatched)
    load = AsyncMock()
    monkeypatch.setattr('llm.local_model_load.ensure_loaded', load)
    await thread_execution.drive_claimed_batch(batch)
    if recovery._TASK is not None:
        await recovery._TASK
    assert message_row('m') == ('queued', 'proved_no_write', 'selected_model_unavailable', None)
    db = get_db()
    revision = db.execute("SELECT delivery_revision FROM messages WHERE id='m'").fetchone()[0]
    db.close()
    network_discovery.notify_cached_model_loaded('srv', 'other')
    await asyncio.sleep(0)
    if recovery._TASK is not None:
        await recovery._TASK
    dispatched.assert_not_awaited()
    db = get_db()
    assert db.execute("SELECT delivery_revision FROM messages WHERE id='m'").fetchone()[0] == revision
    db.close()
    network_discovery.notify_cached_model_loaded('srv', 'm')
    await asyncio.sleep(0)
    if recovery._TASK is not None:
        await recovery._TASK
    dispatched.assert_awaited_once()
    load.assert_not_awaited()
