"""Real two-loop completion and cancellation, without a DB-worker callback."""
import asyncio
import inspect
import threading

import pytest

from infra import app_loop
from infra.db_async import run_in_db


@pytest.fixture
async def owner():
    ready = threading.Event()
    loop = asyncio.new_event_loop()
    def run():
        asyncio.set_event_loop(loop)
        app_loop.capture(loop)
        ready.set()
        loop.run_forever()
        loop.close()
    thread = threading.Thread(target=run)
    thread.start()
    await asyncio.to_thread(ready.wait)
    yield loop
    app_loop.clear()
    drained = asyncio.run_coroutine_threadsafe(app_loop.drain_awaited(), loop)
    await asyncio.wrap_future(drained)
    loop.call_soon_threadsafe(loop.stop)
    await asyncio.to_thread(thread.join)
    assert app_loop.awaited_count() == 0


@pytest.mark.asyncio
async def test_result_runs_on_actual_owner_and_db_worker(owner):
    caller = asyncio.get_running_loop()
    async def work():
        actual = asyncio.get_running_loop()
        worker = await run_in_db(threading.get_ident)
        return actual, threading.get_ident(), worker
    actual, owner_thread, worker_thread = await app_loop.await_on_owner(work(), owner=owner)
    assert actual is owner and actual is not caller
    assert owner_thread != threading.get_ident() and worker_thread != owner_thread


@pytest.mark.asyncio
async def test_missing_mismatched_and_closed_owner_close_coroutine(owner):
    async def work():
        pytest.fail('unavailable owner ran work')
    for target in (None, asyncio.get_running_loop()):
        coro = work()
        with pytest.raises(app_loop.AppLoopUnavailable):
            await app_loop.await_on_owner(coro, owner=target)
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    closed = asyncio.new_event_loop()
    closed.close()
    coro = work()
    with pytest.raises(app_loop.AppLoopUnavailable):
        await app_loop.await_on_owner(coro, owner=closed)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_repeated_started_cancellation_joins_actual_db_worker(owner):
    entered, release = threading.Event(), threading.Event()
    finished = []
    def database_work():
        entered.set()
        assert release.wait(5)
        finished.append('committed')
    async def work():
        try:
            await run_in_db(database_work)
        finally:
            finished.append('owner_done')
    task = asyncio.create_task(app_loop.await_on_owner(work(), owner=owner))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and app_loop.awaited_count() == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ['committed', 'owner_done']
    assert app_loop.awaited_count() == 0


@pytest.mark.asyncio
async def test_clear_refuses_queued_work_and_settles_before_callback(owner):
    blocked, release = threading.Event(), threading.Event()
    def block():
        blocked.set()
        assert release.wait(5)
    owner.call_soon_threadsafe(block)
    assert await asyncio.to_thread(blocked.wait, 5)
    ran = []
    async def work():
        ran.append(True)
    coro = work()
    task = asyncio.create_task(app_loop.await_on_owner(coro, owner=owner))
    await asyncio.sleep(0)
    assert app_loop.awaited_count() == 1
    app_loop.clear()
    with pytest.raises(app_loop.AppLoopUnavailable):
        await task
    release.set()
    assert ran == [] and inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_queued_cancel_never_starts(owner):
    blocked, release = threading.Event(), threading.Event()
    def block():
        blocked.set()
        assert release.wait(5)
    owner.call_soon_threadsafe(block)
    assert await asyncio.to_thread(blocked.wait, 5)
    ran = []
    async def work():
        ran.append(True)
    task = asyncio.create_task(app_loop.await_on_owner(work(), owner=owner))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert ran == [] and app_loop.awaited_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
async def test_cancelled_drain_keeps_receipt_until_actual_two_loop_db_completion(owner, wrapped):
    from app_shutdown_join import join_cleanup_owner

    entered, release = threading.Event(), threading.Event()
    finished = []
    def database_work():
        entered.set()
        assert release.wait(5)
        finished.append('database_done')
    async def work():
        try:
            await run_in_db(database_work)
        finally:
            finished.append('owner_done')
    caller = asyncio.create_task(app_loop.await_on_owner(work(), owner=owner))
    assert await asyncio.to_thread(entered.wait, 5)
    receipt = next(iter(app_loop._AWAITED))
    app_loop.clear()
    awaitable = app_loop.drain_awaited()
    drain = asyncio.create_task(join_cleanup_owner(awaitable) if wrapped else awaitable)
    try:
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        assert not receipt.result.done(), 'cleanup cancellation settled the completion receipt'
        assert not caller.done() and not drain.done()
        assert app_loop.awaited_count() == 1
        release.set()
        if wrapped:
            result, cancellation = await drain
            assert result is None and isinstance(cancellation, asyncio.CancelledError)
        else:
            with pytest.raises(asyncio.CancelledError):
                await drain
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert finished == ['database_done', 'owner_done']
        assert receipt.result.done() and not receipt.result.cancelled()
        assert app_loop.awaited_count() == 0
    finally:
        release.set()
        await asyncio.gather(caller, drain, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_loop_result_uses_same_joined_contract():
    loop = asyncio.get_running_loop()
    app_loop.capture(loop)
    async def work():
        return asyncio.get_running_loop()
    assert await app_loop.await_on_owner(work(), owner=loop) is loop
    assert app_loop.awaited_count() == 0
    app_loop.clear()
