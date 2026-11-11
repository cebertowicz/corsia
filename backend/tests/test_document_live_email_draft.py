"""Live frames of an email draft carry the header as the server reads it, so the view's warnings follow each edit."""
import base64
import os
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_text import clone
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write

TEXT = "From: me@example.com\nTo: sam@example.com\nSubject: Lunch\n\nNoon?\n"


def _record(scope, name: str) -> None:
    relative = os.path.relpath((scope["category"] / name).resolve(), scope["workspace"].resolve())
    scope["db"].execute(
        "INSERT INTO action_drafts (id,thread_id,kind,path,recorded_revision,recorded_at,created_at) "
        "VALUES ('draft-1',?,'email',?,'r','2026-11-10','2026-11-10')", (scope["owner"], relative))
    scope["db"].commit()


async def _open(document_control, socket, scope, user_id: str, name: str) -> dict:
    await document_control.handle_frame(socket, {"id": user_id}, {
        "type": "doc_open", "path": str(scope["category"] / name),
        "project_id": "project-alpha", "thread_id": scope["owner"], "request_id": name})
    return await socket.next("doc_opened")


@pytest.mark.asyncio
async def test_open_and_each_received_edit_carry_the_draft_header_and_a_plain_file_carries_none(ownership_scope, tmp_path,
                                                                                               monkeypatch):
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    _write(scope, "draft.md", TEXT)
    _write(scope, "plain.md", TEXT)
    _record(scope, "draft.md")
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"], db_path=db_path)
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    socket = Socket()
    try:
        opened = await _open(document_control, socket, scope, user_id, "draft.md")
        assert opened["email_draft"]["id"] == "draft-1"
        assert [r["full"] for r in opened["email_draft"]["header"]["recipients"]] == [True]
        client = clone(base64.b64decode(opened["update"]))
        vector = client.get_state()
        client["text"].insert(len("From: me@example.com\nTo: sam@example.com"), ", priya")
        await document_control.handle_frame(socket, {"id": user_id}, {
            "type": "doc_update", "artifact_id": opened["artifact_id"], "store_id": opened["store_id"],
            "message_id": "typing", "update": base64.b64encode(client.get_update(vector)).decode()})
        received = await socket.next("doc_received")
        assert [(r["text"], r["full"]) for r in received["email_draft"]["header"]["recipients"]] == [
            ("sam@example.com", True), ("priya", False)]
        plain = await _open(document_control, socket, scope, user_id, "plain.md")
        assert "email_draft" not in plain
    finally:
        await document_control.disconnect(socket)
        await manager.close()
