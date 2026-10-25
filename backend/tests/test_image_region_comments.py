"""DCOL-FR13.3/13.4/13.7/13.9: region comments on image artifacts, user and agent."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from auth_fixtures import OWNER_TOKEN
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots  # noqa: F401 - autouse fixture

pytestmark = pytest.mark.integration


def _png_bytes(color=(250, 250, 250), size=(640, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def image_file(integration_workspace):
    p = integration_workspace / f"mockup-{uuid.uuid4().hex[:8]}.png"
    p.write_bytes(_png_bytes())
    yield p
    p.unlink(missing_ok=True)


def _url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={OWNER_TOKEN}"


def _register(client, path: Path, thread_id: str | None = None) -> str:
    body = {"path": str(path)}
    if thread_id:
        body["thread_id"] = thread_id
    r = client.post(_url("/api/files/register"), json=body)
    assert r.status_code == 200, r.text
    return r.json()["artifact"]["id"]


def _region_comment(client, artifact_id: str, region: dict, body: str = "Fix this.") -> dict:
    r = client.post(_url(f"/api/artifacts/{artifact_id}/comments"), json={"delivery_policy": "immediate", "body": body, "region": region})
    assert r.status_code == 200, r.text
    return r.json()["comment"]


def _threads(client, artifact_id: str) -> list[dict]:
    r = client.get(_url(f"/api/artifacts/{artifact_id}/comments"))
    assert r.status_code == 200, r.text
    return r.json()["comments"]


def test_user_region_comment_round_trips_with_its_number_and_image_revision(integration_client, image_file):
    artifact_id = _register(integration_client, image_file)

    box = _region_comment(integration_client, artifact_id, {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25})
    point = _region_comment(integration_client, artifact_id, {"x": 0.6, "y": 0.5})

    assert (box["region_x"], box["region_y"], box["region_w"], box["region_h"]) == (0.1, 0.2, 0.3, 0.25)
    assert (point["region_w"], point["region_h"]) == (0.0, 0.0)
    assert [box["region_number"], point["region_number"]] == [1, 2]
    assert box["image_rev"] and box["image_rev"] == point["image_rev"]
    assert box["anchor_text"] is None

    listed = {c["id"]: c for c in _threads(integration_client, artifact_id)}
    assert listed[box["id"]]["region_number"] == 1
    assert listed[box["id"]]["region_stale"] is False


def test_numbers_are_per_image_and_never_reused_after_resolve(integration_client, image_file, integration_workspace):
    artifact_id = _register(integration_client, image_file)
    other = integration_workspace / f"other-{uuid.uuid4().hex[:8]}.png"
    other.write_bytes(_png_bytes())
    other_id = _register(integration_client, other)

    first = _region_comment(integration_client, artifact_id, {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1})
    second = _region_comment(integration_client, artifact_id, {"x": 0.3, "y": 0.3, "w": 0.1, "h": 0.1})
    r = integration_client.patch(_url(f"/api/comments/{second['id']}"), json={"resolved": True})
    assert r.status_code == 200, r.text
    third = _region_comment(integration_client, artifact_id, {"x": 0.5, "y": 0.5})
    elsewhere = _region_comment(integration_client, other_id, {"x": 0.5, "y": 0.5})

    assert [first["region_number"], second["region_number"], third["region_number"]] == [1, 2, 3]
    assert elsewhere["region_number"] == 1
    reply = integration_client.post(
        _url(f"/api/artifacts/{artifact_id}/comments"), json={"delivery_policy": "immediate", "body": "Agreed.", "parent_id": first["id"]},
    ).json()["comment"]
    assert reply["region_number"] is None
    other.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"body": "x", "region": {"x": 1.2, "y": 0.1}}, "region"),
        ({"body": "x", "region": {"x": 0.9, "y": 0.1, "w": 0.2, "h": 0.1}}, "region"),
        ({"body": "x", "region": {"x": 0.1, "y": 0.1, "w": -0.1, "h": 0.1}}, "region"),
        ({"body": "x", "region": {"x": 0.1, "y": 0.1}, "review": True}, "region"),
        ({"body": "x", "region": {"x": 0.1, "y": 0.1}, "anchor_text": "q", "from_off": 0, "to_off": 1}, "region"),
    ],
)
def test_malformed_region_requests_are_refused(integration_client, image_file, body, detail):
    artifact_id = _register(integration_client, image_file)

    r = integration_client.post(_url(f"/api/artifacts/{artifact_id}/comments"), json={**body, "delivery_policy": "immediate"})

    assert r.status_code in (400, 422), r.text
    assert detail in r.text.lower()


def test_region_on_a_text_document_is_refused(integration_client, integration_workspace):
    doc = integration_workspace / f"notes-{uuid.uuid4().hex[:8]}.md"
    doc.write_text("# Notes\n\nBody.\n")
    artifact_id = _register(integration_client, doc)

    r = integration_client.post(
        _url(f"/api/artifacts/{artifact_id}/comments"), json={"delivery_policy": "immediate", "body": "x", "region": {"x": 0.1, "y": 0.1}},
    )

    assert r.status_code == 400
    assert "image" in r.text.lower()
    doc.unlink(missing_ok=True)


def test_pin_reads_stale_after_the_image_changes_and_fresh_after_a_move(integration_client, image_file):
    artifact_id = _register(integration_client, image_file)
    comment = _region_comment(integration_client, artifact_id, {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2})

    image_file.write_bytes(_png_bytes(color=(10, 10, 10)))
    stale = {c["id"]: c for c in _threads(integration_client, artifact_id)}[comment["id"]]
    assert stale["region_stale"] is True

    r = integration_client.patch(
        _url(f"/api/comments/{comment['id']}"), json={"region": {"x": 0.25, "y": 0.25, "w": 0.2, "h": 0.2}},
    )
    assert r.status_code == 200, r.text
    moved = {c["id"]: c for c in _threads(integration_client, artifact_id)}[comment["id"]]
    assert moved["region_stale"] is False
    assert moved["region_x"] == 0.25
    assert moved["region_number"] == 1


@pytest.mark.asyncio
async def test_agent_region_comment_returns_the_marked_up_image(integration_client, image_file):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")
    artifact_id = _register(integration_client, image_file)

    result = await DocCommentCreateTool().execute(
        artifact_id=artifact_id,
        region={"x": 0.4, "y": 0.3, "w": 0.2, "h": 0.2},
        body="This button is clipped.",
        _thread_id=sid,
    )

    media = getattr(result, "media", None)
    assert media is not None, result
    assert media.kind == "image" and media.mime_type == "image/png"
    rendered = Path(media.path)
    assert rendered.is_file()
    assert f"/.threads/{sid}/working/" in str(rendered)
    with Image.open(rendered) as img:
        assert img.size == (640, 400)
    payload = json.loads(str(result).split("\n", 1)[0])
    assert payload["comment"]["region_number"] == 1
    assert payload["comment"]["author_thread_id"] == sid
    assert payload["marked_image"] == str(rendered)


@pytest.mark.asyncio
async def test_agent_moves_its_own_box_and_gets_the_image_again(integration_client, image_file):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")
    artifact_id = _register(integration_client, image_file)
    created = await DocCommentCreateTool().execute(
        artifact_id=artifact_id, region={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, body="Here.", _thread_id=sid,
    )
    comment_id = json.loads(str(created).split("\n", 1)[0])["comment"]["id"]

    moved = await DocCommentUpdateTool().execute(
        comment_id=comment_id, region={"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}, _thread_id=sid,
    )

    assert getattr(moved, "media", None) is not None, moved
    payload = json.loads(str(moved).split("\n", 1)[0])
    assert payload["comment"]["region_x"] == 0.5
    assert payload["comment"]["region_number"] == 1


@pytest.mark.asyncio
async def test_agent_region_needs_an_image_and_no_quote(integration_client, image_file):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")
    artifact_id = _register(integration_client, image_file)

    quoted = await DocCommentCreateTool().execute(
        artifact_id=artifact_id, region={"x": 0.1, "y": 0.1}, quote="x", body="b", _thread_id=sid,
    )
    missing = await DocCommentCreateTool().execute(artifact_id=artifact_id, body="b", _thread_id=sid)

    assert quoted.startswith("Error") and "region" in quoted
    assert missing.startswith("Error") and "region" in missing
