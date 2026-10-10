"""A streamed edit is previewed only where the write itself would be allowed.

The preview passes the same two gates as the tool: the ordinary path boundary
and the document's owner-only rule (DCOL-FR5.9). A refused call never shows.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute
from tests.test_owner_only_edit import _shared_doc
from tests.test_shared_document_ownership import ownership_scope as ownership_scope  # noqa: F401 - the fixture


@pytest.fixture
def opened(ownership_scope, tmp_path, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ownership_scope["workspace"]])
    monkeypatch.setattr(live_preview, "FLUSH_SECONDS", 0)
    lives = []

    async def open_live(path: Path):
        from doceditor.live_document import LiveDocument
        from doceditor.live_runtime import LiveDocuments
        from doceditor.live_shared_policy import SharedLivePolicy

        live = await LiveDocument.open(path, tmp_path / f"store-{len(lives)}.sqlite")
        lives.append(live)
        db_path = Path(ownership_scope["db"].execute("PRAGMA database_list").fetchone()[2])
        owner = LiveDocuments(tmp_path, workspace=ownership_scope["workspace"], db_path=db_path)
        frames: list[dict] = []
        monkeypatch.setattr(live, "preview_changed", frames.append)
        entry = SimpleNamespace(live=live, policy=SharedLivePolicy(live, owner=owner))
        monkeypatch.setattr("doceditor.live_tools.find_live", lambda target: entry if target == path.resolve() else None)
        return frames

    yield open_live
    for live in lives:
        live.close()


async def _stream(scope, actor, path: Path) -> None:
    route = PreviewRoute()
    route.start("call-1", thread_id=scope[actor], turn_id="t", tool="edit_file", workspace=scope["workspace"])
    raw = json.dumps({"path": str(path), "old_string": "beta", "new_string": "streamed text"})
    for index in range(0, len(raw), 6):
        route.update("call-1", raw[index:index + 6])
        for _ in range(5):
            await asyncio.sleep(0.01)
    route.end("call-1", "failed")


def _previewed(frames) -> bool:
    return any(frame["type"] == "doc_preview" for frame in frames)


async def test_the_owner_sees_its_own_edit_typed(ownership_scope, opened):
    path, _ = _shared_doc(ownership_scope)
    frames = await opened(path)
    await _stream(ownership_scope, "owner", path)
    assert _previewed(frames)


async def test_a_non_owner_agent_edit_is_never_previewed(ownership_scope, opened):
    path, _ = _shared_doc(ownership_scope)
    frames = await opened(path)
    await _stream(ownership_scope, "other", path)
    assert frames == []


async def test_a_path_outside_the_agent_boundary_is_never_previewed(ownership_scope, opened, tmp_path):
    outside = tmp_path / "outside" / "doc.md"
    outside.parent.mkdir()
    outside.write_text("alpha beta gamma\n", encoding="utf-8")
    frames = await opened(outside)
    await _stream(ownership_scope, "owner", outside)
    assert frames == []
