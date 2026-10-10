"""Preserve the existing canonical failure order when memory publication yields."""
import inspect

import pytest
from pycrdt import Doc

from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_text import apply_text, clone
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["durable", "live"])
async def test_apply_committed_failure_preserves_existing_partial_state(tmp_path, monkeypatch, failing):
    path = tmp_path / "document.md"
    path.write_text("old")
    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    fork = clone(live.doc.get_update())
    apply_text(fork, "new")
    update = fork.get_update(live.doc.get_state())
    bases = dict(live.bases)
    original = Doc.apply_update
    target = live.durable if failing == "durable" else live.doc
    def refused(doc, data):
        if doc is target:
            raise ValueError("memory apply refused")
        return original(doc, data)
    monkeypatch.setattr(Doc, "apply_update", refused)
    try:
        with pytest.raises(ValueError, match="memory apply refused"):
            result = live.apply_committed(update, fork.get_update(), file_hash(b"new"))
            if inspect.isawaitable(result):
                await result
        assert str(live.doc["text"]) == "old"
        assert str(live.durable["text"]) == ("old" if failing == "durable" else "new")
        assert live.bases == bases
    finally:
        live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["durable", "live"])
async def test_memory_apply_failure_after_author_advance_keeps_receipt_and_partial_state(ownership_scope, tmp_path, monkeypatch, failing):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = str(entry.live.path)
    expected = "a B\nsecond\n"
    advanced = file_hash(expected.encode())
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    original = Doc.apply_update
    target = entry.live.durable if failing == "durable" else entry.live.doc
    observed = []
    def refused(doc, data):
        if doc is target:
            observed.append(True)
            assert entry.live.read_bases[scope["owner"]] == advanced
            assert entry.live.path.read_text() == expected
            raise ValueError("memory apply refused")
        return original(doc, data)
    monkeypatch.setattr(Doc, "apply_update", refused)
    try:
        receipt = await WriteFileTool(workspace=scope["workspace"]).execute(path, expected, **context)
        assert receipt == "Error writing file: memory apply refused"
        assert observed == [True]
        assert entry.live.text == "a b\nsecond\n"
        assert str(entry.live.durable["text"]) == ("a b\nsecond\n" if failing == "durable" else expected)
        # Promotion already recorded disk landing; memory failure preserves
        # that durable promotion and the existing tool-level error receipt.
        assert entry.live.store.pending() is None
        assert entry.live.read_bases[scope["owner"]] == advanced
        assert str(clone(entry.live.bases[advanced])["text"]) == expected
        persisted = clone(entry.live.store.load_updates()[0])
        for update in entry.live.store.load_updates()[1:]:
            persisted.apply_update(update)
        assert str(persisted["text"]) == expected
    finally:
        await manager.close()
