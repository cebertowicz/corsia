"""A cancelled started file call must be safe to rerun after it lands late."""
import asyncio

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["write", "edit", "multi_edit"])
async def test_cancelled_started_file_call_lands_then_same_call_is_not_duplicated(ownership_scope, tmp_path, monkeypatch, tool_name):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    expected = "a B\nsecond\n"
    if tool_name == "write":
        tool = WriteFileTool(workspace=scope["workspace"])
        arguments = {"path": path, "content": expected}
    elif tool_name == "edit":
        tool = EditFileTool(workspace=scope["workspace"])
        arguments = {"path": path, "old_string": "a b", "new_string": "a B"}
    else:
        tool = MultiEditTool(workspace=scope["workspace"])
        arguments = {"path": path, "edits": [{"old_string": "a b", "new_string": "a B"}]}
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    call = asyncio.create_task(tool.execute(**arguments, **context))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        call.cancel("provider interruption")
        with pytest.raises(asyncio.CancelledError):
            await call
        assert not entry.lane._worker.done()
        release.set()
        await asyncio.shield(entry.lane._worker)
        assert entry.live.text == entry.live.path.read_text() == expected
        assert entry.live.store.pending() is None
        receipt = await tool.execute(**arguments, **context)
        assert entry.live.text == entry.live.path.read_text() == expected
        assert "No changes needed" in receipt or receipt.startswith("Error")
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_actor_between", [False, True])
async def test_normal_identical_write_rerun_diffs_from_authors_own_landed_base(ownership_scope, tmp_path, monkeypatch, other_actor_between):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.filesystem_edit import EditFileTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    reader = ReadFileTool(workspace=scope["workspace"])
    writer = WriteFileTool(workspace=scope["workspace"])
    own_text = "a B\nsecond\n"
    try:
        await reader.execute(path, **context)
        result = await writer.execute(path, content=own_text, **context)
        assert not result.startswith("Error")
        assert entry.live.text == entry.live.path.read_text() == own_text
        expected = own_text
        if other_actor_between:
            other = {**context, "_thread_id": scope["conductor"]}
            result = await EditFileTool(workspace=scope["workspace"]).execute(
                path, old_string="second", new_string="other actor", **other)
            assert not result.startswith("Error")
            expected = "a B\nother actor\n"
            assert entry.live.text == entry.live.path.read_text() == expected
        result = await writer.execute(path, content=own_text, **context)
        assert entry.live.text == entry.live.path.read_text() == expected
        assert "No changes needed" in result
    finally:
        await manager.close()
