"""A promote never strands a live edit: the open document is saved into the file before it moves."""
import asyncio
from pathlib import Path

import pytest

from doceditor.live_publication_record import WriteActor
from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone
from tests.test_durable_writes import _working_file
from tests.test_durable_writes import durable_scope as durable_scope

CARRIED_AND_STALE = pytest.mark.parametrize("stale", [False, True], ids=["carried", "stale"])


async def _open(durable_scope, tmp_path, monkeypatch, stale, name="live.md", *, typed=True):
    """A working file open for editing, with one person's keystroke accepted by the store but not yet saved."""
    from doceditor import live_runtime
    from tools import promote

    workspace, _root, thread_id, db, source, source_id, _text = _working_file(durable_scope, name, stale=stale)
    manager = LiveDocuments(tmp_path / "live", workspace=workspace,
                            db_path=Path(db.execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    monkeypatch.setattr(promote, "_require_context", lambda kwargs: (thread_id, {"project_id": "project-alpha"}))
    entry = await manager.open(source_id, source)
    entry.live.viewer_actor = lambda: WriteActor("ui", "project-alpha", direct_user=True, user_id="u1")
    if not typed:
        return manager, entry, workspace, thread_id, source, source_id
    client = clone(entry.live.doc.get_update())
    vector = client.get_state()
    client["text"].insert(0, "TYPED ")
    entry.live.receive(BrowserMessage("typing", client.get_update(vector)))
    await entry.live.commit_received()
    assert entry.live.text.startswith("TYPED ") and not source.read_text().startswith("TYPED ")
    return manager, entry, workspace, thread_id, source, source_id


async def _promote(workspace, thread_id, source) -> str:
    from tools.promote import PromoteArtifactTool

    return await PromoteArtifactTool(workspace).execute(
        str(source), "planning", "Promoted launch plan", update_trigger="when the plan changes", _thread_id=thread_id)


@CARRIED_AND_STALE
@pytest.mark.asyncio
async def test_promote_carries_the_text_a_person_typed_but_not_yet_saved(durable_scope, tmp_path, monkeypatch, stale):
    manager, entry, workspace, thread_id, source, source_id = await _open(durable_scope, tmp_path, monkeypatch, stale)
    try:
        receipt = await _promote(workspace, thread_id, source)
        shared = workspace / "projects" / "alpha" / "shared" / "planning" / "live.md"
        assert not receipt.startswith("Error"), receipt
        assert shared.read_text().startswith("TYPED ")
        assert not source.exists()
        assert manager.documents == {}
    finally:
        await asyncio.wait_for(manager.close(), 20)


@pytest.mark.asyncio
async def test_a_tab_reopens_the_carried_document_on_the_shared_path(durable_scope, tmp_path, monkeypatch):
    manager, entry, workspace, thread_id, source, source_id = await _open(durable_scope, tmp_path, monkeypatch, False)
    try:
        await _promote(workspace, thread_id, source)
        shared = workspace / "projects" / "alpha" / "shared" / "planning" / "live.md"
        reopened = await manager.open(source_id, shared)
        assert reopened is not entry and reopened.live.path == shared
        assert reopened.live.text == shared.read_text() and reopened.live.text.startswith("TYPED ")
    finally:
        await asyncio.wait_for(manager.close(), 20)


@pytest.mark.asyncio
async def test_an_agent_write_at_the_old_path_gets_the_promoted_pointer(durable_scope, tmp_path, monkeypatch):
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_edit import EditFileTool
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool

    async def authorized(*, path, **kwargs):
        return AuthorizedPath(Path(path).resolve(), reviewed=False)
    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorized)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorized)
    # Nothing typed, so the promote cannot carry the identity and leaves the tombstone the pointer reads.
    manager, entry, workspace, thread_id, source, source_id = await _open(
        durable_scope, tmp_path, monkeypatch, True, typed=False)
    context = {"_thread_id": thread_id, "_thread_project_id": "project-alpha"}
    try:
        await _promote(workspace, thread_id, source)
        wrote = await WriteFileTool(workspace=workspace).execute(path=str(source), content="AGENT\n", **context)
        edited = await EditFileTool(workspace=workspace).execute(str(source), old_string="Launch", new_string="L", **context)
        for receipt in (wrote, edited):
            assert "was promoted to" in receipt and "planning/live.md" in receipt, receipt
            assert "Errno" not in receipt and str(workspace) not in receipt.split("promoted to")[0]
        assert not source.exists()
    finally:
        await asyncio.wait_for(manager.close(), 20)


@CARRIED_AND_STALE
@pytest.mark.asyncio
async def test_a_failed_save_moves_nothing(durable_scope, tmp_path, monkeypatch, stale):
    manager, entry, workspace, thread_id, source, source_id = await _open(durable_scope, tmp_path, monkeypatch, stale)
    before = source.read_text()
    entry.live.viewer_actor = lambda: None  # no writable viewer can save the typing
    try:
        receipt = await _promote(workspace, thread_id, source)
        assert receipt.startswith("Error: This document is open with changes that could not be saved yet")
        assert "Errno" not in receipt and str(workspace) not in receipt
        assert source.read_text() == before
        assert not (workspace / "projects" / "alpha" / "shared" / "planning" / "live.md").exists()
        assert manager.documents.get(source_id) is entry and entry.live.text.startswith("TYPED ")
    finally:
        await asyncio.wait_for(manager.close(), 20)


@CARRIED_AND_STALE
@pytest.mark.asyncio
async def test_a_tab_left_on_the_old_path_is_refused_and_recreates_nothing(durable_scope, tmp_path, monkeypatch, stale):
    from doceditor.live_lane import Operation

    manager, entry, workspace, thread_id, source, source_id = await _open(durable_scope, tmp_path, monkeypatch, stale)
    try:
        await _promote(workspace, thread_id, source)
        result = await entry.lane.submit(Operation("flush", actor=WriteActor(
            "ui", "project-alpha", direct_user=True, user_id="u1")))
        assert result.outcome == "refused" and "reopen" in str(result.refusal)
        assert not source.exists()
    finally:
        await asyncio.wait_for(manager.close(), 20)
