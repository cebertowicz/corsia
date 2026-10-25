"""Production refusal, exact commit receipt and shutdown/storage countercases."""
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException

from api import doc_comments
from auth_fixtures import OWNER_TOKEN
from doceditor import comments
from infra import app_loop
from infra.db_async import run_in_db
from tests.test_app_lifecycle_shutdown import _isolate_lifespan
from tests.test_live_comment_creation import hold, invoke, opened, reserve
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


async def post(entry, identifier='refused', **fields):
    app = FastAPI()
    app.include_router(doc_comments.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN},
            json=dict(delivery_policy="immediate", body='countercase', anchor_text='b', from_off=2, to_off=3,
                      client_id=identifier, **{'live_capture': True, **fields}))


@pytest.mark.asyncio
@pytest.mark.parametrize('live_capture', ['true', 1, 'yes'])
async def test_http_live_capture_must_be_a_boolean_and_never_reserves_or_creates(ownership_scope, tmp_path, monkeypatch, live_capture):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        response = await post(entry, live_capture=live_capture)
        assert response.status_code == 422, response.text
        assert 'refused' not in entry.live.ranges
        assert await run_in_db(comments.get_comment, 'refused') is None
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_missing_reservation_and_wrong_quote_bounds_refuse_live_http(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        response = await post(entry)
        assert response.status_code == 409
        # The reserved range covers a different UTF-16 character than the submitted quote.
        reserve(ownership_scope, entry, 'wrong-bounds', 0, 1)
        value = doc_comments.CommentCreate(body='bad bounds', anchor_text='b', from_off=0, to_off=1,
                                          live_capture=True, client_id='wrong-bounds', delivery_policy='immediate')
        with pytest.raises(HTTPException) as refused:
            await doc_comments.create_comment(entry.artifact_id, OWNER_TOKEN, value, BackgroundTasks())
        assert refused.value.status_code == 409
        assert 'wrong-bounds' not in entry.live.ranges
        assert await run_in_db(comments.get_comment, 'wrong-bounds') is None
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['native', 'agent', 'http'])
async def test_exception_after_canonical_commit_retains_reserved_identity(ownership_scope, tmp_path, monkeypatch, kind):
    from doceditor import comment_create_db
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    transaction = comment_create_db.write_transaction
    @contextmanager
    def postcommit_error(*args, **kwargs):
        with transaction(*args, **kwargs) as db:
            yield db
        raise OSError('controlled exception after actual commit')
    monkeypatch.setattr(comment_create_db, 'write_transaction', postcommit_error)
    try:
        if kind == 'native':
            result = await invoke(kind, ownership_scope, entry, 'committed-error')
            assert 'controlled exception' in result
        else:
            with pytest.raises(OSError, match='controlled exception') if kind == 'http' else pytest.raises(HTTPException):
                await invoke(kind, ownership_scope, entry, 'committed-error')
        row = await run_in_db(comments.get_comment, 'committed-error')
        assert row is not None and entry.live.range_text('committed-error') == 'b'
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_eviction_waits_for_exact_create_then_a_later_post_refuses(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    entered, release = hold(monkeypatch, 'eviction-comment')
    caller = asyncio.create_task(invoke('http', ownership_scope, entry, 'eviction-comment'))
    eviction = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        eviction = asyncio.create_task(manager.evict(entry.artifact_id, entry, can_evict=lambda: True))
        await asyncio.sleep(0)
        assert not eviction.done() and entry.live.range_text('eviction-comment') == 'b'
        release.set()
        await caller
        await eviction
        assert entry.artifact_id not in manager.documents
        response = await post(entry, 'after-eviction')
        assert response.status_code == 409
        assert await run_in_db(comments.get_comment, 'after-eviction') is None
    finally:
        release.set()
        await asyncio.gather(*(task for task in (caller, eviction) if task), return_exceptions=True)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_manager_refuses_after_lifetime_wait(ownership_scope, tmp_path, monkeypatch):
    from pathlib import Path
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    lifetime = manager._lifetime(Path(entry.live.path))
    await lifetime.lock.acquire()
    caller = asyncio.create_task(invoke('agent', ownership_scope, entry, 'closed-owner'))
    try:
        # Observe the real owner queued on the lifetime lock before closing it.
        for _ in range(100):
            if lifetime.lock._waiters:
                break
            await asyncio.sleep(.001)
        assert lifetime.lock._waiters
        manager._closed = True
        lifetime.lock.release()
        with pytest.raises(HTTPException) as refused:
            await caller
        assert refused.value.status_code == 409
        assert await run_in_db(comments.get_comment, 'closed-owner') is None
        assert 'closed-owner' not in entry.live.ranges
    finally:
        if lifetime.lock.locked():
            lifetime.lock.release()
        await asyncio.gather(caller, return_exceptions=True)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_shutdown_drains_exact_comment_before_document_and_storage_close(ownership_scope, tmp_path, monkeypatch):
    import app_lifecycle
    import app_shutdown_storage
    _isolate_lifespan(monkeypatch)
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    entered, release = hold(monkeypatch, 'shutdown-comment')
    events = []
    drain_started = asyncio.Event()
    original_drain = app_loop.drain_awaited
    async def drain():
        events.append('drain')
        drain_started.set()
        await original_drain()
        assert await run_in_db(comments.get_comment, 'shutdown-comment') is not None
        events.append('database_done')
    async def close_documents():
        assert app_loop.awaited_count() == 0
        events.append('documents')
        await manager.close()
    monkeypatch.setattr(app_loop, 'drain_awaited', drain)
    monkeypatch.setattr(app_shutdown_storage, 'close_documents', close_documents)
    monkeypatch.setattr('infra.db_async.shutdown', lambda: events.append('db_async'))
    monkeypatch.setattr(app_lifecycle, 'shutdown_pool', lambda: events.append('pool'))
    caller = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(invoke('native', ownership_scope, entry, 'shutdown-comment'))))
    shutdown = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        app_loop.clear()
        events.append('clear')
        shutdown = asyncio.create_task(app_lifecycle._shutdown_runtime_services([None] * 5))
        await drain_started.wait()
        shutdown.cancel()
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert app_loop.awaited_count() == 1 and not shutdown.done()
        assert events == ['clear', 'drain']
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert events == ['clear', 'drain', 'database_done', 'documents', 'db_async', 'pool']
    finally:
        release.set()
        await asyncio.gather(*(task for task in (caller, shutdown) if task), return_exceptions=True)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['http', 'agent', 'native'])
