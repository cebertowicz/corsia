"""A comment keeps its place when the text inside its highlight changes; only a collapsed range orphans."""

from __future__ import annotations

import asyncio

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, comment_anchoring, comments as store, suggestion_overlap
from doceditor.live_comments import positions
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.quote_anchor import anchor_context
from infra.database import get_db, write_transaction
from tests.test_document_live_comments import _edit
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import legacy_writer, typing, user_id
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
async def test_an_open_suggestion_edited_inside_keeps_its_place_and_its_quote(ownership_scope, tmp_path, monkeypatch):
    """A suggestion's quote is the text it replaces: an edit inside leaves it at its line, flagged, never rewritten."""
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is False and thread["text_changed"] is True
        assert thread["anchor_text"] == QUOTE
        assert _span_text(entry, row) == "First part. very Middle sentence here. Last part."
        start, end = entry.live.ranges[row["id"]]
        assert (thread["from_off"], thread["to_off"]) == (start, end)
        # Accept can't apply to the edited text: the file path refuses it as stale (the live path checks the same quote).
        with pytest.raises(comment_actions.CommentActionError) as refused:
            comment_actions.suggestion_span(thread, entry.live.text)
        assert refused.value.kind == "stale"
    finally:
        await manager.close()


@pytest.mark.parametrize(("text", "end", "decision", "orphaned", "changed"), [
    ("The quick fox", 9, None, False, False),
    ("The quXick fox", 10, None, False, True),
    ("The  fox", 4, None, True, False),
    ("The  fox", 4, "rejected", False, False),
])
def test_server_status_cases_match_the_local_projection_cases(text, end, decision, orphaned, changed):
    row = {"id": "case", "parent_id": None, "review": False, "anchor_text": "quick", "from_off": 4,
           "to_off": 9, "anchor_prefix": "The ", "anchor_suffix": " fox", "replacement": "slow",
           "decision": decision, "resolved": False, "orphaned": False}
    comment_anchoring.resolve_threads([row], text, ranges={"case": (4, end)})
    assert bool(row.get("orphaned")) is orphaned
    assert bool(row.get("text_changed")) is changed


