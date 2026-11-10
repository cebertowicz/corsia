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


def test_incremental_preview_matches_full_projection(isolated_db):
    import random

    from infra.chat_comment_answers import strip_answer_tags, visible_answer_content
    from infra.sidebar_preview import project_previews

    rng = random.Random(1847)
    pieces = ['a', 'é🙂', '\n\n', '\n \n', '[c1.1]', '[c2.2]', '[c1.',
              '[[c1.1]]', '[c1.1][c2.2] ', 'x' * 97, 'x' * 100, 'x' * 101]
    texts = [''.join(rng.choices(pieces, k=rng.randint(1, 18))) for _ in range(500)]
    texts += ['x' * n + '\n\n[c1.1] ' + 'private' * 80 + '\n\n[c2.2] visible'
              for n in (0, 95, 98, 99, 100, 101)]
    db = get_db()
    try:
        _seed(db)
        _answer(db, '[c1.1] indexed')
        db.commit()
        rows = [('thread', 'answer', text) for text in texts] + [('thread', 'unplaced', text) for text in texts]
        actual = project_previews(db, rows)
        expected = [strip_answer_tags(visible_answer_content(text, ['c1.1']) if '[c' in text else text)[:100]
                    for text in texts]
        expected += [strip_answer_tags(text)[:100] for text in texts]
        assert actual == expected
    finally:
        db.close()


def test_placed_answer_crossing_preview_boundary_stays_private(isolated_db):
    from infra.sidebar_preview import project_previews

    db = get_db()
    try:
        _seed(db)
        text = 'Introduction ' * 7 + '\n\n[c1.1] ' + 'SECRET' * 60 + '\n\n[c2.2] visible'
        _answer(db, text)
        db.commit()
        preview = project_previews(db, [('thread', 'answer', text)])[0]
        assert 'SECRET' not in preview
        assert preview == ('Introduction ' * 7 + '\n\nvisible')[:100]
    finally:
        db.close()


def test_batched_preview_projects_answers_with_one_lookup(isolated_db):
    from infra.sidebar_preview import project_previews

    db = get_db()
    try:
        _seed(db)
        _answer(db, "[c1.1] Private answer")
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        previews = project_previews(db, [
            ("thread", "answer", "[c1.1] Private answer"),
            ("thread", "answer", "[c1.1] Private answer"),
            ("thread", "plain", "Plain text"),
        ])
        assert previews == ["", "", "Plain text"]
        assert sum("chat_comment_answers" in sql for sql in statements) == 1
    finally:
        db.close()


def test_last_message_ignores_system_and_tool_rows(isolated_db):
    from core.step_projection import turn_sidebar_projection
    from main import app

    db = get_db()
    try:
        _seed(db)
        _answer(db, "Visible")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at) "
                   "VALUES ('later-system','thread','system','Internal','2026-01-03')")
        db.commit()
    finally:
        db.close()
    response = TestClient(app).get("/api/threads", params={"token": OWNER_TOKEN})
    thread = next(t for t in response.json()["threads"] if t["id"] == "thread")
    assert thread["last_message"] == "Visible"
    assert turn_sidebar_projection("thread")["last_message"] == "Visible"


def test_marked_summary_keeps_full_source_and_batches_lookup(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _answer(db, '```\n' + 'code' * 60 + '\n```\n\nReadable summary\n\n[c1.1] Private')
        record_marked(db, thread_id='thread', message_id='answer', node_id=None, created_at='2026-01-02')
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        assert latest_by_thread(db, ['thread'])['thread']['summary'] == 'Readable summary'
        assert sum('chat_comment_answers' in sql for sql in statements) == 1
    finally:
        db.close()


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
