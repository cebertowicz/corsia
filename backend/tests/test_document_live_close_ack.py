"""Retirement may drop only unacknowledged updates, which the client resends."""
import asyncio
import logging
import threading

import pytest

from doceditor.live_document import LiveDocument
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import BrowserMessage, StoreRefused
from doceditor.live_text import clone
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["leave", "shutdown"])
@pytest.mark.parametrize("deletion", [False, True])
async def test_close_refused_batch_never_acks_dropped_updates(ownership_scope, tmp_path, monkeypatch, caplog, closing, deletion):
    from ws import document_control as control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    live = entry.live
    client = clone(live.doc.get_update())
    before = client.get_state()
    client["text"].insert(0, "ACKED ")
    first = BrowserMessage("durable-before-close", client.get_update(before))
    acks = []
    def acknowledged(ids):
        stored = clone(live.store.load_updates()[0])
        for update in live.store.load_updates()[1:]:
            stored.apply_update(update)
        assert str(stored["text"]) == str(live.durable["text"])
        acks.extend(ids)
    live.acknowledge = acknowledged
    assert live.receive(first)
    assert not acks
    await live.commit_received()
    assert acks == [first.message_id]
    durable_text = live.text
    before = client.get_state()
    if deletion:
        del client["text"][0:6]
    else:
        client["text"].insert(0, "UNACKED ")
    second = BrowserMessage("uncommitted-at-close", client.get_update(before))
    assert live.receive(second)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_commit = live.store.commit_batch
    def refused(messages):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        raise StoreRefused("close batch refused")
    monkeypatch.setattr(live.store, "commit_batch", refused)
    socket = object()
    actor = WriteActor(scope["owner"], "project-alpha")
    control._subscriptions[socket] = {entry.artifact_id: control.Subscription(entry, actor, True, "user")}
    caplog.set_level(logging.WARNING, logger="doceditor.live_runtime")
    task = asyncio.create_task(control._leave(socket, entry.artifact_id) if closing == "leave" else manager.close())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert acks == [first.message_id], "a held store commit must not acknowledge the batch"
        release.set()
        await task
        assert acks == [first.message_id], "a refused store commit must not acknowledge dropped bytes"
        assert entry.artifact_id not in manager.documents
        reopened = await LiveDocument.open(live.path, live.store.path)
        try:
            assert reopened.text == durable_text, "already acknowledged data must survive retirement"
            assert reopened.receive(second), "the browser retains and resends the unacknowledged id/bytes"
            resent_acks = []
            reopened.acknowledge = lambda ids: resent_acks.extend(ids)
            await reopened.commit_received()
            assert resent_acks == [second.message_id]
            assert reopened.text == str(client["text"])
            assert (await reopened.commit_received()) == ()
        finally:
            reopened.close()
        errors = [record for record in caplog.records if "close batch refused" in record.message]
        assert len(errors) == 1 and errors[0].levelno == logging.ERROR
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        control._subscriptions.pop(socket, None)
        monkeypatch.setattr(live.store, "commit_batch", real_commit)
        await manager.close()
