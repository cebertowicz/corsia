"""A region on an image in a live document tracks the image's markdown like any live comment range."""
import asyncio
import io
import json

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from api import doc_comments
from auth_fixtures import OWNER_TOKEN
from infra import app_loop
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import typing, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tools.documents import DocCommentCreateTool

MARKDOWN = "![Shot](img/shot.png)"


async def _live_doc_with_image(scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    image = entry.live.path.parent / "img" / "shot.png"
    image.parent.mkdir()
    out = io.BytesIO()
    Image.new("RGB", (200, 100), (240, 240, 240)).save(out, format="PNG")
    image.write_bytes(out.getvalue())
    typing(entry, len("a b\n"), insert=f"{MARKDOWN}\n")
    app_loop.capture(asyncio.get_running_loop())
    return manager, entry, image


@pytest.mark.asyncio
async def test_agent_region_in_a_live_doc_tracks_the_whole_image_markdown(ownership_scope, tmp_path, monkeypatch):
    manager, entry, image = await _live_doc_with_image(ownership_scope, tmp_path, monkeypatch)
    try:
        async def call():
            return await DocCommentCreateTool(workspace=ownership_scope["workspace"]).execute(
                body="Cropped.", _thread_id=ownership_scope["owner"], artifact_id=entry.artifact_id,
                quote="img/shot.png", region={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, client_id="agent-box")

        result = await asyncio.to_thread(lambda: asyncio.run(call()))

        comment = json.loads(str(result).split("\n", 1)[0])["comment"]
        assert comment["anchor_text"] == MARKDOWN and comment["image_src"] == str(image)
        assert entry.live.range_text("agent-box") == MARKDOWN
        typing(entry, 0, insert="New intro. ")
        assert entry.live.range_text("agent-box") == MARKDOWN
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_browser_region_in_a_live_doc_adopts_its_captured_image_range(ownership_scope, tmp_path, monkeypatch):
    manager, entry, image = await _live_doc_with_image(ownership_scope, tmp_path, monkeypatch)
    live = entry.live
    start = live.text.index(MARKDOWN)
    end = start + len(MARKDOWN)
    assert live.reserve("user-box", start, end, user_id(ownership_scope), object(), vector=live.doc.get_state(),
                        digest=live.digest, length=len(live.text.encode("utf-16-le")) // 2)
    app = FastAPI()
    app.include_router(doc_comments.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/artifacts/{entry.artifact_id}/comments", params={"token": OWNER_TOKEN},
                json=dict(body="Too dark.", anchor_text=MARKDOWN, from_off=start, to_off=end, live_capture=True,
                          client_id="user-box", region={"x": 0.2, "y": 0.2}))

        assert response.status_code == 200, response.text
        comment = response.json()["comment"]
        assert comment["image_src"] == str(image) and comment["region_number"] == 1
        assert live.range_text("user-box") == MARKDOWN
    finally:
        await manager.close()
        app_loop.clear()
