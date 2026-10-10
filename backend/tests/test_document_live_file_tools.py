"""File tools await the live lane and base whole writes on disk bytes served."""
import asyncio
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


async def opened(scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from tools import filesystem_rw, filesystem_edit
    from tools.filesystem_gate import AuthorizedPath
    async def authorized(*, path, **kwargs):
        return AuthorizedPath(Path(path).resolve(), reviewed=False)
    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorized)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorized)
    _write(scope, "live.md", "a b\nsecond\n")
    path = scope["category"] / "live.md"
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(path),)).fetchone()
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    entry = await manager.open(artifact[0], path)
    return manager, entry, {"_thread_id": scope["owner"], "_thread_project_id": "project-alpha"}


@pytest.mark.asyncio
async def test_whole_write_requires_read_and_partial_read_records_whole_disk_base(ownership_scope, tmp_path, monkeypatch):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    try:
        writer = WriteFileTool(workspace=scope["workspace"])
        refused = await writer.execute(path=path, content="a B\nsecond\n", **context)
        assert refused.startswith("Error") and "read" in refused
        assert entry.live.path.read_text() == "a b\nsecond\n"
        read = await ReadFileTool(workspace=scope["workspace"]).execute(path, offset=2, limit=1, **context)
        assert "second" in read
        client = clone(entry.live.doc.get_update())
        vector = client.get_state()
        client["text"].insert(0, "USER ")
        entry.live.receive(BrowserMessage("typing", client.get_update(vector)))
        result = await writer.execute(path=path, content="a B\nsecond\n", **context)
        assert not result.startswith("Error")
        assert "merged with live edits" in result
        assert entry.live.text == "USER a B\nsecond\n" == entry.live.path.read_text()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_read_base_does_not_outlive_the_loaded_document(ownership_scope, tmp_path, monkeypatch):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool

    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    reader = ReadFileTool(workspace=scope["workspace"])
    writer = WriteFileTool(workspace=scope["workspace"])
    artifact_id = entry.artifact_id
    try:
        await reader.execute(path, **context)
        await manager.evict(artifact_id, entry, can_evict=lambda: True)
        # Reads with no loaded document need no live merge cache either.
        await reader.execute(path, **context)
        reopened = await manager.open(artifact_id, Path(path))
        refused = await writer.execute(path=path, content="changed", **context)
        assert refused.startswith("Error") and "read" in refused
        assert reopened.live.text == reopened.live.path.read_text() == "a b\nsecond\n"
        await reader.execute(path, **context)
        accepted = await writer.execute(path=path, content="changed", **context)
        assert not accepted.startswith("Error")
        assert reopened.live.text == reopened.live.path.read_text() == "changed"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_edit_and_multi_edit_use_current_live_text(ownership_scope, tmp_path, monkeypatch):
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    try:
        client = clone(entry.live.doc.get_update())
        vector = client.get_state()
        client["text"].insert(0, "USER ")
        entry.live.receive(BrowserMessage("typing", client.get_update(vector)))
        first = await EditFileTool(workspace=scope["workspace"]).execute(path, old_string="USER", new_string="user", **context)
        assert not first.startswith("Error") and entry.live.text.startswith("user ")
        second = await MultiEditTool(workspace=scope["workspace"]).execute(path, edits=[{"old_string": "user", "new_string": "person"},
                                                                                  {"old_string": "second", "new_string": "last"}], **context)
        assert not second.startswith("Error")
        assert entry.live.text == "person a b\nlast\n" == entry.live.path.read_text()
    finally:
        await manager.close()
