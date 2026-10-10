"""Maximum-size writes use the runtime's real policy, publisher and admission under app contention."""
import asyncio
import sqlite3
import time

import pytest

from doceditor.live_document import file_hash
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_document_live_file_tools import opened
from tests.test_document_live_performance import maximum_message
from tests.test_document_live_backpressure import until
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_maximum_runtime_publication_bound_stop_and_refusal_keep_acked_text(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_backpressure
    from doceditor.live_backpressure import OWED_ROW_BOUND, SLOW_DRAIN_MESSAGE
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    actor = WriteActor(scope["owner"], "project-alpha")
    acked = []
    entry.live.acknowledge = acked.extend
    message = maximum_message(entry.live)
    assert entry.live.receive(message)
    await entry.live.commit_received()
    assert message.message_id in acked
    # This is the actual application DB writer: no publication or policy stub.
    lock = sqlite3.connect(entry.policy.db_path)
    lock.execute("BEGIN IMMEDIATE")
    cancel = asyncio.Event()
    waiting = None
    try:
        started = time.perf_counter()
        for _ in range(OWED_ROW_BOUND):
            result = await entry.lane.submit(Operation("flush", actor=actor))
            assert result.server_saved
        # A joined app writer would cost at least its 5s busy timeout on each call.
        assert time.perf_counter() - started < 10
        assert entry.live.store.owed_count() == OWED_ROW_BOUND
        updates, disk = entry.live.store.load_updates(), entry.live.path.read_bytes()
        waiting = asyncio.create_task(entry.lane.submit(Operation("replace", content="NEVER", actor=actor),
                                                       cancel_event=cancel))
        await until(lambda: entry.lane.admission.waiting == 1)
        assert not entry.live.committer_lock.locked()
        stopped = time.perf_counter()
        cancel.set()
        assert (await asyncio.wait_for(waiting, 1)).outcome == "not_started"
        assert time.perf_counter() - stopped < 1
        assert entry.live.path.read_bytes() == disk
        assert entry.live.store.load_updates() == updates
        assert entry.live.store.owed_count() == OWED_ROW_BOUND
        # Deadline seam only: keep the real publisher in its actual app writer wait.
        monkeypatch.setattr(live_backpressure, "DRAIN_WAIT_S", .05)
        refused = await entry.lane.submit(Operation("flush", actor=actor))
        assert refused.outcome == "refused" and refused.refusal.kind == "publication_failed"
        assert str(refused.refusal) == SLOW_DRAIN_MESSAGE
        assert entry.live.store.owed_count() == OWED_ROW_BOUND
        durable = clone(entry.live.store.load_updates()[0])
        for update in entry.live.store.load_updates()[1:]:
            durable.apply_update(update)
        assert str(durable["text"]) == entry.live.text == disk.decode()
    finally:
        cancel.set()
        lock.rollback()
        lock.close()
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        entry.publisher.notify()
        if entry.publisher.task is not None:
            await entry.publisher.task
        await manager.close()
    assert entry.live.store.owed_count() == 0
    assert file_hash(entry.live.path.read_bytes()) == file_hash(disk)
