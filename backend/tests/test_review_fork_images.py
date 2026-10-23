"""A review fork never runs on a document that shows images, or one it cannot read as text."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core import artifact_review_runtime as runtime
from core.fork_runtime import handles
from doceditor.doc_text import MAX_DOC_BYTES
from plans import public
from prompts.artifact_review import continuation
from test_doc_review_requests import request, review_env, stored  # noqa: F401

HAS_IMAGES = (
    "Review update: A separate review could not start because the document has images. "
    "Review the document here instead: open each image with read_file, then comment and call "
    "doc_feedback_send. If an image cannot be opened, say which one in a comment."
)
UNREADABLE = (
    "Review update: A separate review could not start because the document could not be read as text. "
    "Review the document here instead, then call doc_feedback_send."
)


def _document(db, artifact_id, tmp_path, content: str | bytes):
    document = tmp_path / "design.md"
    document.write_bytes(content if isinstance(content, bytes) else content.encode())
    db.execute("UPDATE artifact_identities SET path=? WHERE id=?", (str(document), artifact_id))
    db.commit()
    return document


@pytest.fixture
def cold(monkeypatch):
    """A cold request that builds, so only the document check can stop a start; a started fork records here."""
    built = []

    def build(request_id):
        built.append(request_id)
        row = {"fork_id": f"fork-{request_id}"}
        return row, SimpleNamespace(fork_id=row["fork_id"], thread_id="reviewer")

    async def idle(*_args):
        return None

    monkeypatch.setattr(runtime, "_cold_request", build)
    monkeypatch.setattr(runtime, "_run", idle)
    yield built
    for fork_id in [key for key in handles if key.startswith("fork-")]:
        handles.pop(fork_id, None)


async def _handed_back(db, row, reason, copy):
    stored_row = stored(db, row["id"])
    assert (stored_row["state"], stored_row["reason"]) == ("handed_back", reason)
    assert stored_row["fork_id"] is None
    note = public.get_node(stored_row["node_id"], db=db).description
    assert copy == continuation(0, reason) and note.endswith("Check claims\n\n" + copy)


@pytest.mark.asyncio
async def test_a_saved_document_with_an_image_hands_back_and_builds_no_fork(review_env, tmp_path, cold):
    db, aid = review_env
    _document(db, aid, tmp_path, "# Design\n\n![a](f.png)\n")
    row = request(db, aid)

    await runtime.start(row["id"])

    await _handed_back(db, row, "document_has_images", HAS_IMAGES)
    assert row["fork_id"] not in handles and f"fork-{row['id']}" not in handles


@pytest.mark.asyncio
async def test_an_image_only_in_the_live_snapshot_hands_back(review_env, tmp_path, cold, monkeypatch):
    db, aid = review_env
    document = _document(db, aid, tmp_path, "# Design\n\nNo images saved.\n")
    live = SimpleNamespace(live=SimpleNamespace(snapshot=("# Design\n\n![new](new.png)\n", None)))
    monkeypatch.setattr("doceditor.live_tools.find_live", lambda path: live if path == document else None)
    row = request(db, aid)

    await runtime.start(row["id"])

    await _handed_back(db, row, "document_has_images", HAS_IMAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "| a | b |\n|---|---|\n| <IMG src=x> | y |\n",
    # An accepted false positive: the editor draws no image inside a fenced block, but the check is a
    # superset and would rather send the review back than miss one.
    "```md\n![sample](f.png)\n```\n",
    "| a |\n|---|\n| <svg><image href=x.png/></svg> |\n",
    "| a |\n|---|\n| <div style=\"background:URL(x.png)\">z</div> |\n",
])
async def test_other_image_forms_hand_back(review_env, tmp_path, cold, text):
    db, aid = review_env
    _document(db, aid, tmp_path, text)
    row = request(db, aid)

    await runtime.start(row["id"])

    await _handed_back(db, row, "document_has_images", HAS_IMAGES)


@pytest.mark.asyncio
async def test_a_text_only_document_forks_as_on_dev(review_env, tmp_path, cold):
    db, aid = review_env
    _document(db, aid, tmp_path, "# Design\n\nA table:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    row = request(db, aid)

    await runtime.start(row["id"])
    fork_id = f"fork-{row['id']}"

    assert fork_id in handles
    assert stored(db, row["id"])["state"] == "forked" and stored(db, row["id"])["node_id"] is None
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", b"caf\xe9 au lait", b"a" * (MAX_DOC_BYTES + 1)],
                         ids=["missing", "binary", "not-utf8", "oversize"])
async def test_a_document_that_cannot_be_read_as_text_hands_back(review_env, tmp_path, cold, content):
    db, aid = review_env
    document = _document(db, aid, tmp_path, b"" if content is None else content)
    if content is None:
        document.unlink()
    row = request(db, aid)

    await runtime.start(row["id"])

    await _handed_back(db, row, "document_unreadable", UNREADABLE)


@pytest.mark.asyncio
async def test_a_document_without_a_path_hands_back_as_unreadable(review_env, cold):
    db, aid = review_env
    db.execute("UPDATE artifact_identities SET path=NULL WHERE id=?", (aid,))
    db.commit()
    row = request(db, aid)

    await runtime.start(row["id"])

    await _handed_back(db, row, "document_unreadable", UNREADABLE)


@pytest.mark.asyncio
async def test_a_missing_artifact_row_still_fails_instead_of_handing_back(review_env, monkeypatch):
    from dataclasses import replace

    from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND

    db, aid = review_env
    row = request(db, aid)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM artifact_identities WHERE id=?", (aid,))
    db.commit()
    ended = []

    async def handler(request_id, error=None, **fields):
        ended.append((error, fields))

    monkeypatch.setattr(runtime, "ARTIFACT_REVIEW_KIND", replace(ARTIFACT_REVIEW_KIND, limit_or_failure_handler=handler))

    await runtime.start(row["id"])

    [(error, fields)] = ended
    # The ordinary failure path: the error carries the cause and no hand-back state is forced.
    assert str(error) == "thread_or_artifact" and fields["reason"] == "thread_or_artifact" and "state" not in fields
