"""An opening reservation must release drained publishers on failed admission."""
import asyncio
import hashlib

import pytest

from doceditor.live_admission import OpenRefused
from doceditor.live_document import LiveDocument
from doceditor.live_store import StoreRefused
from tests.test_document_live_owed_startup import runtime
from tests.test_document_live_publisher import setup, owe
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("load refused"), StoreRefused("load refused"), asyncio.CancelledError()])
async def test_failed_open_drops_a_publisher_that_drained_during_admission(ownership_scope, tmp_path, monkeypatch, error):
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "new")
    state_path = tmp_path / (hashlib.sha256(b"doc-id").hexdigest() + ".sqlite")
    store.path.rename(state_path)
    manager = runtime(scope, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(*args):
        entered.set()
        await release.wait()
        raise error
    monkeypatch.setattr(LiveDocument, "open", held)
    opener = asyncio.create_task(manager.open("doc-id", scope["category"] / "live.md"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.startup()
        await manager.startup_task
        publisher = manager.publishers[state_path]
        await publisher.task
        assert publisher.drained and manager.publishers[state_path] is publisher
        release.set()
        refusal_type = OpenRefused if isinstance(error, StoreRefused) else type(error)
        with pytest.raises(refusal_type):
            await opener
        assert manager.publishers == {}
    finally:
        release.set()
        await asyncio.gather(opener, return_exceptions=True)
        await manager.close()
