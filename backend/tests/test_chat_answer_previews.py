"""Placed comment answers must not appear in thread previews."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN
from infra.database import get_db
from infra.thread_updates import latest_by_thread, record_marked
from test_chat_comment_answers import _answer, _seed


def test_placed_answer_is_not_in_thread_or_marked_preview(isolated_db):
    from main import app

    db = get_db()
    try:
        _seed(db)
        _answer(db, "Visible introduction\n\n[c1.1] Private answer")
        record_marked(db, thread_id="thread", message_id="answer", node_id=None, created_at="2026-01-02")
        db.commit()
        summary = latest_by_thread(db, ["thread"])["thread"]["summary"]
    finally:
        db.close()
    response = TestClient(app).get("/api/threads", params={"token": OWNER_TOKEN})
    assert response.status_code == 200
    thread = next(t for t in response.json()["threads"] if t["id"] == "thread")
    assert thread["last_message"] == "Visible introduction"
    assert summary == "Visible introduction"


def test_long_answer_is_projected_before_preview_is_cut(isolated_db):
    from core.step_projection import turn_sidebar_projection
    from main import app

    db = get_db()
    try:
        _seed(db)
        _answer(db, "[c1.1] " + "Private " * 30 + "\n\n[c99.1] " + "Visible " * 20)
        db.commit()
    finally:
        db.close()

    response = TestClient(app).get("/api/threads", params={"token": OWNER_TOKEN})
    assert response.status_code == 200
    thread = next(t for t in response.json()["threads"] if t["id"] == "thread")
    assert thread["last_message"] == ("Visible " * 20)[:100]
    projection = turn_sidebar_projection("thread")
    assert projection is not None
    assert "Private" not in projection["last_message"]
    assert "Visible" in projection["last_message"]


@pytest.fixture
def seeded_answer(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _answer(db, "[c1.1] Private answer")
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_turn_complete_preview_omits_placed_answer(seeded_answer, monkeypatch):
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
    update = next(frame for frame in emitted if frame["type"] == "session_updated")
    assert update["last_message"] == ""