async def test_db_worker_rechecks_access_and_removes_uncommitted_range(ownership_scope, tmp_path, monkeypatch, kind):
    from doceditor import comment_create_db
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    # Transport preflight remains accessible; access is refused at the final DB boundary.
    monkeypatch.setattr(comment_create_db, 'may_access_artifact', lambda *_args, **_kwargs: False)
    try:
        if kind == 'http':
            reserve(ownership_scope, entry, 'access-refused')
            response = await post(entry, 'access-refused')
            assert response.status_code == 404
        elif kind == 'agent':
            with pytest.raises(HTTPException) as refused:
                await invoke(kind, ownership_scope, entry, 'access-refused')
            assert refused.value.status_code == 404
        else:
            result = await invoke(kind, ownership_scope, entry, 'access-refused')
            assert result == 'Error: Artifact not found'
        assert await run_in_db(comments.get_comment, 'access-refused') is None
        assert 'access-refused' not in entry.live.ranges
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_socket_echoes_distinct_request_ids(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_publication_record import WriteActor
    from tests.test_document_live_suggestions import typing
    from tests.test_document_live_transport import Socket
    from ws import document_control
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = Socket()
    user_id = ownership_scope['db'].execute('SELECT user_id FROM threads WHERE id=?', (ownership_scope['owner'],)).fetchone()[0]
    actor = WriteActor(ownership_scope['owner'], 'project-alpha', direct_user=True)
    document_control._subscriptions[socket] = {entry.artifact_id: document_control.Subscription(entry, actor, True, user_id)}
    try:
        typing(entry, 0, insert='😀 ')
        for request_id in ('placement', 'capture'):
            await document_control.handle_frame(socket, {'id': user_id}, dict(type='doc_positions', artifact_id=entry.artifact_id,
                store_id=entry.live.store.store_id, seq=1, request_id=request_id))
            frame = await socket.next('doc_positions')
            assert frame['request_id'] == request_id and frame['seq'] == 1
    finally:
        document_control._subscriptions.pop(socket, None)
        await manager.close()


@pytest.mark.asyncio
async def test_remaining_handoff_count_prevents_storage_teardown(monkeypatch):
    import app_lifecycle
    import app_shutdown_storage
    _isolate_lifespan(monkeypatch)
    storage = []
    async def no_drain():
        return None
    async def close_documents():
        storage.append('documents')
    monkeypatch.setattr(app_loop, 'drain_awaited', no_drain)
    monkeypatch.setattr(app_loop, 'awaited_count', lambda: 1)
    monkeypatch.setattr(app_shutdown_storage, 'close_documents', close_documents)
    monkeypatch.setattr(app_lifecycle, 'shutdown_pool', lambda: storage.append('pool'))
    with pytest.raises(RuntimeError, match="'awaited_handoffs': 1"):
        await app_lifecycle._shutdown_runtime_services([None] * 5)
    assert storage == []
