"""A refused last-viewer commit cannot retain a dead socket or another document."""
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import BrowserMessage, StoreRefused
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_disconnect_isolates_refused_commit_and_removes_dead_socket(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control
    scope = ownership_scope
    for name in ("one.md", "two.md"):
        _write(scope, name, "base")
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    socket = Socket()
    entries = []
    try:
        for name in ("one.md", "two.md"):
            await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(scope["category"] / name),
                "project_id": "project-alpha", "thread_id": scope["owner"]})
            opened = await socket.next("doc_opened")
            entry = manager.documents[opened["artifact_id"]]
            entries.append(entry)
            peer = clone(entry.live.doc.get_update())
            before = peer.get_state()
            peer["text"].insert(4, " TYPED")
            assert entry.live.receive(BrowserMessage(name, peer.get_update(before)))
        original = entries[0].live.commit_received
        async def refused():
            raise StoreRefused("commit refused")
        monkeypatch.setattr(entries[0].live, "commit_received", refused)
        try:
            await document_control.disconnect(socket)
        finally:
            monkeypatch.setattr(entries[0].live, "commit_received", original)
        assert socket not in document_control._subscriptions
        assert socket not in document_control._tasks and socket not in document_control._openings
        assert manager.documents == {}
        assert entries[0].live.path.read_text() == "base"
        assert entries[1].live.path.read_text() == "base TYPED"
        reopened = await type(entries[1].live).open(entries[1].live.path, entries[1].live.store.path)
        try:
            assert reopened.text == "base TYPED"
        finally:
            reopened.close()
    finally:
        await document_control.disconnect(socket)
        await manager.close()
