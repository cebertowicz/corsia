"""Per-document lifetime ownership under stalls, store failures and caller cancellation."""
import asyncio
import sqlite3

import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from tests.test_document_live_file_tools import opened
from tests.test_document_live_legacy_save import old_client, save_old
from tests.test_document_live_runtime import runtime
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
async def test_stalled_eviction_does_not_hold_unrelated_open_or_legacy_save(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    actor = WriteActor(scope["owner"], "project-alpha")
    _write(scope, "other.md", "other")
    other_path = scope["category"] / "other.md"
    other_id = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(other_path),)).fetchone()[0]
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    call = asyncio.create_task(entry.lane.submit(Operation("replace", content="late", actor=actor)))
    await entered.wait()
    evict = asyncio.create_task(manager.evict(entry.artifact_id, entry, can_evict=lambda: True))
    while not entry.lane.closed:
        await asyncio.sleep(0)
    reopen = asyncio.create_task(manager.open(entry.artifact_id, entry.live.path))
    other = asyncio.create_task(manager.open(other_id, other_path))
    try:
        done, _ = await asyncio.wait({other}, timeout=.1)
        assert other in done, "one path's lane must not block another path's open"
        assert not reopen.done()
        other_entry = other.result()
        await manager.evict(other_id, other_entry, can_evict=lambda: True)
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, other_path, "legacy other")
        assert response.status_code == 200
        assert other_path.read_text() == "legacy other"
        release.set()
        await evict
        assert (await reopen).live.text == "late"
    finally:
        release.set()
        await asyncio.gather(call, evict, reopen, other, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sqlite", "cancel"])
async def test_last_viewer_cleanup_survives_real_sqlite_error_and_cancellation(ownership_scope, tmp_path, monkeypatch, failure):
    from ws import document_control as control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    socket = object()
    actor = WriteActor(scope["owner"], "project-alpha")
    control._subscriptions[socket] = {entry.artifact_id: control.Subscription(entry, actor, True, "user")}
    entered, release = asyncio.Event(), asyncio.Event()
    broken = sqlite3.connect(":memory:")
    async def commit():
        entered.set()
        if failure == "sqlite":
            # A real SQLite operation generates the error, rather than a domain refusal.
            broken.execute("INSERT INTO missing VALUES (1)")
        else:
            await release.wait()
        return ()
    original_commit = entry.live.commit_received
    monkeypatch.setattr(entry.live, "commit_received", commit)
    leave = asyncio.create_task(control._leave(socket, entry.artifact_id))
    try:
        await entered.wait()
        if failure == "cancel":
            leave.cancel("viewer disconnected")
        await asyncio.gather(leave, return_exceptions=True)
        release.set()
        async def evicted():
            while entry.artifact_id in manager.documents:
                await asyncio.sleep(0)
        # Eviction must happen, not within a latency budget: a loaded CI runner
        # delayed the shielded flush and close past 0.2s.
        await asyncio.wait_for(evicted(), 5)
        assert entry.live.store.path.exists()
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, entry.live.path, "legacy after close")
        assert response.status_code == 200
    finally:
        release.set()
        control._subscriptions.pop(socket, None)
        monkeypatch.setattr(entry.live, "commit_received", original_commit)
        await manager.close()
        broken.close()


@pytest.mark.asyncio
async def test_startup_isolates_corrupt_store_and_publishes_healthy_owed_row(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_publisher
    scope = ownership_scope
    _write(scope, "healthy.md", "old")
    manager = runtime(scope, tmp_path)
    actor = WriteActor(scope["owner"], "project-alpha")
    entry = await manager.open("healthy-id", scope["category"] / "healthy.md")
    real = live_publisher.publish_one
    def refuse(*args, **kwargs):
        raise PermissionError("publication unavailable")
    monkeypatch.setattr(live_publisher, "publish_one", refuse)
    await entry.lane.submit(Operation("replace", content="new", actor=actor))
    await entry.publisher.task
    assert entry.live.store.next_publication() is not None
    await manager.close()
    corrupt = manager.root / "000-corrupt.sqlite"
    original = b"not a SQLite database; preserve this evidence"
    corrupt.write_bytes(original)
    monkeypatch.setattr(live_publisher, "publish_one", real)
    fresh = runtime(scope, tmp_path)
    try:
        await fresh.startup()
        await fresh.startup_task
        await asyncio.gather(*(publisher.task for publisher in fresh.publishers.values()))
        assert corrupt.read_bytes() == original
        # A store name has no content path; the refusal record starts at the next open.
        assert fresh.refused_stores == {}
        assert entry.live.store.next_publication() is None
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_repeated_cancel_during_open_keeps_reopen_behind_owned_cleanup(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_lane import DocumentLane
    scope = ownership_scope
    _write(scope, "opening.md", "old")
    manager = runtime(scope, tmp_path)
    path = scope["category"] / "opening.md"
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    imports = []
    importer, closer = DocumentLane._import, DocumentLane.close
    async def held_import(lane):
        imports.append(lane)
        entered.set()
        await release.wait()
        return await importer(lane)
    async def observe_close(lane):
        closing.set()
        return await closer(lane)
    monkeypatch.setattr(DocumentLane, "_import", held_import)
    monkeypatch.setattr(DocumentLane, "close", observe_close)
    opening = asyncio.create_task(manager.open("opening-id", path))
    reopen = None
    try:
        await entered.wait()
        opening.cancel("first")
        await closing.wait()
        opening.cancel("second")
        with pytest.raises(asyncio.CancelledError):
            await opening
        reopen = asyncio.create_task(manager.open("opening-id", path))
        await asyncio.wait({reopen}, timeout=.05)
        assert len(imports) == 1, "a cancelled opener must not leave an independent old lane behind"
        assert not reopen.done()
        release.set()
        assert (await reopen).live.text == "old"
        assert imports[0].closed
    finally:
        release.set()
        await asyncio.gather(opening, *([reopen] if reopen else []), return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_corrupt_open_answers_non_live_with_the_reason_without_changing_bytes(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_transport import Socket
    from ws import document_control as control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    original = b"corrupt SQLite evidence"
    entry.live.store.path.write_bytes(original)
    socket = Socket()
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    try:
        await control.handle_frame(socket, {"id": user_id}, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened_frame = await socket.next("doc_opened")
        # Damage is typed; the browser holds the copy and the SQLite reason goes to the log and event only.
        assert opened_frame["live"] is False and opened_frame["live_unavailable"] == "store_damaged"
        assert entry.live.store.path.read_bytes() == original
        assert manager.documents == {}
    finally:
        await manager.close()
