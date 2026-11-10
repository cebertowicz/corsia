"""Placed comment answers must not appear in thread previews."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN
from infra.database import get_db
from infra.sidebar_preview import LAST_MESSAGE_PREVIEW
from infra.thread_updates import latest_by_thread, record_marked
from test_chat_comment_answers import _answer, _seed


def _listed_preview():
    from main import app

    response = TestClient(app).get("/api/threads", params={"token": OWNER_TOKEN})
    assert response.status_code == 200
    return next(t for t in response.json()["threads"] if t["id"] == "thread")["last_message"]


async def _turn_complete_preview(monkeypatch):
    from core import step_handler
    from core.turn_core import TurnIdentity

    emitted = []

    async def broadcast(frame):
        emitted.append(frame)

    handler = step_handler.StepHandler("thread", {}, broadcast, "turn")
    handler.bind_turn_identity(TurnIdentity("turn", 1))
    monkeypatch.setattr(handler, "_persist_terminal_context", AsyncMock(return_value={}))
    monkeypatch.setattr(step_handler, "record_turn_end_logged", AsyncMock())
    await handler._handle_turn_complete({})
    return next(frame for frame in emitted if frame["type"] == "session_updated")["last_message"]


def test_placed_answer_is_not_in_thread_or_marked_preview(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _answer(db, "Visible introduction\n\n[c1.1] Private answer")
        record_marked(db, thread_id="thread", message_id="answer", node_id=None, created_at="2026-01-02")
        db.commit()
        summary = latest_by_thread(db, ["thread"])["thread"]["summary"]
    finally:
        db.close()
    assert _listed_preview() == "Visible introduction"
    assert summary == "Visible introduction"


@pytest.mark.asyncio
async def test_list_and_turn_complete_previews_are_the_saved_text_cut_at_100(isolated_db, monkeypatch):
    db = get_db()
    try:
        _seed(db)
        _answer(db, "[c1.1] " + "Private " * 30 + "\n\n[c99.1] " + "Visible " * 20)
        db.commit()
    finally:
        db.close()
    # The unplaced section keeps its words; only its tag is dropped from the preview.
    expected = ("[c99.1] " + "Visible " * 20)[:100].replace("[c99.1] ", "")
    assert _listed_preview() == expected
    assert await _turn_complete_preview(monkeypatch) == expected


@pytest.mark.asyncio
async def test_last_message_ignores_system_and_tool_rows(isolated_db, monkeypatch):
    db = get_db()
    try:
        _seed(db)
        _answer(db, "Visible")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at) "
                   "VALUES ('later-system','thread','system','Internal','2026-01-03')")
        db.commit()
    finally:
        db.close()
    assert _listed_preview() == "Visible"
    assert await _turn_complete_preview(monkeypatch) == "Visible"


@pytest.mark.asyncio
async def test_turn_complete_preview_omits_placed_answer(isolated_db, monkeypatch):
    db = get_db()
    try:
        _seed(db)
        _answer(db, "[c1.1] Private answer")
        db.commit()
    finally:
        db.close()
    assert await _turn_complete_preview(monkeypatch) == ""


def test_marked_summary_reads_the_saved_text_whole(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _answer(db, '```\n' + 'code' * 60 + '\n```\n\nReadable summary\n\n[c1.1] Private')
        record_marked(db, thread_id='thread', message_id='answer', node_id=None, created_at='2026-01-02')
        db.commit()
        assert latest_by_thread(db, ['thread'])['thread']['summary'] == 'Readable summary'
    finally:
        db.close()


def test_preview_reads_saved_text_by_primary_key(isolated_db):
    db = get_db()
    try:
        plan = " | ".join(row[-1] for row in db.execute(
            f"EXPLAIN QUERY PLAN SELECT {LAST_MESSAGE_PREVIEW} FROM threads s WHERE s.id=?", ("thread",),
        ))
    finally:
        db.close()
    assert "SEARCH cv USING INDEX sqlite_autoindex_chat_visible_content_1 (message_id=?)" in plan, plan
