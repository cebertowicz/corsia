"""Reconnects preserve their established document identity."""
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_reconnect_refuses_retargeted_alias_before_manager_open(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control

    scope = ownership_scope
    _write(scope, "first.md", "FIRST")
    _write(scope, "second.md", "SECOND")
    first, second = scope["category"] / "first.md", scope["category"] / "second.md"
    alias = scope["category"] / "alias.md"
    alias.symlink_to(first)
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    first_socket, reconnect_socket = Socket(), Socket()
    request = {"type": "doc_open", "path": str(alias), "project_id": "project-alpha", "thread_id": scope["owner"]}
    try:
        await document_control.handle_frame(first_socket, user, {**request, "request_id": "first"})
        opened = await first_socket.next("doc_opened")
        assert opened["live"] is True
        alias.unlink()
        alias.symlink_to(second)
        await document_control.handle_frame(reconnect_socket, user, {
            **request, "request_id": "reconnect", "expected_artifact_id": opened["artifact_id"],
        })
        assert await reconnect_socket.next("doc_opened") == {
            "type": "doc_opened", "live": False, "request_id": "reconnect",
        }
        assert list(manager.documents) == [opened["artifact_id"]]
    finally:
        await document_control.disconnect(first_socket)
        await document_control.disconnect(reconnect_socket)
        await manager.close()
