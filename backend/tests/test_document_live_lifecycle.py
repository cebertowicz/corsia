"""Runtime eviction drains writes; retry and cached base retention remain bounded."""
import asyncio

import pytest

from doceditor.live_lane import LaneRefused, Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import StoreRefused
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_recent_bases_are_bounded_and_missing_base_refuses_before_write(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    first = next(iter(entry.live.bases))
    actor = WriteActor(scope["owner"], "project-alpha")
    try:
        for number in range(12):
            await entry.lane.submit(Operation("replace", content=f"version {number}", actor=actor))
        assert len(entry.live.bases) <= 8
        before = entry.live.path.read_text()
        with pytest.raises(LaneRefused, match="read it again"):
            refused = await entry.lane.submit(Operation("write", content="OLD", base_hash=first, actor=actor))
            assert refused.outcome == "refused"
            refused.require_landed()
        assert entry.live.path.read_text() == before
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_evict_drains_started_write_and_blocks_reopen_until_finished(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    write = entry.lane.write
    async def held(text, prior_hash):
        entered.set()
        await release.wait()
        await write(text, prior_hash)
    entry.lane.write = held
    actor = WriteActor(scope["owner"], "project-alpha")
    call = asyncio.create_task(entry.lane.submit(Operation("replace", content="DRAINED", actor=actor)))
    await entered.wait()
    eviction = asyncio.create_task(manager.evict(entry.artifact_id, entry, can_evict=lambda: True))
    async def closing():
        while not entry.lane.closed:
            await asyncio.sleep(0)
    await asyncio.wait_for(closing(), 2)
    reopen = asyncio.create_task(manager.open(entry.artifact_id, entry.live.path))
    try:
        await asyncio.sleep(0)
        assert not eviction.done() and not reopen.done()
        with pytest.raises(LaneRefused, match="closed"):
            refused = await entry.lane.submit(Operation("flush", actor=actor))
            assert refused.outcome == "refused"
            refused.require_landed()
        release.set()
        assert (await call).server_saved
        await eviction
        replacement = await reopen
        assert replacement is not entry and replacement.live.text == "DRAINED"
        assert entry.lane.retry.closed and entry.lane.retry.timer is None
    finally:
        release.set()
        await asyncio.gather(call, eviction, reopen, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_deleted_file_refuses_flush_and_keeps_acknowledged_store(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_suggestions import typing
    from doceditor.live_text import clone
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    typing(entry, 0, insert="DURABLE ")
    assert await entry.live.commit_received()
    entry.live.path.unlink()
    try:
        with pytest.raises(FileNotFoundError):
            await entry.lane.submit(Operation("flush", actor=WriteActor(scope["owner"], "project-alpha")))
        assert not entry.live.path.exists()
        saved = clone(b"\x00\x00")
        for update in entry.live.store.load_updates():
            saved.apply_update(update)
        assert str(saved["text"]) == entry.live.text == "DURABLE a b\nsecond\n"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_refused_clear_retries_as_priority_lane_item(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    discard = entry.live.store.discard
    attempts = []
    def refused_once(op_id, *args):
        attempts.append(op_id)
        if len(attempts) == 1:
            raise StoreRefused("clear refused")
        return discard(op_id, *args)
    async def no_write(text, prior_hash):
        raise OSError("write refused")
    monkeypatch.setattr(entry.live.store, "discard", refused_once)
    entry.lane.write = no_write
    actor = WriteActor(scope["owner"], "project-alpha")
    try:
        with pytest.raises(StoreRefused, match="clear refused"):
            await entry.lane.submit(Operation("replace", content="NEVER", actor=actor))
        assert entry.live.store.pending() is not None
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold():
            entered.set()
            await release.wait()
        entry.lane.schedule_setup(hold)
        await entered.wait()
        seen = []
        async def observe():
            seen.append(entry.live.store.pending())
        entry.lane.schedule_setup(observe)
        # Fire the real timer callback deterministically while one item is started.
        entry.lane.retry.timer.cancel()
        entry.lane.retry._enqueue()
        release.set()
        await entry.lane._worker
        assert seen == [None]
        assert attempts[0] == attempts[1] and len(attempts) == 2
        assert entry.live.path.read_text() == "a b\nsecond\n"
    finally:
        await manager.close()
