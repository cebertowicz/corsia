"""Unit shutdown owns its lifespan tasks and restores the session app unchanged."""
import asyncio
from unittest.mock import Mock

import pytest

from tests.test_vendor_account_controls import isolated_shutdown_tasks


@pytest.fixture
def session_task_snapshot(integration_client):
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from infra import state

    originals = {attr: getattr(state, attr, None) for _, attr in RUNTIME_LOOP_TASKS}
    originals['_github_ownership_login_task'] = getattr(state, '_github_ownership_login_task', None)
    suspended = [task for task in originals.values() if task is not None
                 and task.get_coro().__name__ == '_suspend_forever']
    assert len(suspended) == 6
    assert all(not task.done() and not task.cancelling() and task.get_loop().is_running()
               for task in suspended)
    with pytest.MonkeyPatch.context() as owned_lifespan:
        isolated_shutdown_tasks.__wrapped__(owned_lifespan)
        yield originals, owned_lifespan
    # The actual fixture has only monkeypatch state; its complete undo ran above.
    assert {attr: getattr(state, attr, None) for attr in originals} == originals
    assert all(not task.done() and not task.cancelling() and task.get_loop().is_running()
               for task in suspended)


@pytest.mark.asyncio
async def test_scoped_shutdown_joins_own_task_then_restores_session_lifecycle(
    session_task_snapshot,
):
    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from connections import account_controls
    from infra import state

    originals, monkeypatch = session_task_snapshot
    closed = []
    entered = asyncio.Event()

    async def owned():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.append('joined')

    async def no_signins():
        pass

    monkeypatch.setattr(account_controls, 'stop_all_signins', no_signins)
    task = asyncio.create_task(owned())
    await entered.wait()
    monkeypatch.setattr(state, '_github_ownership_login_task', task)
    assert task.get_loop() is asyncio.get_running_loop()
    assert await shutdown_owned_tasks([None] * 5, Mock()) is None
    assert task.cancelled() and closed == ['joined']
    assert all(not original.cancelling() for original in originals.values()
               if original is not None)


@pytest.mark.asyncio
async def test_foreign_task_still_refuses_join_and_is_cleaned_on_its_owner_loop(monkeypatch):
    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from connections import account_controls
    from infra import state

    isolated_shutdown_tasks.__wrapped__(monkeypatch)
    foreign = asyncio.new_event_loop()
    errors = []
    foreign.set_exception_handler(lambda _loop, context: errors.append(context))

    async def held():
        await asyncio.Event().wait()

    async def no_signins():
        pass

    monkeypatch.setattr(account_controls, 'stop_all_signins', no_signins)
    task = foreign.create_task(held())
    monkeypatch.setattr(state, '_idle_recovery_task', task)
    try:
        with pytest.raises(RuntimeError, match='attached to a different loop'):
            await shutdown_owned_tasks([None] * 5, Mock())
        assert task.cancelling() == 1
    finally:
        async def drain_owner():
            tasks = asyncio.all_tasks(foreign) - {asyncio.current_task()}
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            assert not asyncio.all_tasks(foreign) - {asyncio.current_task()}
        await asyncio.to_thread(foreign.run_until_complete, drain_owner())
        assert task.cancelled()
        foreign.close()
    assert errors == []
