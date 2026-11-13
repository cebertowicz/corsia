"""An assistant step that draws nothing in chat is stamped once, and every reader reads the stamp.

`place_answers` is the one writer of `metadata.chat_drawn = false`. The Focus filter, the
run and stub counts, the saved-answer fields frames carry, the sidebar preview and the update
feed read it; none re-derives it (#2602: the folded line counted a step its list did not show).
"""

import json

import pytest

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.chat_comment_answers import chat_not_drawn_sql, draws_nothing, message_answer_fields
from infra.database import get_db
from infra.queries import insert_message

THREAD = "thread"
COMMENT_META = {"reply_context": {"comments": [{"cid": "c1.1"}]}}


def _seed(db):
    seed_thread(db, THREAD)
    db.execute("UPDATE threads SET user_id=?, next_message_seq=2 WHERE id=?", (OWNER_USER, THREAD))
    db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,seq) VALUES ('target',?,'assistant','target text','2026-01-01',0)", (THREAD,))
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq,input_order) "
        "VALUES ('question',?,'user','',?,'2026-01-01',1,1)", (THREAD, json.dumps(COMMENT_META)),
    )
    db.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) VALUES (?,'target','question','c1.1','2026-01-01')", (THREAD,))
    db.commit()


def _step(db, message_id, text, extra=None):
    meta = {"turn_unbound_reason": "legacy_import", **(extra or {})}
    insert_message(THREAD, "assistant", text, meta, msg_id=message_id, db=db, created_at="2026-01-02")
    db.commit()


def _flag(db, message_id):
    raw = db.execute("SELECT metadata FROM messages WHERE id=?", (message_id,)).fetchone()[0]
    return json.loads(raw).get("chat_drawn", "unset")


@pytest.fixture
def db(isolated_db):
    connection = get_db()
    _seed(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize(("name", "text", "extra", "stamped"), [
    ("answer-only", "[c1.1] Moved.", {}, True),
    ("answer-and-chat-text", "Done.\n\n[c1.1] Moved.", {}, False),
    ("answer-and-reasoning", "[c1.1] Moved.", {"reasoning": "thinking"}, False),
    ("answer-and-tool-calls", "[c1.1] Moved.", {"llm_tool_calls": [{"id": "t1"}]}, False),
    ("answer-and-tool-hints", "[c1.1] Moved.", {"tool_hints": ["Reading a file"]}, False),
    ("empty-reasoning-and-calls", "[c1.1] Moved.", {"reasoning": "", "llm_tool_calls": [], "tool_hints": []}, True),
    ("unknown-comment", "[c9.9] Nobody asked.", {}, False),
    ("no-answers", "Plain text", {}, False),
])
def test_the_writer_stamps_only_a_step_with_nothing_left_for_chat(db, name, text, extra, stamped):
    _step(db, name, text, extra)
    assert _flag(db, name) == (False if stamped else "unset")
    assert (message_answer_fields(THREAD, name, db).get("chat_drawn", "unset")) == (False if stamped else "unset")


def test_the_rule_is_one_function_of_the_text_left_and_the_metadata():
    assert draws_nothing("", None) and draws_nothing("", {"reasoning": None, "tool_hints": []})
    assert not draws_nothing("text", {}) and not draws_nothing("", {"reasoning": "x"})
    assert not draws_nothing("", {"tool_calls": [{"id": "t"}]})


def test_the_read_form_matches_only_the_stamp():
    import sqlite3

    db = sqlite3.connect(":memory:")
    cases = [("false", 1), ("true", 0), ("null", 0), ("0", 0), ("{}", 0), ("not json", 0)]
    for raw, expected in cases:
        value = raw if raw == "not json" else json.dumps({"chat_drawn": json.loads(raw)}) if raw != "{}" else "{}"
        row = db.execute(f"SELECT {chat_not_drawn_sql('m')} FROM (SELECT ? AS m)", (value,)).fetchone()
        assert (1 if row[0] else 0) == expected, raw


def _page(db):
    from api.thread_paging import read_authorized_page_with_summaries

    return read_authorized_page_with_summaries(db, THREAD, None, None, user_id=OWNER_USER)


def _worked_turn(db):
    """One turn: an answer-only step, a step with a tool call, then the said-to-you reply."""
    _step(db, "answer-only", "[c1.1] Moved.")
    _step(db, "worked", "", {"tool_hints": ["Reading a file"], "llm_tool_calls": [{"id": "t1"}]})
    _step(db, "reply", "All done.", {"for_user": True})


def test_the_page_read_drops_the_stamped_step_and_keeps_the_rest(db):
    _worked_turn(db)
    messages, *_rest = _page(db)
    ids = {m["id"] for m in messages}
    assert "answer-only" not in ids and {"worked", "reply"} <= ids


def test_the_focus_page_counts_only_steps_it_lists(db):
    """#2602: the run's count and weight cover the one step the opened run lists, not the stamped one."""
    from api.thread_focus_page import read_authorized_focus_page

    _worked_turn(db)
    page = read_authorized_focus_page(db, THREAD, 50, None, user_id=OWNER_USER)
    assert "answer-only" not in json.dumps(page, default=str)
    stepped = [run for run in page["runs"] if "steps" in run["kinds"]]
    assert [(run["first_id"], run["count"], run["weight"]) for run in stepped] == [("worked", 1, 1)]


def test_a_comment_only_turn_leaves_no_run_for_its_step(db):
    from api.thread_focus_page import read_authorized_focus_page

    _step(db, "answer-only", "[c1.1] Moved.")
    page = read_authorized_focus_page(db, THREAD, 50, None, user_id=OWNER_USER)
    assert "answer-only" not in json.dumps(page, default=str)
    assert not [run for run in page["runs"] if "steps" in run["kinds"]]


def test_the_sidebar_preview_skips_the_stamped_step(db):
    from infra.sidebar_preview import sidebar_update

    _step(db, "answer-only", "[c1.1] Moved.")
    assert sidebar_update(THREAD)["last_message"] == "target text"
