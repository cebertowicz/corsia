"""Comment-only turns do not contribute a Focus work receipt."""

from api.thread_focus_page import _walk, page_run_range
from api.thread_focus_projection import page_run_projection
from test_thread_focus_page import _SID, _Thread, _db, _live
from infra.chat_comment_receipts import annotate_comment_only_receipts
from infra.chat_comment_schema import CHAT_COMMENT_SCHEMA_SQL


def _comment_turn(db, thread, turn, *, visible_text="", status="completed"):
    _live(db, turn, status)
    input_id = thread.add("u", content="", meta={"reply_context": {"comments": [{"cid": "c1.1"}]}})
    db.execute("UPDATE messages SET input_order=?, active_turn_id=? WHERE id=?", (thread.n, turn, input_id))
    step = thread.add("w", turn)
    answer = thread.add("s", turn, content="[c1.1] Answer")
    db.execute("INSERT INTO chat_visible_content VALUES (?,?,?)", (answer, _SID, visible_text))
    db.execute("INSERT INTO chat_comments VALUES (?,?,?,?,?,?,?)", (_SID, step, input_id, "c1.1", "t", None, None))
    db.execute("INSERT INTO chat_comment_answers VALUES (?,?,?,?,?)", (_SID, input_id, "c1.1", answer, "t"))
    return step


def test_plain_page_carries_derived_receipt_fact_for_focus_rendering():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    hidden = _comment_turn(db, thread, "comment")
    ordinary = thread.add("w", "ordinary")
    rows = [dict(row) for row in db.execute("SELECT * FROM messages WHERE id IN (?, ?)", (hidden, ordinary))]
    annotate_comment_only_receipts(db, _SID, rows)
    by_id = {row["id"]: row for row in rows}
    assert by_id[hidden]["commentOnlyReceipt"] is True
    assert "commentOnlyReceipt" not in by_id[ordinary]


def test_reproject_live_stub_keeps_unattributed_work():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment", status="running")
    context = thread.add("c")
    thread.add("w", "comment")
    stub = next(run for run in _walk(db, _SID, None, 20, 100).runs if run["first_id"] == context)
    db.execute("UPDATE active_turns SET status='completed' WHERE id='comment'")
    runs = page_run_projection(db, _SID, stub["first_id"], stub["last_id"])
    assert len(runs) == 1
    assert runs[0]["first_id"] == runs[0]["last_id"] == context
    assert runs[0]["turn_ids"] == []
    assert runs[0]["count"] == 1


def test_focus_omits_a_finished_comment_only_turn():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    assert _walk(db, _SID, None, 20, 100).runs == []


def test_focus_mixed_turns_keep_only_ordinary_work_counts():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    normal = thread.add("w", "ordinary")
    runs = _walk(db, _SID, None, 20, 100).runs
    assert len(runs) == 1
    assert runs[0]["turn_ids"] == ["ordinary"]
    assert runs[0]["count"] == 1
    assert runs[0]["weight"] == 1
    assert runs[0]["first_id"] == normal


def test_expanded_mixed_range_omits_comment_turn_work():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    first = thread.add("w", "ordinary")
    hidden = _comment_turn(db, thread, "comment")
    last = thread.add("w", "ordinary")
    page = page_run_range(db, _SID, 20, first, last, None)
    assert hidden not in [row["id"] for row in page["messages"]]
    assert first in [row["id"] for row in page["messages"]]
    assert last in [row["id"] for row in page["messages"]]


def test_mixed_run_continues_across_suppressed_scan_boundary():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    thread.add("w", "older")
    _comment_turn(db, thread, "comment")
    thread.add("w", "newer")
    walk = _walk(db, _SID, None, 20, 2)
    assert walk.has_more
    assert walk.runs[0]["turn_ids"] == ["newer"]
    assert walk.runs[0]["continues_older"] is True


def test_expanded_range_advances_through_an_entirely_suppressed_page():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    first = thread.add("w", "ordinary")
    _comment_turn(db, thread, "comment")
    last = thread.add("w", "ordinary")
    page = page_run_range(db, _SID, 1, first, last, None)
    seen = []
    while True:
        seen.extend(row["id"] for row in page["messages"])
        if not page["has_more"]:
            break
        previous = page["oldest_id"]
        page = page_run_range(db, _SID, 1, first, last, previous)
        assert page["oldest_id"] != previous
    assert first in seen and last in seen
    assert len(seen) < thread.n


def test_answer_to_an_older_input_does_not_hide_current_turn():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    db.execute("UPDATE messages SET active_turn_id='older' WHERE role='user'")
    current = thread.add("u", content="", meta={"reply_context": {"comments": [{"cid": "c2.1"}]}})
    db.execute("UPDATE messages SET input_order=?, active_turn_id='comment' WHERE id=?", (thread.n, current))
    assert any("comment" in run["turn_ids"] for run in _walk(db, _SID, None, 20, 100).runs)


def test_comment_turn_without_a_placed_answer_keeps_its_work():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    db.execute("DELETE FROM chat_visible_content")
    assert any("comment" in run["turn_ids"] for run in _walk(db, _SID, None, 20, 100).runs)


def test_comment_turn_with_an_ordinary_input_keeps_its_work():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    ordinary = thread.add("u", content="A separate instruction")
    db.execute("UPDATE messages SET input_order=?, active_turn_id=? WHERE id=?", (thread.n, "comment", ordinary))
    assert any("comment" in run["turn_ids"] for run in _walk(db, _SID, None, 20, 100).runs)


def test_focus_keeps_comment_turn_with_visible_chat_and_live_turn():
    for text, status in [("Chat text", "completed"), ("", "running")]:
        db = _db()
        db.executescript(CHAT_COMMENT_SCHEMA_SQL)
        thread = _Thread(db)
        _comment_turn(db, thread, "comment", visible_text=text, status=status)
        assert any("comment" in run["turn_ids"] for run in _walk(db, _SID, None, 20, 100).runs)


def test_comment_only_work_rule_matches_its_sql_twin():
    from infra.focus_rows import COMMENT_ONLY_WORK_SQL, is_comment_only_work

    db = _db()
    thread = _Thread(db)
    for turn in ("comment", "ordinary"):
        thread.add("u", turn)
        thread.add("w", turn)
        thread.add("s", turn)
        thread.add("c", turn)
        thread.add("c", turn, meta={"type": "error"})
    thread.add("w")
    thread.add("w", content="x", meta={"turn_id": 7})
    db.execute("UPDATE messages SET metadata='not json' WHERE id=?", (thread.add("w", "comment"),))
    rows = db.execute(
        f"SELECT *, {COMMENT_ONLY_WORK_SQL} AS work FROM messages", {"suppressed": '["comment"]'},
    ).fetchall()
    assert sum(row["work"] for row in rows) == 3
    for row in rows:
        assert bool(row["work"]) is is_comment_only_work(dict(row), {"comment"}), row["id"]
