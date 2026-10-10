"""Owner transaction provenance survives delayed completion and missed clears."""
import asyncio
from contextlib import asynccontextmanager
import threading

import pytest

from doceditor.live_lane import Operation
from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control


@asynccontextmanager
async def connected(scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    socket = Socket()
    data = {"type": "doc_open", "path": str(entry.live.path), "project_id": "project-alpha",
            "thread_id": scope["owner"], "request_id": "open"}
    try:
        await document_control.handle_frame(socket, user, data)
        admission = await socket.next("doc_opened")
        yield manager, entry, socket, user, data, admission
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_new_failure_survives_delayed_old_promote_clear(ownership_scope, tmp_path, monkeypatch):
    async with connected(ownership_scope, tmp_path, monkeypatch) as (_, entry, socket, _, _, _):
        for _ in range(3):
            await entry.lane.retry.budget.record(OSError("old fixture failure"))
        old = await socket.next("doc_error")
        # Reset admission's in-memory latch only: the old durable record remains.
        entry.lane.retry.blocked_reason = ""
        original = entry.live.store.promote
        entered, release = threading.Event(), threading.Event()

        def delayed(*args):
            result = original(*args)
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(entry.live.store, "promote", delayed)
        # Admission checks the exhausted old budget; clear only its admission test
        # through an existing replacement failure below exhaustion before starting.
        await asyncio.to_thread(entry.live.store.clear_failure, "store_refused")
        captured = await asyncio.to_thread(entry.live.store.record_failure, "store_refused", "old fixture failure")
        actor = document_control._subscriptions[socket][entry.artifact_id].actor
        call = asyncio.create_task(entry.lane.submit(Operation("flush", actor=actor)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(3):
                await entry.lane.retry.budget.record(OSError("new fixture failure"))
            newer = await socket.next("doc_error")
            release.set()
            assert (await call).server_saved
            cleared = await socket.next("doc_cleared")
            assert old["failure_generation"] < captured.generation == cleared["clears_through"]
            assert newer["failure_generation"] > cleared["clears_through"]
            assert entry.live.store.failure("store_refused").generation == newer["failure_generation"]
        finally:
            release.set()
            await call


@pytest.mark.asyncio
async def test_failed_publisher_clear_emits_no_clear(ownership_scope, tmp_path, monkeypatch):
    async with connected(ownership_scope, tmp_path, monkeypatch) as (_, entry, socket, _, _, _):
        for _ in range(3):
            await entry.publisher.budget.record(OSError("publication fixture failure"))
        await socket.next("doc_error")

        def refused(kind):
            raise OSError("clear fixture refused")

        monkeypatch.setattr(entry.live.store, "clear_failure", refused)
        with pytest.raises(OSError, match="clear fixture refused"):
            await entry.publisher.retry_blocked()
        await asyncio.sleep(0)
        assert socket.sent.empty()
        assert entry.live.store.failure("publication_failed").attempts == 3


@pytest.mark.asyncio
async def test_reopen_explicit_absence_after_unsubscribed_clear(ownership_scope, tmp_path, monkeypatch):
    async with connected(ownership_scope, tmp_path, monkeypatch) as (manager, entry, socket, user, data, admission):
        for _ in range(3):
            await entry.publisher.budget.record(OSError("publication fixture failure"))
        old = await socket.next("doc_error")
        manager.setup_changed(entry.live.path, "setup fixture failure")
        setup = await socket.next("doc_error")
        subscriptions = document_control._subscriptions.pop(socket)
        await entry.publisher.retry_blocked()
        manager.setup_changed(entry.live.path, "")
        await asyncio.sleep(0)
        await entry.publisher.task
        assert socket.sent.empty()
        document_control._subscriptions[socket] = subscriptions
        await document_control.handle_frame(socket, user, {**data, "request_id": "reopen"})
        reopened = await socket.next("doc_opened")
        clears = {row["kind"]: row for row in reopened["failure_clears"]}
        assert reopened["store_id"] == admission["store_id"]
        assert clears["publication_failed"]["clears_through"] >= old["failure_generation"]
        assert clears["setup_failed"]["clears_through"] > setup["setup_revision"]
        assert clears["setup_failed"]["setup_incarnation"] == admission["setup_incarnation"]
        assert manager.setup_failures == {}
        assert entry.live.text == entry.live.path.read_text()


@pytest.mark.asyncio
async def test_setup_callbacks_keep_locked_revisions_when_delivery_is_delayed(ownership_scope, tmp_path, monkeypatch):
    async with connected(ownership_scope, tmp_path, monkeypatch) as (manager, entry, socket, _, _, admission):
        queued = []
        original = manager._loop

        class PausedDelivery:
            def call_soon_threadsafe(self, callback, *args):
                queued.append((callback, args))

        monkeypatch.setattr(manager, "_loop", PausedDelivery())
        await asyncio.to_thread(manager.setup_changed, entry.live.path, "first setup fixture")
        first, _ = manager.setup_state(entry.live.path)
        await asyncio.to_thread(manager.setup_changed, entry.live.path, "first setup fixture")
        assert len(queued) == 1
        await asyncio.to_thread(manager.setup_changed, entry.live.path, "")
        assert manager.setup_state(entry.live.path)[0] is None
        await asyncio.to_thread(manager.setup_changed, entry.live.path, "new setup fixture")
        newer, revision = manager.setup_state(entry.live.path)
        monkeypatch.setattr(manager, "_loop", original)
        for index in (2, 1, 0):
            callback, args = queued[index]
            callback(*args)
        error = await socket.next("doc_error")
        cleared = await socket.next("doc_cleared")
        stale = await socket.next("doc_error")
        assert error["setup_revision"] == newer.revision == revision
        assert stale["setup_revision"] == first.revision < cleared["clears_through"] < error["setup_revision"]
        assert error["setup_incarnation"] == cleared["setup_incarnation"] == admission["setup_incarnation"]
        assert manager.setup_state(entry.live.path)[0] == newer
        manager.setup_changed(entry.live.path, "")
        assert manager.setup_failures == {}


def test_failure_snapshot_and_noop_clear_use_existing_committed_counter(tmp_path):
    from doceditor.live_store import LiveStore

    store = LiveStore(tmp_path / "state.sqlite")
    record = store.record_failure("store_refused", "store fixture")
    records, revision = store.failure_snapshot()
    assert records["store_refused"] == record
    assert revision == record.generation
    assert store.clear_failure("publication_failed") == record.generation
    assert store.failure("store_refused") == record
