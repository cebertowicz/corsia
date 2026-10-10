"""Transactions broadcast their insertion/deletion delta only to other viewers."""
import asyncio
import base64
from pathlib import Path
import pytest
from doceditor.live_runtime import LiveDocuments
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_three_viewers_receive_transaction_delta_without_sender_echo(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control
    scope = ownership_scope
    _write(scope, "delta.md", "x" * 20000)
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
        db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    for name in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, name, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    sockets, peers, opens = [Socket() for _ in range(3)], [], []
    try:
        for socket in sockets:
            await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(scope["category"] / "delta.md"),
                "project_id": "project-alpha", "thread_id": scope["owner"]})
            opened = await socket.next("doc_opened")
            opens.append(opened)
            peers.append(clone(base64.b64decode(opened["update"])))
        identity = {key: opens[0][key] for key in ("artifact_id", "store_id")}
        entry = manager.documents[identity["artifact_id"]]
        for idx, kind in enumerate(("insert", "delete")):
            before = peers[0].get_state()
            if kind == "insert":
                peers[0]["text"].insert(3, "typed")
            else:
                del peers[0]["text"][3:8]
            delta = peers[0].get_update(before)
            await document_control.handle_frame(sockets[0], user, {**identity, "type": "doc_update",
                "message_id": str(idx), "update": base64.b64encode(delta).decode()})
            while True:
                sender_frame = await asyncio.wait_for(sockets[0].sent.get(), 2)
                assert sender_frame["type"] != "doc_update"
                if sender_frame["type"] == "doc_ack":
                    break
            for socket, peer in zip(sockets[1:], peers[1:]):
                frame = await socket.next("doc_update")
                update = base64.b64decode(frame["update"])
                assert update == delta
                peer.apply_update(update)
                assert str(peer["text"]) == entry.live.text
            await asyncio.sleep(0)
            while not sockets[0].sent.empty():
                assert sockets[0].sent.get_nowait()["type"] != "doc_update"
    finally:
        for socket in sockets:
            await document_control.disconnect(socket)
        await manager.close()
