"""A comment keeps its place when the text inside its highlight changes; only a collapsed range orphans."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, comment_anchoring, comments as store
from doceditor.quote_anchor import anchor_context
from infra.database import get_db, write_transaction
from tests.test_document_live_comments import _edit
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import typing
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

QUOTE = "First part. Middle sentence here. Last part."
BODY = f"# Doc\n\nIntro line.\n\n{QUOTE}\n\nTail line.\n"


def _comment_on(scope, entry, quote=QUOTE, *, replacement=None):
    """One comment on ``quote`` in the live document, tracked the way a browser capture tracks it."""
    typing(entry, 0, delete=len(entry.live.text), insert=BODY)
    artifact = scope["db"].execute("SELECT id FROM artifact_identities WHERE path=?", (str(entry.live.path),)).fetchone()[0]
    start = BODY.index(quote)
    prefix, suffix = anchor_context(BODY, start, start + len(quote), quote)
    row, _ = store.create_with_status(
        artifact, body="on the quote", author="author", author_kind="agent", author_thread_id=scope["owner"],
        anchor_text=quote, from_off=start, to_off=start + len(quote), anchor_prefix=prefix, anchor_suffix=suffix,
        replacement=replacement, db=scope["db"])
    entry.live.track_range(row["id"], row["from_off"], row["to_off"])
    return artifact, row


def _read(scope, entry, artifact):
    threads, checked = comment_anchoring.anchored_threads(artifact, str(entry.live.path), scope["db"])
    assert checked
    return {thread["id"]: thread for thread in threads}


def _span_text(entry, row):
    start, end = entry.live.ranges[row["id"]]
    raw = entry.live.text.encode("utf-16-le")
    return raw[start * 2:end * 2].decode("utf-16-le")


@pytest.mark.asyncio
async def test_an_edit_inside_the_highlight_keeps_the_comment_attached(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is False
        assert thread["anchor_text"] == "First part. very Middle sentence here. Last part."
        assert _span_text(entry, row) == thread["anchor_text"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_deleting_a_sentence_from_the_middle_keeps_the_comment_on_the_rest(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        _edit(entry, entry.live.text.index(" Middle"), len(" Middle sentence here."))
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is False
        assert thread["anchor_text"] == "First part. Last part."
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_typing_right_outside_either_end_is_not_in_the_highlight(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        _edit(entry, entry.live.text.index(QUOTE) + len(QUOTE), 0, " after")
        _edit(entry, entry.live.text.index(QUOTE), 0, "before ")
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is False
        assert thread["anchor_text"] == QUOTE
        assert _span_text(entry, row) == QUOTE
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_deleting_all_of_the_highlight_orphans_the_comment_at_the_collapse_point(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        at = entry.live.text.index(QUOTE)
        _edit(entry, at, len(QUOTE))
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is True
        assert thread["from_off"] == thread["to_off"] == at
        assert thread["anchor_text"] == QUOTE
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_an_open_suggestion_keeps_its_quote_rule(ownership_scope, tmp_path, monkeypatch):
    """A suggestion's quote is the text it replaces, so an edit inside it leaves it stale, not rewritten."""
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is True
        assert thread["anchor_text"] == QUOTE
    finally:
        await manager.close()


@pytest.fixture
def db_scope(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        yield db, seed_document(db, "owner")
    finally:
        db.close()


def test_the_new_quote_is_persisted_and_found_again_on_reload(db_scope):
    db, artifact = db_scope
    row, _ = add_comment(db, artifact)
    store.update_positions(artifact, [{"id": row["id"], "from_off": 2, "to_off": 15,
                                       "anchor_prefix": "A ", "anchor_suffix": " stays."}], db=db)
    edited = "A quoted NEW source stays."
    updates = comment_anchoring.prepare_positions(artifact, edited, db, ranges={row["id"]: (2, 19)})
    assert updates and updates[0]["anchor_text"] == "quoted NEW source"
    store.update_positions(artifact, updates, db=db)
    stored = store.get_comment(row["id"], db=db)
    assert stored["anchor_text"] == "quoted NEW source" and (stored["from_off"], stored["to_off"]) == (2, 19)

    # With no live range (a reload from disk) the new quote and its fresh context find the same place.
    threads, _ = comment_anchoring.anchored_threads(artifact, None, db, text=edited)
    reloaded = next(thread for thread in threads if thread["id"] == row["id"])
    assert reloaded["orphaned"] is False and (reloaded["from_off"], reloaded["to_off"]) == (2, 19)


def test_an_accepted_suggestion_still_behaves_as_before(db_scope):
    db, artifact = db_scope
    row, _ = add_comment(db, artifact, replacement="better wording")
    store.update_positions(artifact, [{"id": row["id"], "from_off": 4, "to_off": 17,
                                       "anchor_prefix": "The ", "anchor_suffix": " stays here."}], db=db)
    with write_transaction(db, scope="test_edit_inside_anchor") as connection:
        comment_actions.record_decision(row["id"], decision="accept", decided_by="Robin", decided_by_thread_id=None, db=connection)
    after = "The better wording stays here."
    threads, _ = comment_anchoring.anchored_threads(artifact, None, db, text=after)
    found = next(thread for thread in threads if thread["id"] == row["id"])
    assert found["orphaned"] is False
    assert (found["from_off"], found["to_off"]) == (4, 4 + len("better wording"))
    assert found["anchor_text"] == "quoted source"
