"""DCOL-FR13.3/13.5/13.6/13.7/13.9 on images inside a markdown document, user and agent."""

from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path

import pytest
from PIL import Image

from auth_fixtures import OWNER_TOKEN
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots  # noqa: F401 - autouse fixture
from registry.identity_metadata import sha256_file

pytestmark = pytest.mark.integration


def _png_bytes(color=(250, 250, 250), size=(640, 400)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


LOGIN = "![Login screen](img/login.png)"
CART = "![Cart](./img/cart.png)"


@pytest.fixture
def doc(integration_workspace):
    """A doc in its own folder showing two images by relative path, one of them twice."""
    folder = integration_workspace / f"review-{uuid.uuid4().hex[:8]}"
    (folder / "img").mkdir(parents=True)
    (folder / "img" / "login.png").write_bytes(_png_bytes())
    (folder / "img" / "cart.png").write_bytes(_png_bytes(size=(320, 200)))
    path = folder / "notes.md"
    path.write_text(f"# Review\n\nThe login screen:\n\n{LOGIN}\n\nThe cart:\n\n{CART}\n\nLogin again: {LOGIN}\n")
    yield path
    shutil.rmtree(folder, ignore_errors=True)


def _url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={OWNER_TOKEN}"


def _register(client, path: Path) -> str:
    r = client.post(_url("/api/files/register"), json={"path": str(path)})
    assert r.status_code == 200, r.text
    return r.json()["artifact"]["id"]


def _post(client, artifact_id: str, doc: Path, markdown: str, region: dict, *, occurrence: int = 0,
          body: str = "Fix this."):
    text = doc.read_text()
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(markdown, start + 1)
    return client.post(_url(f"/api/artifacts/{artifact_id}/comments"), json={
        "body": body, "region": region, "anchor_text": markdown, "from_off": start, "to_off": start + len(markdown),
    })


def _region_comment(client, artifact_id: str, doc: Path, markdown: str, region: dict, **kwargs) -> dict:
    r = _post(client, artifact_id, doc, markdown, region, **kwargs)
    assert r.status_code == 200, r.text
    return r.json()["comment"]


def _threads(client, artifact_id: str) -> dict[str, dict]:
    r = client.get(_url(f"/api/artifacts/{artifact_id}/comments"))
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["comments"]}


def test_a_region_on_a_doc_image_anchors_on_its_markdown_and_numbers_per_image(integration_client, doc):
    artifact_id = _register(integration_client, doc)
    login = doc.parent / "img" / "login.png"

    first = _region_comment(integration_client, artifact_id, doc, LOGIN, {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.2})
    cart = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.5, "y": 0.5})
    second = _region_comment(integration_client, artifact_id, doc, LOGIN, {"x": 0.6, "y": 0.6}, occurrence=1)

    assert first["anchor_text"] == LOGIN and first["from_off"] == doc.read_text().index(LOGIN)
    assert first["image_src"] == str(login)
    assert first["image_rev"] == sha256_file(login)
    assert cart["image_src"] == str(doc.parent / "img" / "cart.png")
    # One sequence per image file within the doc: the second showing of login.png continues it.
    assert [first["region_number"], cart["region_number"], second["region_number"]] == [1, 1, 2]
    listed = _threads(integration_client, artifact_id)
    assert listed[first["id"]]["region_stale"] is False
    assert listed[first["id"]].get("orphaned") is not True


def test_only_the_changed_image_reads_stale(integration_client, doc):
    artifact_id = _register(integration_client, doc)
    login = _region_comment(integration_client, artifact_id, doc, LOGIN, {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2})
    cart = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.3, "y": 0.3})

    doc.write_text(doc.read_text().replace("# Review", "# Review, edited"))
    assert not any(c["region_stale"] for c in _threads(integration_client, artifact_id).values())

    (doc.parent / "img" / "cart.png").write_bytes(_png_bytes(color=(10, 10, 10), size=(320, 200)))
    listed = _threads(integration_client, artifact_id)
    assert listed[login["id"]]["region_stale"] is False
    assert listed[cart["id"]]["region_stale"] is True


def test_removing_the_image_from_the_doc_orphans_its_region_comments(integration_client, doc):
    artifact_id = _register(integration_client, doc)
    cart = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.3, "y": 0.3})

    doc.write_text(doc.read_text().replace(CART, "(image removed)"))

    assert _threads(integration_client, artifact_id)[cart["id"]]["orphaned"] is True


def test_a_moved_doc_pin_is_placed_on_the_current_image(integration_client, doc):
    artifact_id = _register(integration_client, doc)
    comment = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2})
    cart = doc.parent / "img" / "cart.png"
    cart.write_bytes(_png_bytes(color=(10, 10, 10), size=(320, 200)))

    r = integration_client.patch(
        _url(f"/api/comments/{comment['id']}"), json={"region": {"x": 0.25, "y": 0.25, "w": 0.2, "h": 0.2}},
    )

    assert r.status_code == 200, r.text
    moved = _threads(integration_client, artifact_id)[comment["id"]]
    assert moved["region_stale"] is False
    assert moved["image_rev"] == sha256_file(cart)


