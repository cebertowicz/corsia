"""``comment_turn`` frames: a page's hold on a comment thread lapses, leaves with its socket, and never blocks Stop."""

from __future__ import annotations

import asyncio
import json

import pytest

from delegation_docs_fixtures import _OTHER, _OWNER, _comment_by_thread_a, _mk_thread, _post, _registrable_roots, doc
from test_comment_writer_turn import _fresh_turns, _frame, _line, _rows, driven
from test_doc_suggestions import _own, _suggest

pytestmark = pytest.mark.integration

__all__ = ["_fresh_turns", "_registrable_roots", "doc", "driven"]

SECOND_QUOTE = "# Design"


def _execute(sql: str, *args) -> None:
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="test_comment_turn_frames") as connection:
            connection.execute(sql, args)
    finally:
        db.close()


async def _settled() -> None:
    from ws import comment_turn

    await asyncio.gather(*comment_turn._tasks)


async def _held_with_b_in_line(client, doc):
    from doceditor import writer_turns

    _a, root = _comment_by_thread_a(client, doc)
    b = _mk_thread(client, _OWNER, title="Agent B")
    page = object()
    await _frame(page, root, True)
    _post(client, _OWNER, parent_id=root, body="B saved", as_session_id=b)
    assert writer_turns.person_holding(root) is not None
    assert [r["state"] for r in _line(root)] == ["waiting"]
    return root, b, page


@pytest.mark.asyncio
async def test_a_page_that_stops_renewing_loses_the_thread_after_the_lease_and_the_line_is_served(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import writer_turns
    from ws import comment_turn

    root, b, page = await _held_with_b_in_line(integration_client, doc)
    handle = comment_turn._leases[page]
    assert handle.when() - asyncio.get_running_loop().time() == pytest.approx(writer_turns.LEASE_SECONDS, abs=5)

    handle._run()  # the lease timer fires; no sleep
    await _settled()

    assert writer_turns.is_free(root)
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken")]
    assert b in driven


@pytest.mark.asyncio
async def test_a_renewal_moves_the_lease_and_a_closed_socket_frees_the_thread_at_once(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import writer_turns
    from ws import comment_turn

    root, b, page = await _held_with_b_in_line(integration_client, doc)
    first = comment_turn._leases[page]
    await _frame(page, root, True)
    assert first.cancelled() and comment_turn._leases[page] is not first

    comment_turn.disconnect(page)
    await _settled()

    assert page not in comment_turn._leases and page not in comment_turn._order
    assert writer_turns.is_free(root)
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken")]


@pytest.mark.asyncio
async def test_a_frame_for_a_comment_the_user_cannot_reach_takes_no_hold(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import writer_turns
    from infra.auth import authenticate
    from ws import comment_turn

    _a, root = _comment_by_thread_a(integration_client, doc)
    page = object()
    await comment_turn.handle_frame(page, authenticate(_OTHER), {"type": "comment_turn", "comment_id": root, "on": True})
    await comment_turn.handle_frame(page, authenticate(_OWNER), {"type": "comment_turn", "comment_id": "missing", "on": True})

    assert writer_turns.is_free(root)
    assert page not in comment_turn._leases


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "comment_turn"},
    {"type": "comment_turn", "on": "yes"},
    {"type": "comment_turn", "on": True},
    {"type": "comment_turn", "on": True, "comment_id": 7},
])
async def test_a_malformed_frame_is_ignored_without_raising(frame):
    from doceditor import writer_turns
    from ws import comment_turn

    page = object()
    await comment_turn.handle_frame(page, {"id": "user-1", "name": "Someone"}, frame)

    assert page not in comment_turn._leases and page not in comment_turn._order
    assert writer_turns.release(page) == []


@pytest.mark.asyncio
async def test_decide_all_decides_free_threads_and_saves_held_ones_in_line(
    integration_client, doc, _registrable_roots, driven,
):
    from doceditor import comments as store
    from tools.documents import DocCommentUpdateTool

    owner, _artifact_id = await _own(integration_client, doc)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    free = await _suggest(reviewer, doc)
    held = await _suggest(reviewer, doc, replacement="A new title", quote=SECOND_QUOTE)
    await _frame(object(), held["id"], True)

    result = json.loads(await DocCommentUpdateTool().execute(
        comment_id=free["id"], decision="reject", all_from_author=True, _thread_id=owner))

    assert store.get_comment(free["id"])["decision"] == "rejected"
    assert store.get_comment(held["id"])["decision"] is None
    assert [item["comment_id"] for item in result["queued"]] == [held["id"]]
    assert [(r["agent_thread_id"], r["state"]) for r in _line(held["id"])] == [(owner, "waiting")]
    assert _line(free["id"]) == []


@pytest.mark.asyncio
async def test_a_deleted_agent_leaves_the_line_and_the_next_is_woken(
    integration_client, doc, _registrable_roots, driven,
):
    from datetime import datetime, timezone

    from automation.continuation.sources.feedback_batches import FeedbackBatchSource

    root, b, page = await _held_with_b_in_line(integration_client, doc)
    c = _mk_thread(integration_client, _OWNER, title="Agent C")
    _post(integration_client, _OWNER, parent_id=root, body="C saved", as_session_id=c)
    await _frame(page, root, False)
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(b, "woken"), (c, "waiting")]

    _execute("DELETE FROM threads WHERE id=?", b)  # its saved row goes with it
    woken = FeedbackBatchSource().prepare(datetime.now(timezone.utc).isoformat(), db=None)

    assert c in woken
    assert [(r["agent_thread_id"], r["state"]) for r in _line(root)] == [(c, "woken")]


@pytest.mark.asyncio
async def test_a_deleted_comment_thread_drops_everyone_in_its_line(
    integration_client, doc, _registrable_roots, driven,
):
    from ws import comment_turn

    root, b, page = await _held_with_b_in_line(integration_client, doc)
    _execute("DELETE FROM doc_comments WHERE parent_id=? OR id=?", root, root)
    comment_turn.disconnect(page)
    await _settled()

    assert _line(root) == []
    assert b not in driven
    notes = _rows("SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", b)
    assert any("the thread was deleted" in note["summary"] and "B saved" in note["summary"] for note in notes)


@pytest.mark.asyncio
async def test_stop_is_handled_while_a_comment_turn_frame_is_still_being_served(monkeypatch):
    import ws
    from ws import comment_turn, document_control
    from test_document_live_transport import Socket

    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(_socket, _user, _data):
        entered.set()
        await release.wait()

    monkeypatch.setattr(comment_turn, "handle_frame", slow)
    monkeypatch.setattr(ws, "_handle_stop", lambda *args, **kwargs: stopped.set())
    socket = Socket()
    receiver = asyncio.create_task(ws._receive_frames(socket, {"id": "user-1"}, asyncio.Queue()))
    try:
        await socket.inbound.put({"type": "comment_turn", "comment_id": "c-1", "on": True})
        await asyncio.wait_for(entered.wait(), 2)
        await socket.inbound.put({"type": "stop", "thread_id": "thread-1"})
        await asyncio.wait_for(stopped.wait(), 2)
        assert not release.is_set()
    finally:
        release.set()
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await document_control.disconnect(socket)
