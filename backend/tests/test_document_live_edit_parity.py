"""Live and closed-file tools share exact matching errors and success receipts."""
import json
import pytest
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


def receipt(value):
    try:
        return json.loads(value)["result"]
    except (ValueError, KeyError):
        return value


@pytest.mark.asyncio
@pytest.mark.parametrize("multi,edits", [
    (False, [{"old_string": "absent", "new_string": "x"}]),
    (False, [{"old_string": "\n", "new_string": "x"}]),
    (False, [{"old_string": "b", "new_string": "B"}]),
    (False, [{"old_string": "\n", "new_string": "!", "replace_all": True}]),
    (True, [{"old_string": "b", "new_string": "B"}, {"old_string": "absent", "new_string": "x"}]),
    (True, [{"old_string": "\n", "new_string": "x"}]),
    (True, [{"old_string": "b", "new_string": "b"}]),
    (True, [{"old_string": "b", "new_string": "B"}]),
])
async def test_live_edit_matches_legacy_receipt(ownership_scope, tmp_path, monkeypatch, multi, edits):
    from tools import filesystem_edit
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    def plain_write(path, text, kwargs, *, success, **options):
        path.write_text(text)
        return success
    monkeypatch.setattr(filesystem_edit, "managed_write", plain_write)
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = entry.live.path
    text = path.read_text()
    tool = (MultiEditTool if multi else EditFileTool)(workspace=scope["workspace"])
    args = {"edits": edits} if multi else edits[0]
    try:
        live_result = await tool.execute(str(path), **args, **context)
        live_text = path.read_text()
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        path.write_text(text)
        legacy_result = await tool.execute(str(path), **args, **context)
        assert receipt(live_result) == receipt(legacy_result)
        assert live_text == path.read_text()
    finally:
        await manager.close()