@pytest.mark.parametrize(
    ("text", "markdown", "detail"),
    [
        ("Plain words here.\n", "Plain words", "image"),
        ("![Remote](https://example.com/a.png)\n", "![Remote](https://example.com/a.png)", "web image"),
        ("![Gone](img/missing.png)\n", "![Gone](img/missing.png)", "could not be found"),
        ("![Notes](notes.md)\n", "![Notes](notes.md)", "not an image"),
        ("![Folder](img/folder.png)\n", "![Folder](img/folder.png)", "not a file"),
    ],
)
def test_a_doc_region_needs_an_image_stored_in_the_workspace(integration_client, doc, text, markdown, detail):
    (doc.parent / "img" / "folder.png").mkdir()
    doc.write_text(text)
    artifact_id = _register(integration_client, doc)

    r = _post(integration_client, artifact_id, doc, markdown, {"x": 0.1, "y": 0.1})

    assert r.status_code == 400, r.text
    assert detail in r.text


def test_a_doc_region_without_its_image_is_refused(integration_client, doc):
    artifact_id = _register(integration_client, doc)

    r = integration_client.post(
        _url(f"/api/artifacts/{artifact_id}/comments"), json={"body": "x", "region": {"x": 0.1, "y": 0.1}},
    )

    assert r.status_code == 400
    assert "quote" in r.text and "image" in r.text


@pytest.mark.asyncio
async def test_agent_region_on_a_doc_image_named_by_its_path_returns_that_image_marked(integration_client, doc):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")

    result = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="img/cart.png", region={"x": 0.4, "y": 0.3, "w": 0.2, "h": 0.2},
        body="The total is cut off.", _thread_id=sid,
    )

    media = getattr(result, "media", None)
    assert media is not None, result
    with Image.open(media.path) as img:
        assert img.size == (320, 200)
    payload = json.loads(str(result).split("\n", 1)[0])
    comment = payload["comment"]
    assert comment["anchor_text"] == CART
    assert comment["from_off"] == doc.read_text().index(CART)
    assert comment["image_src"] == str(doc.parent / "img" / "cart.png")
    assert comment["region_number"] == 1
    assert payload["marked_image"] == media.path


@pytest.mark.asyncio
async def test_agent_picks_one_showing_of_a_repeated_image_with_near(integration_client, doc):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")

    ambiguous = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="img/login.png", region={"x": 0.1, "y": 0.1}, body="b", _thread_id=sid,
    )
    picked = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="img/login.png", near="Login again", region={"x": 0.1, "y": 0.1},
        body="b", _thread_id=sid,
    )

    assert str(ambiguous).startswith("Error"), ambiguous
    comment = json.loads(str(picked).split("\n", 1)[0])["comment"]
    assert comment["from_off"] == doc.read_text().rindex(LOGIN)


@pytest.mark.asyncio
async def test_agent_doc_region_quote_must_be_inside_an_image(integration_client, doc):
    from tools.documents import DocCommentCreateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")

    outside = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="The cart:", region={"x": 0.1, "y": 0.1}, body="b", _thread_id=sid,
    )
    bare = await DocCommentCreateTool().execute(doc_path=str(doc), region={"x": 0.1, "y": 0.1}, body="b", _thread_id=sid)

    assert outside.startswith("Error") and "image" in outside
    assert bare.startswith("Error") and "quote" in bare


@pytest.mark.asyncio
async def test_agent_moves_its_doc_image_box_and_gets_that_image_again(integration_client, doc):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")
    created = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=CART, region={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, body="Here.", _thread_id=sid,
    )
    comment_id = json.loads(str(created).split("\n", 1)[0])["comment"]["id"]

    moved = await DocCommentUpdateTool().execute(
        comment_id=comment_id, region={"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}, _thread_id=sid,
    )

    media = getattr(moved, "media", None)
    assert media is not None, moved
    with Image.open(media.path) as img:
        assert img.size == (320, 200)
    assert json.loads(str(moved).split("\n", 1)[0])["comment"]["region_x"] == 0.5


def _outside_png(tmp_path_factory) -> Path:
    """An image outside every root the document's owner may read."""
    target = tmp_path_factory.mktemp("outside-scope") / "secret.png"
    target.write_bytes(_png_bytes(color=(200, 0, 0), size=(320, 200)))
    return target


def test_a_doc_pin_does_not_move_onto_an_image_now_outside_the_scope(integration_client, doc, tmp_path_factory):
    """The stored image path is a name, not a grant: it passes the scope check again on every read."""
    artifact_id = _register(integration_client, doc)
    comment = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.2, "y": 0.2})
    cart = doc.parent / "img" / "cart.png"
    cart.unlink()
    cart.symlink_to(_outside_png(tmp_path_factory))

    r = integration_client.patch(_url(f"/api/comments/{comment['id']}"), json={"region": {"x": 0.5, "y": 0.5}})

    assert r.status_code == 400, r.text
    assert "outside this document's scope" in r.text


