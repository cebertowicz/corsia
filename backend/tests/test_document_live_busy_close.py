"""A last viewer's busy flush is retried and its durable edits survive eviction."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from doceditor.live_lane import WriteActor
from doceditor.live_store import BrowserMessage, StoreRefused
from doceditor.live_text import clone
from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_close_flush, document_control


def test_history_lock_refusal_is_identified_as_busy(tmp_path, monkeypatch):
    from doceditor import live_text_write

    path = tmp_path / 'doc.md'
    path.write_text('base')
    monkeypatch.setattr(live_text_write, 'history_target', lambda *args: SimpleNamespace(document=True))
    monkeypatch.setattr(live_text_write, 'save_with_history', lambda *args, **kwargs: SimpleNamespace(
        reason='deferred-guidance-lock', line='busy'))
    with pytest.raises(live_text_write.HistoryLockBusy) as refused:
        live_text_write.write_live_text(path, 'changed', 'previous')
    assert refused.value.code == 'history_lock_busy'
    assert path.read_text() == 'base'


@pytest.mark.asyncio
@pytest.mark.parametrize('busy_count', [1, 5])
async def test_last_viewer_busy_flush_retries_and_keeps_acknowledged_text(ownership_scope, tmp_path, monkeypatch, caplog, busy_count):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = object()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    monkeypatch.setattr(document_control, '_subscriptions', {socket: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}})
    monkeypatch.setattr(document_control, '_commits', {})
    monkeypatch.setattr(document_close_flush, 'BUSY_CLOSE_DELAYS', (0, 0, 0, 0))
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    assert entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    original = entry.lane.write
    calls = []

    async def write(*args):
        calls.append(1)
        if len(calls) <= busy_count:
            error = StoreRefused('The document is busy. Try saving again.')
            error.code = 'history_lock_busy'
            raise error
        return await original(*args)

    entry.lane.write = write
    try:
        with caplog.at_level(logging.WARNING):
            await document_control._leave(socket, entry.artifact_id)
        assert len(calls) == (2 if busy_count == 1 else 5)
        if busy_count == 1:
            assert entry.live.path.read_text() == 'typed a b\nsecond\n'
        else:
            assert entry.live.path.read_text() == 'a b\nsecond\n'
            assert any(entry.artifact_id in row.message and row.levelno == logging.WARNING for row in caplog.records)
        reopened = await manager.open(entry.artifact_id, entry.live.path)
        assert reopened.live.text == 'typed a b\nsecond\n'
        if busy_count == 5:
            # Opening alone does not flush the recovered store to the file.
            await asyncio.sleep(0.1)
            assert entry.live.path.read_text() == 'a b\nsecond\n'
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_busy_refusal_reaches_the_flush_client(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_text_write import HistoryLockBusy

    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    socket = Socket()
    actor = WriteActor(ownership_scope['owner'], 'project-alpha')
    monkeypatch.setattr(document_control, '_subscriptions', {socket: {entry.artifact_id: document_control.Subscription(entry, actor, True, 'user')}})
    peer = clone(entry.live.doc.get_update())
    vector = peer.get_state()
    peer['text'].insert(0, 'typed ')
    entry.live.receive(BrowserMessage('typed', peer.get_update(vector)))
    await entry.live.commit_received()
    original = entry.lane.write

    async def busy(*args):
        raise HistoryLockBusy('The document is busy. Try saving again.')

    entry.lane.write = busy
    try:
        await document_control.handle_frame(socket, {'id': 'user'}, {
            'type': 'doc_flush', 'artifact_id': entry.artifact_id,
            'store_id': entry.live.store.store_id, 'request_id': 'flush',
        })
        error = await socket.next('doc_error')
        assert (error['request_id'], error['kind'], error['code']) == ('flush', 'store_refused', 'history_lock_busy')
    finally:
        entry.lane.write = original
        await manager.close()
