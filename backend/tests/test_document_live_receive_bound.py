"""Receive checks size with a cheap upper bound and dedupes by message id."""
import pytest

from doceditor import live_document
from doceditor.document_scope import MAX_TEXT_BYTES
from doceditor.live_document import LiveDocument
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone


def typed(peer, text: str, at: int = 0) -> bytes:
    before = peer.get_state()
    peer["text"].insert(at, text)
    return peer.get_update(before)


async def opened(tmp_path, size: int) -> LiveDocument:
    path = tmp_path / "document.md"
    path.write_text("x" * size)
    return await LiveDocument.open(path, tmp_path / "store.sqlite")


@pytest.mark.asyncio
async def test_receive_within_the_bound_never_clones(tmp_path, monkeypatch):
    live = await opened(tmp_path, 1000)
    peer = clone(live.doc.get_update())
    clones = []
    monkeypatch.setattr(live_document, "clone", lambda update: clones.append(update) or clone(update))
    try:
        for index in range(20):
            assert live.receive(BrowserMessage(str(index), typed(peer, "a", index)))
        assert clones == [] and live.text == str(peer["text"])
    finally:
        live.close()


@pytest.mark.asyncio
async def test_receive_at_the_limit_checks_exactly(tmp_path):
    live = await opened(tmp_path, MAX_TEXT_BYTES - 1)
    peer = clone(live.doc.get_update())
    try:
        assert live.receive(BrowserMessage("fits", typed(peer, "a")))
        assert len(live.text.encode("utf-8")) == MAX_TEXT_BYTES
        with pytest.raises(ValueError, match="too large"):
            live.receive(BrowserMessage("over", typed(peer, "b")))
        assert len(live.text.encode("utf-8")) == MAX_TEXT_BYTES and list(live.received) == ["fits"]
    finally:
        live.close()


@pytest.mark.asyncio
async def test_receive_dedupes_by_message_id(tmp_path):
    live = await opened(tmp_path, 10)
    peer = clone(live.doc.get_update())
    update = typed(peer, "a")
    try:
        assert live.receive(BrowserMessage("same", update))
        assert live.receive(BrowserMessage("same", update))
        assert live.text == "a" + "x" * 10 and live.backlog_bytes == len(update)
        with pytest.raises(ValueError, match="two document updates"):
            live.receive(BrowserMessage("same", typed(peer, "b")))
    finally:
        live.close()
