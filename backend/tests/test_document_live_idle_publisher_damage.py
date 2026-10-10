"""An idle retained publisher must never record a budget into damaged bytes."""
import asyncio
import threading

import pytest

from doceditor.live_publication_record import WriteActor
from tests.test_document_live_damaged_fallback import _corrupt_page
from tests.test_document_live_file_tools import opened
from tests.test_document_live_stopped_publisher import owe_without_drain
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_damage_after_eviction_stops_idle_publisher_without_writing_failure_meta(
        ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    entered, release = threading.Event(), threading.Event()
    publisher = entry.publisher
    original = publisher.store.next_publication
    def held():
        entered.set()
        assert release.wait(3)
        return original()
    try:
        await owe_without_drain(entry, actor)
        monkeypatch.setattr(publisher.store, "next_publication", held)
        publisher.notify()
        assert await asyncio.to_thread(entered.wait, 2)
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert manager.publishers[entry.live.store.path] is publisher
        damaged = _corrupt_page("publish")(entry.live.store.path)
        release.set()
        await publisher.task
        assert entry.live.store.path.read_bytes() == damaged
        assert publisher.closed and publisher.stop_cause is not None
        assert publisher.retry is None
        await asyncio.sleep(0)  # Deliver the completed publisher's lifetime callback.
        assert entry.live.store.path not in manager.publishers
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_unavailable_store_keeps_existing_publication_budget_and_retry(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_store import StoreRefused
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    original = entry.publisher.store.next_publication
    def busy():
        raise StoreRefused("database is locked")
    try:
        await owe_without_drain(entry, actor)
        monkeypatch.setattr(entry.publisher.store, "next_publication", busy)
        entry.publisher.notify()
        await entry.publisher.task
        assert not entry.publisher.closed and entry.publisher.stop_cause is None
        failure = entry.publisher.store.failure("publication_failed")
        assert failure.attempts == 1 and failure.message == "database is locked"
        assert entry.publisher.retry is not None and entry.live.store.owed_count() == 1
        monkeypatch.setattr(entry.publisher.store, "next_publication", original)
        entry.publisher.notify()
        await entry.publisher.task
        assert entry.live.store.owed_count() == 0
        assert entry.publisher.store.failure("publication_failed") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_refused_open_joins_retained_publisher_before_retiring_it(ownership_scope, tmp_path, monkeypatch, cancel):
    from doceditor.live_admission import OpenRefused
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    publisher = entry.publisher
    entered, release = threading.Event(), threading.Event()
    stopped = asyncio.Event()
    original = publisher.store.next_publication
    stop = publisher.stop
    def held():
        entered.set()
        assert release.wait(3)
        return original()
    def observed_stop(cause=None):
        stop(cause)
        if cause is not None:
            stopped.set()
    call = None
    try:
        await owe_without_drain(entry, actor)
        monkeypatch.setattr(publisher.store, "next_publication", held)
        monkeypatch.setattr(publisher, "stop", observed_stop)
        publisher.notify()
        assert await asyncio.to_thread(entered.wait, 2)
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        damaged = _corrupt_page("live")(entry.live.store.path)
        call = asyncio.create_task(manager.open(entry.artifact_id, entry.live.path))
        await asyncio.wait_for(stopped.wait(), 2)
        assert not call.done() and manager.publishers[entry.live.store.path] is publisher
        if cancel:
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert entry.live.store.path not in manager._opening
        release.set()
        if not cancel:
            with pytest.raises(OpenRefused) as refused:
                await call
            assert refused.value.kind == "store_damaged"
        await publisher.task
        await asyncio.sleep(0)
        assert publisher.task.done() and entry.live.store.path not in manager.publishers
        assert entry.live.store.path.read_bytes() == damaged
        assert entry.live.store.owed_count() == 1
    finally:
        release.set()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        await manager.close()
