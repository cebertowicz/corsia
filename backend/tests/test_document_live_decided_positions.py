"""A decided suggestion keeps its last offsets; a later, shorter text still opens live."""
import asyncio
from pathlib import Path

import pytest

from doceditor import comments
from doceditor.live_runtime import LiveDocuments
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "rejected"])
async def test_a_decided_suggestion_past_the_end_of_the_text_does_not_refuse_the_open(
        ownership_scope, tmp_path, monkeypatch, decision):
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    _write(scope, "decided.md", "first line\nsecond line\n")
    path = scope["category"] / "decided.md"
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(path),)).fetchone()[0]
    decided, _ = comments.create_with_status(artifact, body="Shorter.", author="author", author_kind="agent",
        author_thread_id=scope["other"], anchor_text="second line", from_off=11, to_off=22,
        replacement="2nd line", db=scope["db"])
    placed, _ = comments.create_with_status(artifact, body="Kept.", author="author", author_kind="agent",
        author_thread_id=scope["other"], anchor_text="first", from_off=0, to_off=5, db=scope["db"])
    scope["db"].execute("UPDATE doc_comments SET decision=?, resolved=1 WHERE id=?", (decision, decided["id"]))
    scope["db"].commit()
    # The owner later cuts the text the decision was about, so its kept offsets reach past the end.
    path.write_text("first line\n", encoding="utf-8")
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"], db_path=db_path)
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    socket = Socket()
    try:
        await document_control.handle_frame(socket, {"id": user_id}, {
            "type": "doc_open", "path": str(path), "project_id": "project-alpha",
            "thread_id": scope["owner"], "request_id": "open"})
        frame = await open_answer(socket)
        assert frame["type"] == "doc_opened", frame
        assert frame["live"] is True
        assert frame["ranges"] == [{"id": placed["id"], "from_off": 0, "to_off": 5}]
    finally:
        await document_control.disconnect(socket)
        await manager.close()


async def open_answer(socket: Socket) -> dict:
    """The open's own answer, whichever it is."""
    while True:
        frame = await asyncio.wait_for(socket.sent.get(), 2)
        if frame.get("request_id") == "open":
            return frame
