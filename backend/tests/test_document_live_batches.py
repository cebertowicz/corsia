"""Message-ID acknowledgements and durable closure on the real CRDT/store."""
import asyncio
import sqlite3
import threading

import pytest

from doceditor.live_store import BrowserMessage, LiveStore, StoreRefused
from doceditor.live_text import clone, create


async def opened(tmp_path, **kwargs):
    from doceditor.live_document import LiveDocument

    path = tmp_path / "document.md"
    path.write_text("hello quote")
    return await LiveDocument.open(path, tmp_path / "state.sqlite", **kwargs)


def message(live, identifier, change):
    peer = clone(live.doc.get_update())
    before = peer.get_state()
    change(peer["text"])
    return BrowserMessage(identifier, peer.get_update(before))


@pytest.mark.asyncio
async def test_deletion_ack_is_by_message_id_and_survives_restart(tmp_path):
    live = await opened(tmp_path)
    delete = message(live, "delete", lambda text: text.__delitem__(slice(0, 6)))
    assert live.receive(delete)
    assert await live.commit_received() == ("delete",)
    resumed = await opened(tmp_path)
    assert resumed.text == "quote"
    assert resumed.receive(delete)
    assert await resumed.commit_received() == ("delete",)
    assert resumed.text == "quote"


@pytest.mark.asyncio
async def test_inflight_batch_does_not_ack_next_update(tmp_path):
    live = await opened(tmp_path)
    first = message(live, "first", lambda text: text.insert(0, "A"))
    live.receive(first)
    entered = threading.Event()
    release = threading.Event()
    original = live.store.commit_batch

    def held(messages):
        entered.set()
        assert release.wait(2)
        return original(messages)

    live.store.commit_batch = held
    task = asyncio.create_task(live.commit_received())
    assert await asyncio.to_thread(entered.wait, 2)
    second = message(live, "second", lambda text: text.insert(0, "B"))
    live.receive(second)
    release.set()
    assert await task == ("first",)
    assert await live.commit_received() == ("second",)
    assert live.text == "BAhello quote"


@pytest.mark.asyncio
async def test_store_refusal_keeps_deletion_for_retry(tmp_path):
    live = await opened(tmp_path)
    live.receive(message(live, "delete", lambda text: text.__delitem__(slice(0, 6))))
    lock = sqlite3.connect(live.store.path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StoreRefused):
            await live.commit_received()
        assert live.text == "quote"
        assert list(live.received) == ["delete"]
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert await live.commit_received() == ("delete",)
    assert (await opened(tmp_path)).text == "quote"


@pytest.mark.asyncio
async def test_backlog_cap_refuses_without_merging_then_accepts_retry(tmp_path):
    live = await opened(tmp_path, backlog_limit=100)
    first = message(live, "first", lambda text: text.insert(0, "x" * 60))
    second = message(live, "second", lambda text: text.insert(0, "y" * 60))
    assert live.receive(first)
    assert not live.receive(second)
    assert "y" not in live.text
    assert await live.commit_received() == ("first",)
    assert live.receive(second)
    assert await live.commit_received() == ("second",)


@pytest.mark.asyncio
async def test_live_comment_maps_local_and_remote_unicode_boundaries(tmp_path):
    live = await opened(tmp_path)
    live.track_range("comment", 6, 11)
    live.receive(message(live, "start", lambda text: text.insert(6, "🙂")))
    assert live.ranges["comment"] == (8, 13)
    live.receive(message(live, "end", lambda text: text.insert(15, "👍🏽")))
    assert live.ranges["comment"] == (8, 13)
    assert live.range_text("comment") == "quote"
