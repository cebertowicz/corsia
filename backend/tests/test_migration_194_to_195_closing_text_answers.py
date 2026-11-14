"""Schema 195: a closing-text answer that repeats a sent answer in the same Turn is dropped; a different one stays."""
import json
import sqlite3
from contextlib import closing

from loguru import logger

from infra.database import initialize_schema
from infra.migration_194_to_195_closing_text_answers import migrate_194_to_195


def _message(db, message_id, turn, text="Moved it.", cid="c1.1", for_user=False):
    meta = {"turn_id": turn, **({"for_user": True} if for_user else {})}
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,'thread','assistant',?,?,'t')",
        (message_id, f"[{cid}] {text}", json.dumps(meta)),
    )


def _answer(db, message_id, cid="c1.1", user_message="question"):
    db.execute("INSERT INTO chat_comment_answers VALUES ('thread',?,?,?,'t')", (user_message, cid, message_id))


def _left(db):
    return sorted(tuple(row) for row in db.execute("SELECT assistant_message_id,cid FROM chat_comment_answers"))


def _run(db):
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        migrate_194_to_195(db)
    finally:
        logger.remove(sink)
    return [line.strip() for line in lines if "closing-text answers" in line]


def test_an_exact_repeat_is_dropped_and_a_different_closing_answer_stays():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _message(db, "sent", "t1", for_user=True)
        # Same words, different spacing and line breaks: an exact repeat.
        _message(db, "repeat", "t1", text="Moved   it.\n")
        _message(db, "sent-2", "t2", text="Moved it.", for_user=True)
        # A fuller answer the person may already have read: kept.
        _message(db, "partial", "t2", text="Moved it, and tonight runs at 4 AM.")
        for message_id in ("sent", "repeat", "sent-2", "partial"):
            _answer(db, message_id, user_message="q1" if message_id in ("sent", "repeat") else "q2")
        migrate_194_to_195(db)
        assert _left(db) == [("partial", "c1.1"), ("sent", "c1.1"), ("sent-2", "c1.1")]


def test_the_log_line_reports_repeats_and_kept_different_answers_apart():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _message(db, "sent", "t1", for_user=True)
        _message(db, "repeat", "t1")
        _message(db, "partial", "t1", cid="c1.2", text="More words.")
        _message(db, "sent-b", "t1", cid="c1.2", text="Fewer.", for_user=True)
        for message_id, cid in (("sent", "c1.1"), ("repeat", "c1.1"), ("partial", "c1.2"), ("sent-b", "c1.2")):
            _answer(db, message_id, cid=cid)
        (line,) = _run(db)
        assert "deleted=1 kept_different=1 total=4" in line


def test_only_a_repeat_in_the_same_turn_of_the_same_comment_is_dropped():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _message(db, "sent", "t1", for_user=True)
        _message(db, "closing", "t1")
        _message(db, "other-turn-closing", "t2")
        _message(db, "only-closing", "t3")
        _message(db, "closing-other-comment", "t1", cid="c1.2")
        for message_id in ("sent", "closing", "other-turn-closing", "only-closing"):
            _answer(db, message_id)
        _answer(db, "closing-other-comment", cid="c1.2")
        migrate_194_to_195(db)
        migrate_194_to_195(db)
        assert _left(db) == [
            ("closing-other-comment", "c1.2"), ("only-closing", "c1.1"),
            ("other-turn-closing", "c1.1"), ("sent", "c1.1"),
        ]
