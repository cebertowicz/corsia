"""Opening guidance creates no live state and its existing save still commits history."""
from pathlib import Path

import pytest

from doceditor.document_save import DocumentSave, save_document_text
from doceditor.live_runtime import LiveDocuments
from infra.database import get_db
from tests.test_document_live_transport import Socket
from tests.test_guidance_history import history_scope as history_scope, SKILL_BODY, _git


@pytest.mark.asyncio
async def test_guidance_legacy_open_then_save_records_history(history_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from ws import document_control
    workspace, thread_id = history_scope
    path = workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(SKILL_BODY)
    db = get_db()
    try:
        user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()[0]
        db_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    finally:
        db.close()
    manager = LiveDocuments(tmp_path / "live", workspace=workspace, db_path=db_path)
    monkeypatch.setattr(live_runtime, "_manager", manager)
    socket = Socket()
    try:
        await document_control.handle_frame(socket, {"id": user_id}, {"type": "doc_open", "path": str(path), "thread_id": thread_id})
        assert (await socket.next("doc_opened"))["live"] is False
        assert manager.documents == {} and manager.publishers == {}
        assert not list(manager.root.glob("*.sqlite"))
        text = SKILL_BODY + "\nEdited guidance.\n"
        receipt = await save_document_text(DocumentSave(path=str(path), text=text, thread_id=thread_id), user_id)
        assert receipt["saved"]
        assert any(line.startswith("Saved and committed") for line in receipt["guidance_history"].splitlines())
        relative = path.relative_to(workspace).as_posix()
        assert _git(["show", "HEAD:" + relative], cwd=workspace) == text
        assert not list(manager.root.glob("*.sqlite"))
    finally:
        await document_control.disconnect(socket)
        await manager.close()