def test_listing_comments_never_reads_an_image_now_outside_the_scope(integration_client, doc, tmp_path_factory,
                                                                      monkeypatch):
    """Judging a pin stale reads its image, so the list passes the same scope check as every other read."""
    from doceditor import image_regions

    artifact_id = _register(integration_client, doc)
    comment = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.2, "y": 0.2})
    cart = doc.parent / "img" / "cart.png"
    outside = _outside_png(tmp_path_factory)
    cart.unlink()
    cart.symlink_to(outside)
    read = []
    real = image_regions.sha256_file
    monkeypatch.setattr(image_regions, "sha256_file", lambda path: read.append(Path(path).resolve()) or real(path))

    listed = _threads(integration_client, artifact_id)[comment["id"]]

    assert outside.resolve() not in read
    assert listed["region_stale"] is False


def test_a_region_comment_stays_on_its_image(integration_client, doc):
    """Its box is in that image's coordinates: an anchor that names another image (or none) is refused."""
    artifact_id = _register(integration_client, doc)
    comment = _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.2, "y": 0.2})
    text = doc.read_text()

    def reanchor(markdown: str, start: int):
        return integration_client.patch(_url(f"/api/comments/{comment['id']}"), json={
            "anchor_text": markdown, "from_off": start, "to_off": start + len(markdown)})

    other = reanchor(LOGIN, text.index(LOGIN))
    assert other.status_code == 400, other.text
    assert "stays on its image" in other.text
    words = reanchor("The cart:", text.index("The cart:"))
    assert words.status_code == 400, words.text
    same = reanchor(CART, text.index(CART))
    assert same.status_code == 200, same.text
    assert _threads(integration_client, artifact_id)[comment["id"]]["image_src"] == str(doc.parent / "img" / "cart.png")


@pytest.mark.asyncio
async def test_agent_region_comment_stays_on_its_image(integration_client, doc):
    from tools.documents import DocCommentCreateTool, DocCommentUpdateTool

    sid = _mk_thread(integration_client, _OWNER, title="Design Reviewer")
    created = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=CART, region={"x": 0.1, "y": 0.1}, body="Here.", _thread_id=sid,
    )
    comment_id = json.loads(str(created).split("\n", 1)[0])["comment"]["id"]
    start = doc.read_text().index(LOGIN)

    moved = await DocCommentUpdateTool().execute(
        comment_id=comment_id, anchor_text=LOGIN, from_off=start, to_off=start + len(LOGIN), _thread_id=sid,
    )

    assert str(moved).startswith("Error:") and "stays on its image" in str(moved), moved


def _project_root(client, integration_workspace) -> tuple[str, Path]:
    from core.workspace_layout import resolve_project_root
    from infra.database import get_db

    r = client.post(_url("/api/projects"), json={"name": f"Regions {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 200, r.text
    project_id = r.json()["project"]["id"]
    db = get_db()
    try:
        root = resolve_project_root(integration_workspace, project_id, db=db)
    finally:
        db.close()
    assert root is not None
    root.mkdir(parents=True, exist_ok=True)
    return project_id, root


@pytest.mark.asyncio
async def test_a_project_document_never_reads_an_image_in_another_project(integration_client, integration_workspace):
    """A document's images are read in its reader's scope, as the file route reads them: one project, never another."""
    from tools.documents import DocCommentCreateTool

    project, root = _project_root(integration_client, integration_workspace)
    _, other_root = _project_root(integration_client, integration_workspace)
    secret = other_root / "secret.png"
    secret.write_bytes(_png_bytes(color=(200, 0, 0), size=(320, 200)))
    doc = root / "notes.md"
    doc.write_text(f"# Notes\n\n![Secret]({secret})\n")
    r = integration_client.post(_url("/api/threads"), json={"title": "Design Reviewer", "project_id": project})
    assert r.status_code == 200, r.text

    created = await DocCommentCreateTool().execute(
        doc_path=str(doc), quote=str(secret), region={"x": 0.1, "y": 0.1}, body="b", _thread_id=r.json()["thread_id"],
    )

    assert str(created).startswith("Error") and "outside" in str(created), created


def test_listing_scopes_its_reader_once_however_many_images(integration_client, doc, monkeypatch):
    """The reader's roots are the same for every image in one list, so they are resolved once."""
    from doceditor import document_scope, image_regions

    artifact_id = _register(integration_client, doc)
    _region_comment(integration_client, artifact_id, doc, LOGIN, {"x": 0.1, "y": 0.1})
    _region_comment(integration_client, artifact_id, doc, CART, {"x": 0.3, "y": 0.3})
    calls = []
    real = document_scope.scope_bases
    monkeypatch.setattr(image_regions, "scope_bases", lambda *a, **k: calls.append(a) or real(*a, **k), raising=False)

    _threads(integration_client, artifact_id)

    assert len(calls) == 1