@pytest.mark.asyncio
async def test_live_positions_reports_the_server_flags_for_an_edited_suggestion(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        snapshot = await positions(entry, artifact)
        states = {item["id"]: item for item in snapshot["states"]}
        assert states[row["id"]] == {"id": row["id"], "orphaned": False, "text_changed": True}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_decided_open_suggestion_with_deleted_text_has_same_flag_in_read_and_snapshot(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        store.create(artifact, body="Reply", parent_id=row["id"], announce=False, db=scope["db"])
        with write_transaction(scope["db"], scope="test_decided_open_lost") as connection:
            comment_actions.record_decision(row["id"], decision="reject", decided_by="Robin", decided_by_thread_id=None, db=connection)
        assert not store.get_comment(row["id"], db=scope["db"])["resolved"]
        _edit(entry, entry.live.text.index(QUOTE), len(QUOTE))
        orphaned = _read(scope, entry, artifact)[row["id"]]["orphaned"]
        assert orphaned is False
        states = {item["id"]: item for item in (await positions(entry, artifact))["states"]}
        assert states[row["id"]]["orphaned"] is orphaned
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_an_open_suggestion_with_all_its_text_deleted_still_orphans(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        _edit(entry, entry.live.text.index(QUOTE), len(QUOTE))
        thread = _read(scope, entry, artifact)[row["id"]]
        assert thread["orphaned"] is True and not thread.get("text_changed")
    finally:
        await manager.close()


async def _publish(scope, entry):
    """Save the live text and let the publisher persist the comment places that go with it."""
    flushed = await entry.lane.submit(Operation("flush", actor=WriteActor(scope["owner"], "project-alpha")))
    assert flushed.server_saved

    async def published():
        while await asyncio.to_thread(entry.live.store.next_publication):
            await asyncio.sleep(0.01)
    await asyncio.wait_for(published(), 3)


@pytest.mark.asyncio
async def test_edited_suggestion_does_not_block_overlap_and_is_marked_stranded(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry, replacement="Replaced.")
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        assert _read(scope, entry, artifact)[row["id"]]["text_changed"] is True
        start, end = entry.live.ranges[row["id"]]
        suggestion_overlap.check_agent_suggestion_fits(scope["db"], artifact, str(entry.live.path), start, end)
        await _publish(scope, entry)
        assert store.get_comment(row["id"], db=scope["db"])["stranded_notified_at"] is not None
    finally:
        await manager.close()


def _reloaded(scope, entry, artifact):
    """What a reader gets after the live document is gone: the file's text and the stored rows only."""
    threads, checked = comment_anchoring.anchored_threads(artifact, None, scope["db"], text=entry.live.path.read_text())
    assert checked
    return {thread["id"]: thread for thread in threads}


def _text_at(text, thread):
    raw = text.encode("utf-16-le")
    return raw[thread["from_off"] * 2:thread["to_off"] * 2].decode("utf-16-le")


async def _live_then_reloaded(scope, entry, artifact, row):
    """The thread as the live document reports it, then as a reload from disk reports it."""
    live = _read(scope, entry, artifact)[row["id"]]
    await _publish(scope, entry)
    return live, _reloaded(scope, entry, artifact)[row["id"]]


@pytest.mark.asyncio
async def test_an_edit_inside_the_highlight_stays_attached_after_a_reload(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        _edit(entry, entry.live.text.index("Middle"), 0, "very ")
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is False
            assert _text_at(entry.live.text, thread) == "First part. very Middle sentence here. Last part."
        assert reloaded["anchor_text"] == "First part. very Middle sentence here. Last part."
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["start", "end"])
async def test_typing_at_an_edge_stays_outside_the_highlight_after_a_reload(ownership_scope, tmp_path, monkeypatch, edge):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        begin = entry.live.text.index(QUOTE)
        _edit(entry, begin if edge == "start" else begin + len(QUOTE), 0, "before " if edge == "start" else " after")
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is False
            assert _text_at(entry.live.text, thread) == QUOTE
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["start", "end"])
async def test_deleting_text_at_an_edge_shrinks_the_highlight_and_keeps_it_after_a_reload(ownership_scope, tmp_path, monkeypatch, edge):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        begin = entry.live.text.index(QUOTE)
        cut = "First part. " if edge == "start" else " Last part."
        _edit(entry, begin if edge == "start" else begin + len(QUOTE) - len(cut), len(cut))
        kept = QUOTE.replace(cut, "")
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is False
            assert _text_at(entry.live.text, thread) == kept
        assert reloaded["anchor_text"] == kept
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_fully_deleted_highlight_keeps_its_thread_at_the_nearest_line_after_a_reload(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        at = entry.live.text.index(QUOTE)
        _edit(entry, at, len(QUOTE))
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is True
            assert thread["from_off"] == thread["to_off"] == at
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_deleted_highlights_thread_follows_later_edits_above_it_after_a_reload(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = _comment_on(scope, entry)
        at = entry.live.text.index(QUOTE)
        _edit(entry, at, len(QUOTE))
        await _publish(scope, entry)
        _edit(entry, 0, 0, "Added line above.\n\n")
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        shifted = at + len("Added line above.\n\n")
        for thread in (live, reloaded):
            assert thread["orphaned"] is True
            assert thread["from_off"] == thread["to_off"] == shifted
    finally:
        await manager.close()


async def _accepted(scope, entry, replacement="Better words."):
    """A suggestion on the middle sentence, accepted through the live lane."""
    artifact, row = _comment_on(scope, entry, "Middle sentence here.", replacement=replacement)
    await comment_actions.decide_suggestion(row["id"], "accept", user_id=user_id(scope), decided_by="user",
                                            actor_thread_id=None, write=legacy_writer)
    return artifact, row


@pytest.mark.asyncio
async def test_a_decided_suggestion_follows_later_edits_above_it_after_a_reload(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = await _accepted(scope, entry)
        assert "Better words." in entry.live.text
        _edit(entry, 0, 0, "Added line above.\n\n")
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is False
            assert _text_at(entry.live.text, thread) == "Better words."
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_decided_suggestion_edited_inside_is_tracked_at_its_live_place(ownership_scope, tmp_path, monkeypatch):
    """The decided text was edited, so its stored quote no longer matches; its live range, not its stale offsets, says where it is."""
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    try:
        artifact, row = await _accepted(scope, entry)
        _edit(entry, entry.live.text.index("words"), 0, "very ")
        _edit(entry, 0, 0, "Added line above.\n\n")
        placed = await positions(entry, artifact)
        by_id = {item["id"]: item for item in placed["ranges"]}
        assert _text_at(entry.live.text, by_id[row["id"]]) == "Better very words."
        live, reloaded = await _live_then_reloaded(scope, entry, artifact, row)
        for thread in (live, reloaded):
            assert thread["orphaned"] is False
            assert _text_at(entry.live.text, thread) == "Better very words."
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


def test_an_edited_suggestion_keeps_its_place_on_reload_and_loses_it_when_its_surroundings_change(db_scope):
    db, artifact = db_scope
    row, _ = add_comment(db, artifact, replacement="better wording")
    store.update_positions(artifact, [{"id": row["id"], "from_off": 4, "to_off": 17,
                                       "anchor_prefix": "The ", "anchor_suffix": " stays here."}], db=db)
    edited = "The quoted NEW source stays here."
    updates = comment_anchoring.prepare_positions(artifact, edited, db, ranges={row["id"]: (4, 21)})
    assert updates == [{"id": row["id"], "from_off": 4, "to_off": 21, "anchor_prefix": "The ", "anchor_suffix": " stays here."}]
    store.update_positions(artifact, updates, db=db)

    # No live range (a reload from disk): the stored place still has its stored surroundings, so the suggestion stays there.
    threads, _ = comment_anchoring.anchored_threads(artifact, None, db, text=edited)
    reloaded = next(thread for thread in threads if thread["id"] == row["id"])
    assert reloaded["orphaned"] is False and reloaded["text_changed"] is True
    assert (reloaded["from_off"], reloaded["to_off"]) == (4, 21) and reloaded["anchor_text"] == "quoted source"

    # The text around it changed too: nothing says the stored place is still its place.
    threads, _ = comment_anchoring.anchored_threads(artifact, None, db, text="Other words. The quoted NEW source stays here.")
    lost = next(thread for thread in threads if thread["id"] == row["id"])
    assert lost["orphaned"] is True and not lost.get("text_changed")
