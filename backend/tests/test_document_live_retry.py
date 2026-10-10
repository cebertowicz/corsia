"""A refused promote is retried as a later lane item, outside the tool join."""
import asyncio

import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import StoreRefused
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_runtime_retries_promote_without_another_file_write(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    promote, write = entry.live.store.promote, entry.lane.write
    attempts, writes = [], []
    def once(op_id, *clears_through):
        attempts.append(op_id)
        if len(attempts) == 1:
            raise StoreRefused("promote refused")
        return promote(op_id, *clears_through)
    async def counted(text, prior_hash):
        writes.append(text)
        return await write(text, prior_hash)
    monkeypatch.setattr(entry.live.store, "promote", once)
    entry.lane.write = counted
    try:
        result = await entry.lane.submit(Operation("replace", content="changed",
            actor=WriteActor(scope["owner"], "project-alpha")))
        assert not result.server_saved and entry.live.path.read_text() == "changed"
        async def recovered():
            while await asyncio.to_thread(entry.live.store.pending) is not None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(recovered(), 2)
        assert attempts[0] == attempts[1] and len(attempts) == 2
        assert writes == ["changed"] and entry.live.text == "changed"
    finally:
        await manager.close()
