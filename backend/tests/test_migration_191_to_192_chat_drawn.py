"""Schema 192: steps stored before 192 get the flag the writer stamps from 192 on, by the same rule."""
import json
import sqlite3
from contextlib import closing

from infra.chat_comment_answers import draws_nothing
from infra.database import initialize_schema
from infra.migration_191_to_192_chat_drawn import migrate_191_to_192


def _step(db, message_id, visible, metadata, role="assistant"):
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,'thread',?,'[c1.1] x',?,'t')",
        (message_id, role, json.dumps(metadata) if metadata is not None else None),
    )
    if visible is not None:
        db.execute("INSERT INTO chat_visible_content VALUES (?,'thread',?)", (message_id, visible))


def _flags(db):
    rows = db.execute("SELECT id,metadata FROM messages WHERE json_valid(metadata)").fetchall()
    return {mid: json.loads(raw).get("chat_drawn") for mid, raw in rows}


def test_the_data_step_stamps_exactly_what_the_writer_rule_stamps():
    """The data step and the writer decide each step alike, so a step never draws live and vanishes on reload."""
    metadata = {
        "none": None, "empty": {}, "turn": {"turn_id": "t"},
        "reasoning": {"reasoning": "thinking"}, "empty-reasoning": {"reasoning": ""},
        "null-reasoning": {"reasoning": None}, "false-reasoning": {"reasoning": False},
        "zero-reasoning": {"reasoning": 0}, "text-zero-reasoning": {"reasoning": "0"},
        "llm-calls": {"llm_tool_calls": [{"id": "1"}]}, "empty-llm-calls": {"llm_tool_calls": []},
        "calls": {"tool_calls": [{"id": "1"}]}, "empty-calls": {"tool_calls": []},
        "hints": {"tool_hints": ["Reading"]}, "empty-hints": {"tool_hints": []},
        "string-calls": {"llm_tool_calls": "x"}, "object-calls": {"tool_calls": {"id": 1}},
        "for-user": {"for_user": True},
    }
    cases = {}
    for name, meta in metadata.items():
        cases[f"{name}/no-text"] = ("", meta)
        cases[f"{name}/text"] = ("kept", meta)
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        for message_id, (visible, meta) in cases.items():
            _step(db, message_id, visible, meta)
        migrate_191_to_192(db)
        migrate_191_to_192(db)
        stamped = {mid for mid, flag in _flags(db).items() if flag is False}
    assert stamped == {mid for mid, (visible, meta) in cases.items() if draws_nothing(visible, meta)}
    assert {"empty/no-text", "empty-reasoning/no-text", "empty-calls/no-text", "none/no-text"} <= stamped
    assert not {"reasoning/no-text", "llm-calls/no-text", "hints/no-text", "empty/text"} & stamped


def test_it_skips_what_never_placed_an_answer_and_rows_that_are_not_steps():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _step(db, "no-answer", None, {})
        _step(db, "a-user", "", {}, role="user")
        _step(db, "commented-on", "", {})
        db.execute(
            "INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) "
            "VALUES ('thread','commented-on','q','c1.1','t')"
        )
        _step(db, "answer-only", "", {})
        db.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES ('bad','thread','assistant','','not json','t')")
        db.execute("INSERT INTO chat_visible_content VALUES ('bad','thread','')")
        migrate_191_to_192(db)
        flags = _flags(db)
        assert db.execute("SELECT metadata FROM messages WHERE id='bad'").fetchone()[0] == "not json"
    assert {mid for mid, flag in flags.items() if flag is False} == {"answer-only"}


def test_it_keeps_every_other_metadata_key():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _step(db, "answer-only", "", {"turn_id": "t", "step_type": "text"})
        migrate_191_to_192(db)
        meta = json.loads(db.execute("SELECT metadata FROM messages").fetchone()[0])
    assert meta == {"turn_id": "t", "step_type": "text", "chat_drawn": False}
