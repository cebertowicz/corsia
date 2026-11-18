"""Last-viewer contention must not hold lifecycle locks during backoff."""
import asyncio
import fcntl
import os
import time

import pytest

from doceditor.live_lane import WriteActor
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from doceditor.live_text_write import HistoryLockBusy
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_close_flush, document_control


@pytest.mark.asyncio
async def test_busy_close_releases_lifetime_locks_during_backoff(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = object()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    monkeypatch.setattr(document_control, '_subscriptions', {socket: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}})
    monkeypatch.setattr(document_control, '_commits', {})
    monkeypatch.setattr(document_close_flush, 'BUSY_CLOSE_DELAYS', (0.4,))
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    assert entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    attempted = asyncio.Event()

    async def busy(*args):
        attempted.set()
        raise HistoryLockBusy('busy')

    entry.lane.write = busy
    closing = asyncio.create_task(document_control._leave(socket, entry.artifact_id))
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        started = time.monotonic()
        await asyncio.wait_for(entry.lifetime.lock.acquire(), 0.15)
        try:
            await asyncio.wait_for(entry.live.comment_mutex.acquire(), 0.15)
            entry.live.comment_mutex.release()
        finally:
            entry.lifetime.lock.release()
        assert time.monotonic() - started < 0.15
        assert not closing.done()
    finally:
        await asyncio.gather(closing, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['reopen', 'cancel', 'shutdown'])
async def test_busy_close_lifecycle_during_backoff(ownership_scope, tmp_path, monkeypatch, action):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = object()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    subscriptions = {socket: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}}
    monkeypatch.setattr(document_control, '_subscriptions', subscriptions)
    monkeypatch.setattr(document_control, '_commits', {})
    monkeypatch.setattr(document_close_flush, 'BUSY_CLOSE_DELAYS', (0.2,))
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    attempted = asyncio.Event()
    original = entry.lane.write
    calls = 0

    async def busy_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            attempted.set()
            raise HistoryLockBusy('busy')
        return await original(*args)

    entry.lane.write = busy_once
    closing = asyncio.create_task(document_control._leave(socket, entry.artifact_id))
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        # Wait until the attempt has actually released the lifetime lock.
        async with entry.lifetime.lock:
            pass
        if action == 'reopen':
            reopened = await manager.open(entry.artifact_id, entry.live.path)
            assert reopened is entry
            other = object()
            subscriptions[other] = {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}
            await closing
            assert manager.documents[entry.artifact_id] is entry
            assert calls == 1
            await document_control._leave(other, entry.artifact_id)
            assert entry.live.path.read_text().startswith('typed ')
        elif action == 'cancel':
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            async def wait_for_retirement():
                while entry.artifact_id in manager.documents:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_retirement(), 3)
            assert entry.live.path.read_text().startswith('typed ')
        else:
            await manager.close()
            await closing
            assert entry.artifact_id not in manager.documents
            reopened = await manager.open(entry.artifact_id, entry.live.path)
            assert reopened.live.text.startswith('typed ')
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_overlapping_last_viewer_closes_keep_acknowledged_text(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    first, second = object(), object()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    subscriptions = {first: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}}
    monkeypatch.setattr(document_control, '_subscriptions', subscriptions)
    monkeypatch.setattr(document_control, '_commits', {})
    monkeypatch.setattr(document_close_flush, 'BUSY_CLOSE_DELAYS', (0,))
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    attempted, release = asyncio.Event(), asyncio.Event()
    original = entry.lane.write
    calls = 0

    async def busy_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            attempted.set()
            await release.wait()
            raise HistoryLockBusy('busy')
        return await original(*args)

    entry.lane.write = busy_once
    first_close = asyncio.create_task(document_control._leave(first, entry.artifact_id))
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        subscriptions[second] = {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}
        second_close = asyncio.create_task(document_control._leave(second, entry.artifact_id))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first_close, second_close), 3)
        assert entry.artifact_id not in manager.documents
        assert entry.live.path.read_text().startswith('typed ')
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_real_history_contention_uses_bounded_close_wait(ownership_scope, tmp_path, monkeypatch):
    from infra import guidance_git

    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = object()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    monkeypatch.setattr(document_control, '_subscriptions', {socket: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}})
    monkeypatch.setattr(document_control, '_commits', {})
    monkeypatch.setattr(document_control, 'CLOSE_LOCK_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(document_close_flush, 'BUSY_CLOSE_DELAYS', (0, 0, 0, 0))
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    repo, reason = guidance_git.workspace_repository(ownership_scope['workspace'], create=True)
    assert repo is not None, reason
    holder = os.open(guidance_git.git_dir(repo) / 'corsia-guidance.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    waits = []
    original = guidance_git._acquire

    def measured(*args, **kwargs):
        started = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            waits.append((kwargs.get('wait_seconds'), time.monotonic() - started))

    monkeypatch.setattr(guidance_git, '_acquire', measured)
    try:
        await asyncio.wait_for(document_control._leave(socket, entry.artifact_id), 10)
        assert len(waits) == 5
        assert all(limit == 0.05 and duration >= 0.05 for limit, duration in waits)
        assert entry.live.path.read_text() == 'a b\nsecond\n'
    finally:
        os.close(holder)
        await manager.close()
