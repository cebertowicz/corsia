"""Document frames use the real load, lane and durable message receipt owners."""
import asyncio
import base64
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_text import clone
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write, _enforce
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
async def test_disconnect_of_non_document_socket_preserves_other_viewers(monkeypatch):
    """An ordinary socket has no live-document cleanup, even with other viewers."""
    from types import SimpleNamespace
    from ws import document_control

    viewer = object()
    subscriptions = {viewer: {"open-document": object()}}
    tasks = {viewer: set()}
    openings = {viewer: set()}
    monkeypatch.setattr(document_control, "_subscriptions", subscriptions)
    monkeypatch.setattr(document_control, "_tasks", tasks)
    monkeypatch.setattr(document_control, "_openings", openings)
    await document_control.disconnect(SimpleNamespace())
    assert subscriptions[viewer].keys() == {"open-document"}
    assert tasks == {viewer: set()} and openings == {viewer: set()}


class Socket:
    def __init__(self):
        self.sent = asyncio.Queue()
        self.inbound = asyncio.Queue()

    async def send_json(self, frame):
        await self.sent.put(frame)

    async def receive_json(self):
        return await self.inbound.get()

    async def next(self, kind):
        while True:
            frame = await asyncio.wait_for(self.sent.get(), 2)
            if frame["type"] == kind:
                return frame


@pytest.mark.asyncio
async def test_frames_open_merge_ack_and_flush_without_whole_text_save(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    _write(scope, "live.md", "old")
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"], db_path=db_path)
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    socket = Socket()
    try:
        await document_control.handle_frame(socket, {"id": user_id}, {
            "type": "doc_open", "path": str(scope["category"] / "live.md"),
            "project_id": "project-alpha", "thread_id": scope["owner"], "request_id": "open"})
        opened = await socket.next("doc_opened")
        assert opened["live"] is True
        entry = manager.documents[opened["artifact_id"]]
        client = clone(base64.b64decode(opened["update"]))
        vector = client.get_state()
        client["text"].insert(3, " typed")
        await document_control.handle_frame(socket, {"id": user_id}, {
            "type": "doc_update", "artifact_id": opened["artifact_id"], "store_id": opened["store_id"],
            "message_id": "typing", "update": base64.b64encode(client.get_update(vector)).decode()})
        received = await socket.next("doc_received")
        assert received["message_ids"] == ["typing"]
        ack = await socket.next("doc_ack")
        assert ack["message_ids"] == ["typing"]
        await document_control.handle_frame(socket, {"id": user_id}, {
            "type": "doc_flush", "artifact_id": opened["artifact_id"], "store_id": opened["store_id"],
            "request_id": "flush"})
        saved = await socket.next("doc_saved")
        assert saved["server_saved"] is True
        assert entry.live.text == "old typed" == entry.live.path.read_text()
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_store_mismatch_and_unsubscribed_updates_cannot_merge(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    _write(scope, "live.md", "old")
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_update", "artifact_id": "unknown",
                                                        "store_id": "other", "message_id": "bad", "update": "AA=="})
        frame = await socket.next("doc_error")
        assert frame["kind"] == "not_open"
        assert manager.documents == {}
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_receiver_keeps_stop_and_updates_flowing_during_started_flush(ownership_scope, tmp_path, monkeypatch):
    import ws
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    _write(scope, "live.md", "old")
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    stopped, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(ws, "_handle_stop", lambda *args, **kwargs: stopped.set())
    receiver = asyncio.create_task(ws._receive_frames(socket, user, asyncio.Queue()))
    try:
        await socket.inbound.put({"type": "doc_open", "path": str(scope["category"] / "live.md"),
                                  "project_id": "project-alpha", "thread_id": scope["owner"]})
        opened = await socket.next("doc_opened")
        entry = manager.documents[opened["artifact_id"]]
        async def held(stage):
            if stage == "record":
                entered.set()
                await release.wait()
        cut_at(monkeypatch, held)
        base = {"artifact_id": opened["artifact_id"], "store_id": opened["store_id"]}
        await socket.inbound.put({**base, "type": "doc_flush"})
        await asyncio.wait_for(entered.wait(), 2)
        client = clone(base64.b64decode(opened["update"]))
        vector = client.get_state()
        client["text"].insert(3, " during")
        await socket.inbound.put({**base, "type": "doc_update", "message_id": "during",
                                  "update": base64.b64encode(client.get_update(vector)).decode()})
        await socket.inbound.put({"type": "stop", "thread_id": scope["owner"]})
        await asyncio.wait_for(stopped.wait(), 2)
        receipt = await socket.next("doc_received")
        assert receipt["message_ids"] == ["during"] and not release.is_set()
    finally:
        release.set()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_frame_for_unopened_document_refuses_with_its_artifact_id(monkeypatch):
    """Clients route by artifact id; a refusal without it leaves the request in flight."""
    from ws import document_control
    monkeypatch.setattr(document_control, "_subscriptions", {})
    socket = Socket()

    await document_control.handle_frame(socket, {"id": "user-1"}, {
        "type": "doc_positions", "artifact_id": "artifact-1", "store_id": "store-1", "seq": 1, "request_id": "r-1"})

    frame = await socket.next("doc_error")
    assert (frame["kind"], frame["artifact_id"], frame["request_id"]) == ("not_open", "artifact-1", "r-1")
