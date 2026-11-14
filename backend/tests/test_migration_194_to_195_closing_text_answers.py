"""Schema 195: a closing-text answer a sent message in the same Turn already gave is dropped."""
import json
import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.migration_194_to_195_closing_text_answers import migrate_194_to_195


def _message(db, message_id, turn, for_user=False):
    meta = {"turn_id": turn, **({"for_user": True} if for_user else {})}
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,'thread','assistant','[c1.1] x',?,'t')",
        (message_id, json.dumps(meta)),
    )


def _answer(db, message_id, cid="c1.1", user_message="question"):
    db.execute("INSERT INTO chat_comment_answers VALUES ('thread',?,?,?,'t')", (user_message, cid, message_id))


def _left(db):
    return sorted(tuple(row) for row in db.execute("SELECT assistant_message_id,cid FROM chat_comment_answers"))


def test_only_the_unsent_duplicate_of_a_sent_answer_in_the_same_turn_is_dropped():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _message(db, "sent", "t1", for_user=True)
        _message(db, "closing", "t1")
        _message(db, "other-turn-closing", "t2")
        _message(db, "only-closing", "t3")
        _message(db, "closing-other-comment", "t1")
        for message_id in ("sent", "closing", "other-turn-closing", "only-closing"):
            _answer(db, message_id)
        _answer(db, "closing-other-comment", cid="c1.2")
        migrate_194_to_195(db)
        migrate_194_to_195(db)
        assert _left(db) == [
            ("closing-other-comment", "c1.2"), ("only-closing", "c1.1"),
            ("other-turn-closing", "c1.1"), ("sent", "c1.1"),
        ]
