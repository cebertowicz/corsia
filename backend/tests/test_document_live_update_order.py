"""A merged update reaches every other viewer before any answer that captured it."""
import asyncio
import base64
from pathlib import Path

import pytest

from doceditor import live_comments
from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


async def _frames_until(socket, kind):
    frames = []
    while not frames or frames[-1]["type"] != kind:
        frames.append(await asyncio.wait_for(socket.sent.get(), 2))
    return frames


@pytest.fixture
async def viewers(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control
    scope = ownership_scope
    _write(scope, "order.md", "hello")
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
        db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    for name in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, name, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    sockets = []
    def open_frame():
        return {"type": "doc_open", "path": str(scope["category"] / "order.md"),
                "project_id": "project-alpha", "thread_id": scope["owner"]}
    try:
        yield document_control, manager, user, sockets, open_frame
    finally:
        for socket in sockets:
            await document_control.disconnect(socket)
        await manager.close()


def _merge_from(writer, opened, entry):
    """Merge one browser edit exactly as ``_operate`` does, with no await."""
    peer = clone(base64.b64decode(opened["update"]))
    before = peer.get_state()
    peer["text"].insert(5, " world")
    delta = peer.get_update(before)
    assert entry.live.receive(BrowserMessage("u-1", delta), sender=writer)
    return delta


def _merge_before_capture(monkeypatch, merge):
    """Run ``merge`` in the same loop step as the next positions capture."""
    real = live_comments.run_in_db
    async def run_in_db(*args, **kwargs):
        rows = await real(*args, **kwargs)
        if merge:
            merge.pop()()
        return rows
    monkeypatch.setattr(live_comments, "run_in_db", run_in_db)


@pytest.mark.asyncio
async def test_update_reaches_reader_before_positions_that_include_it(viewers, monkeypatch):
    control, manager, user, sockets, open_frame = viewers
    reader, writer = Socket(), Socket()
    sockets += [reader, writer]
    await control.handle_frame(reader, user, open_frame())
    opened = await reader.next("doc_opened")
    await control.handle_frame(writer, user, open_frame())
    writer_opened = await writer.next("doc_opened")
    entry = manager.documents[opened["artifact_id"]]
    merged = []
    _merge_before_capture(monkeypatch, [lambda: merged.append(_merge_from(writer, writer_opened, entry))])

    await control.handle_frame(reader, user, {"type": "doc_positions", "artifact_id": opened["artifact_id"],
                                              "store_id": opened["store_id"], "seq": 1})
    frames = await _frames_until(reader, "doc_positions")

    assert entry.live.text == "hello world"
    updates = [frame for frame in frames if frame["type"] == "doc_update"]
    assert [base64.b64decode(frame["update"]) for frame in updates] == merged
    assert frames[-1]["digest"] == entry.live.digest


@pytest.mark.asyncio
async def test_update_reaches_opener_before_doc_opened_that_includes_it(viewers, monkeypatch):
    control, manager, user, sockets, open_frame = viewers
    writer, opener = Socket(), Socket()
    sockets += [writer, opener]
    await control.handle_frame(writer, user, open_frame())
    writer_opened = await writer.next("doc_opened")
    entry = manager.documents[writer_opened["artifact_id"]]
    merged = []
    _merge_before_capture(monkeypatch, [lambda: merged.append(_merge_from(writer, writer_opened, entry))])

    await control.handle_frame(opener, user, open_frame())
    frames = await _frames_until(opener, "doc_opened")

    assert entry.live.text == "hello world"
    updates = [frame for frame in frames if frame["type"] == "doc_update"]
    assert [base64.b64decode(frame["update"]) for frame in updates] == merged
    assert frames[-1]["digest"] == entry.live.digest
