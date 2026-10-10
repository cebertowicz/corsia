"""Forced document admission orderings preserve owned evidence."""
import asyncio

import pytest

from doceditor.live_lane import DocumentLane, Operation
from doceditor.live_publication_record import WriteActor
from tests.test_document_live_lifetime_reset import runtime
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_prequeued_opener_waits_for_repeated_cancelled_open_cleanup(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    _write(scope, "prequeued.md", "old")
    manager = runtime(scope, tmp_path)
    actor = WriteActor(scope["owner"], "project-alpha")
    path = scope["category"] / "prequeued.md"
    owner = manager._lifetime(path)
    entered, queued, closing, release = (asyncio.Event() for _ in range(4))
    importer, closer, acquire = DocumentLane._import, DocumentLane.close, owner.lock.acquire
    lanes = []
    async def held_import(lane):
        lanes.append(lane)
        entered.set()
        await release.wait()
        return await importer(lane)
    async def observe_close(lane):
        closing.set()
        return await closer(lane)
    async def observe_wait():
        if owner.lock.locked():
            queued.set()
        return await acquire()
    monkeypatch.setattr(DocumentLane, "_import", held_import)
    monkeypatch.setattr(DocumentLane, "close", observe_close)
    monkeypatch.setattr(owner.lock, "acquire", observe_wait)
    opening = asyncio.create_task(manager.open("prequeued-id", path))
    reopen = None
    try:
        await entered.wait()
        reopen = asyncio.create_task(manager.open("prequeued-id", path))
        await queued.wait()  # The second opener has passed the first cleanup check.
        opening.cancel("first")
        await closing.wait()
        opening.cancel("second")
        with pytest.raises(asyncio.CancelledError):
            await opening
        await asyncio.wait({reopen}, timeout=.05)
        assert lanes == [lanes[0]], "a queued opener cannot start a second lane before cleanup settles"
        if reopen.done():
            from doceditor.live_lane import LaneRefused
            with pytest.raises(LaneRefused, match="cleanup"):
                await reopen
            release.set()
            await owner.closing
            entry = await manager.open("prequeued-id", path)
        else:
            release.set()
            entry = await asyncio.wait_for(reopen, 2)
        assert lanes[0].closed
        assert not entry.lane.closed and entry.live.text == path.read_text() == "old"
        result = await entry.lane.submit(Operation("replace", content="healthy", actor=actor))
        assert result.outcome == "landed" and result.server_saved
        assert entry.live.text == path.read_text() == "healthy"
    finally:
        release.set()
        await asyncio.gather(opening, *([reopen] if reopen else []), return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_path_cannot_have_two_live_artifact_identities(ownership_scope, tmp_path):
    from doceditor.live_lane import LaneRefused
    scope = ownership_scope
    _write(scope, "identity.md", "old")
    manager = runtime(scope, tmp_path)
    path = scope["category"] / "identity.md"
    try:
        first = await manager.open("first-id", path)
        with pytest.raises(LaneRefused, match="identity"):
            await manager.open("second-id", path)
        assert tuple(manager.documents.values()) == (first,)
        await manager.evict("first-id", first, can_evict=lambda: True)
        assert manager.documents == {} and first.lane.closed
        second = await manager.open("second-id", path)
        assert not second.lane.closed and second.artifact_id == "second-id"
        assert second.live.text == "old"
    finally:
        await manager.close()
