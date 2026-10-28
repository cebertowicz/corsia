"""Chat answers are indexed at write time and read per person."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.chat_comment_answers import UNREAD_SQL, parse_answers, unread_answers, visible_answer_content
from infra.database import get_db, initialize_schema
from infra.database_migrations import migrate_to_current
from infra.queries import insert_message
from infra.reply_context import RejectedComments, require_every_comment
from infra.schema import SCHEMA_VERSION


def _seed(db):
    seed_thread(db, "thread")
    db.execute("UPDATE threads SET user_id=?, next_message_seq=2 WHERE id='thread'", (OWNER_USER,))
    metadata = json.dumps({"reply_context": {"comments": [{"cid": "c1.1"}, {"cid": "c1.2"}]}})
    db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq,input_order) "
               "VALUES ('question','thread','user','text',?,'2026-01-01',1,1)", (metadata,))
    db.commit()


def _answer(db, text="[c1.1][c1.2] shared\n\nmore detail\n\n[c1.9] unknown"):
    return insert_message("thread", "assistant", text, {"turn_unbound_reason": "legacy_import"},
                          msg_id="answer", db=db, created_at="2026-01-02")


def test_parser_places_multitag_sections_and_leaves_uncited_text():
    parsed = parse_answers("intro\n\n[c1.1][c1.2] shared\n\nmore\n\n[c1.3] final", "answer")
    assert parsed["c1.1"] == parsed["c1.2"] == [{"text": "shared\n\nmore", "assistant_message_id": "answer"}]
    assert parsed["c1.3"][0]["text"] == "final"
    assert "intro" not in str(parsed)
    trailing = parse_answers("leading post\n\n[c1.1] answer\n\ntrailing paragraph", "answer")
    assert trailing["c1.1"][0]["text"] == "answer\n\ntrailing paragraph"
    assert "leading post" not in str(trailing)


@pytest.mark.parametrize(('text', 'cids', 'expected'), [
    ('Introduction\n\n[c1.1][c1.2] shared answer', ['c1.1', 'c1.2'], 'Introduction'),
    ('[c1.9] unknown\n\n[c1.1] placed', ['c1.1'], '[c1.9] unknown'),
    ('Leading post\n\n[c1.1] answer\n\nTrailing paragraph', ['c1.1'], 'Leading post'),
    ('[c1.1] answer', ['c1.1'], ''),
])
def test_visible_content_keeps_only_unplaced_sections(text, cids, expected):
    assert visible_answer_content(text, cids) == expected


def test_malformed_cid_is_rejected():
    with pytest.raises(RejectedComments) as exc:
        require_every_comment({"comments": [{}]}, {"comments": [{"cid": "c1.invalid"}]})
    assert exc.value.positions == [1]


def test_fresh_and_upgraded_schema():
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.execute("DROP TABLE chat_comment_answers")
    db.execute("DROP TABLE chat_comment_views")
    db.execute("PRAGMA user_version=148")
    db.commit()
    migrate_to_current(db, SCHEMA_VERSION)
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_comment_%'")} == {
        "chat_comment_answers", "chat_comment_views", "chat_comments"
    }
    db.close()


def test_write_places_known_answers_and_unread_skips_deleted(isolated_db):
    db = get_db()
    try:
        _seed(db)
        _answer(db)
        assert [r["cid"] for r in unread_answers(db, OWNER_USER, "thread", 10)] == ["c1.1", "c1.2"]
        db.execute("INSERT INTO chat_comment_views VALUES (?, 'question', 'c1.1', '2026-01-01T12')", (OWNER_USER,))
        assert [r["cid"] for r in unread_answers(db, OWNER_USER, "thread", 10)] == ["c1.1", "c1.2"]
        db.execute("UPDATE chat_comment_views SET seen_at='2026-01-03' WHERE cid='c1.1'")
        assert [r["cid"] for r in unread_answers(db, OWNER_USER, "thread", 1)] == ["c1.2"]
        assert unread_answers(db, "another-user", "thread", 10) == []
        db.execute("SAVEPOINT deletion")
        db.execute("DELETE FROM messages WHERE id='answer'")
        assert unread_answers(db, OWNER_USER, "thread", 10) == []
        db.execute("ROLLBACK TO deletion")
        db.execute("DELETE FROM messages WHERE id='question'")
        assert unread_answers(db, OWNER_USER, "thread", 10) == []
        db.rollback()
    finally:
        db.close()


def test_route_stamps_the_newest_seen_answer_and_checks_ownership(isolated_db):
    from main import app
    db = get_db()
    try:
        _seed(db)
    finally:
        db.close()
    client = TestClient(app)
    url = "/api/threads/thread/messages/question/comments/c1.1/viewed"
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}

    def unread_and_stamps():
        db = get_db()
        try:
            unread = [r["cid"] for r in unread_answers(db, OWNER_USER, "thread", 10)]
            return unread, [r[0] for r in db.execute("SELECT seen_at FROM chat_comment_views")]
        finally:
            db.close()

    assert client.post(url, headers={"Authorization": f"Bearer {OTHER_TOKEN}"}, json={"assistant_message_id": "answer"}).status_code == 404
    # An answer not yet committed cannot move the stamp.
    assert client.post(url, headers=headers, json={"assistant_message_id": "answer"}).status_code == 200
    db = get_db()
    try:
        _answer(db)
        db.commit()
    finally:
        db.close()
    assert unread_and_stamps() == (["c1.1", "c1.2"], [])
    assert client.post(url, headers=headers, json={"assistant_message_id": "answer"}).status_code == 200
    assert unread_and_stamps() == (["c1.2"], ["2026-01-02"])
    db = get_db()
    try:
        insert_message("thread", "assistant", "[c1.1] again", {"turn_unbound_reason": "legacy_import"},
                       msg_id="answer-2", db=db, created_at="2026-01-04")
        db.commit()
    finally:
        db.close()
    assert unread_and_stamps() == (["c1.2", "c1.1"], ["2026-01-02"])
    # A newer answer arriving during the first reply's dwell stays unread.
    assert client.post(url, headers=headers, json={"assistant_message_id": "answer"}).status_code == 200
    assert unread_and_stamps() == (["c1.2", "c1.1"], ["2026-01-02"])
    assert client.post(url, headers=headers, json={"assistant_message_id": "answer-2"}).status_code == 200
    assert unread_and_stamps() == (["c1.2"], ["2026-01-04"])
    assert client.post(url, headers=headers, json={"assistant_message_id": "answer"}).status_code == 200
    assert unread_and_stamps() == (["c1.2"], ["2026-01-04"])


@pytest.mark.parametrize("operation", ["purge", "delete"])
def test_thread_teardown_removes_only_its_chat_comment_rows(isolated_db, operation):
    from datetime import datetime, timezone

    from core.thread_family import delete_thread_family
    from core.thread_lifecycle import _apply_purge

    db = get_db()
    try:
        _seed(db)
        _answer(db)
        seed_thread(db, "other")
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) "
                   "VALUES ('other-question','other','user','x','t',1)")
        db.execute("INSERT INTO chat_comment_answers VALUES ('other','other-question','c1.1','other-answer','t')")
        db.executemany("INSERT INTO chat_comments (thread_id,target_message_id,user_message_id,cid,created_at) VALUES (?,?,?,?,?)", [
            ('thread', 'answer', 'question', 'c1.1', 't'),
            ('other', 'other-answer', 'other-question', 'c1.1', 't'),
        ])
        db.executemany("INSERT INTO chat_comment_views VALUES (?,?,?,?)", [
            (OWNER_USER, "question", "c1.1", "t"),
            (OWNER_USER, "other-question", "c1.1", "t"),
        ])
        if operation == "purge":
            _apply_purge(db, "thread", datetime.now(timezone.utc))
        else:
            delete_thread_family(db, ["thread"])
        assert [row[0] for row in db.execute("SELECT thread_id FROM chat_comment_answers")] == ["other"]
        assert [row[0] for row in db.execute("SELECT thread_id FROM chat_comments")] == ["other"]
        assert [row[0] for row in db.execute("SELECT user_message_id FROM chat_comment_views")] == ["other-question"]
    finally:
        db.rollback()
        db.close()


def test_unread_query_uses_indexes_on_large_thread(isolated_db):
    db = get_db()
    try:
        _seed(db)
        db.executemany("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES (?, 'thread', 'tool', '', 't', ?)",
                       [(f"tool-{n}", n + 2) for n in range(2000)])
        db.execute("UPDATE threads SET next_message_seq=2002 WHERE id='thread'")
        _answer(db)
        plan = " ".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + UNREAD_SQL, (OWNER_USER, OWNER_USER, "thread", 10)))
        assert "idx_chat_comment_answers_thread_time" in plan
        assert "SCAN u" not in plan and "SCAN m" not in plan
        assert len(unread_answers(db, OWNER_USER, "thread", 10)) == 2
    finally:
        db.rollback()
        db.close()
