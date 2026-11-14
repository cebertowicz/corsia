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
    db.execute("INSERT INTO chat_visible_content(message_id,thread_id,content) VALUES ('question',?,'')", (THREAD,))
    db.commit()


def _step(db, message_id, text, extra=None):
    meta = {"turn_unbound_reason": "legacy_import", "for_user": True, **(extra or {})}
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
    """#2602: the run's count and weight cover the steps the opened run lists (the target and the tool step), not the stamped one."""
    from api.thread_focus_page import read_authorized_focus_page

    _worked_turn(db)
    page = read_authorized_focus_page(db, THREAD, 50, None, user_id=OWNER_USER)
    assert "answer-only" not in json.dumps(page, default=str)
    stepped = [run for run in page["runs"] if "steps" in run["kinds"]]
    assert [(run["first_id"], run["count"], run["weight"]) for run in stepped] == [("target", 2, 1)]


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


def test_a_stamped_for_user_step_broadcasts_the_stamp_it_was_saved_with(db):
    """send_broadcast re-derives nothing: the update row carries the saved stamp, in its fields and its metadata."""
    from infra.send_broadcast import project_input_message, read_input_broadcast_row

    _step(db, "update-only", "[c1.1] Moved.", {"for_user": True})
    projected = project_input_message(read_input_broadcast_row("update-only", db=db))
    assert projected["chat_drawn"] is False and projected["metadata"]["chat_drawn"] is False
    _step(db, "update-with-text", "Done.\n\n[c1.1] Moved.", {"for_user": True})
    projected = project_input_message(read_input_broadcast_row("update-with-text", db=db))
    assert "chat_drawn" not in projected and "chat_drawn" not in projected["metadata"]


def test_a_tool_call_appended_to_a_stamped_step_clears_the_stamp(db):
    """A Codex text step is saved first and tool calls are appended to the same row later: a step with a tool call draws."""
    from subagents.assistant_step_tool_append import append_tool_use_locked

    _step(db, "text-then-tool", "[c1.1] Moved.", {"provider_turn_id": "p1"})
    assert _flag(db, "text-then-tool") is False
    append_tool_use_locked(
        THREAD, "text-then-tool", turn_id=None, tool_id="t1", name="read_file", arguments={}, hint="Reading",
        provider_turn_id="p1", db=db)
    db.commit()
    assert _flag(db, "text-then-tool") == "unset"
    messages, *_rest = _page(db)
    assert "text-then-tool" in {m["id"] for m in messages}


def test_the_sidebar_preview_skips_a_stamped_step_but_the_update_dot_still_counts_it(db):
    from infra.thread_updates import latest_by_thread_with_attention, record_marked

    _step(db, "answer-only", "[c1.1] Moved.", {"for_user": True})
    record_marked(db, thread_id=THREAD, message_id="target", node_id=None, created_at="2026-01-01T00:00:00")
    record_marked(db, thread_id=THREAD, message_id="answer-only", node_id=None, created_at="2026-01-02T00:00:00")
    db.commit()
    display, attention = latest_by_thread_with_attention(db, [THREAD])
    assert display[THREAD]["message_id"] == "target"
    assert attention[THREAD]["message_id"] == "answer-only"


def test_an_ask_hold_maps_to_the_latest_step_chat_draws(db):
    from api.thread_cards import _turn_step

    for message_id, text, extra, seq in (
        ("worked", "", {"turn_id": "turn-1", "tool_hints": ["Reading"], "llm_tool_calls": [{"id": "t1"}]}, 2),
        ("answer-only", "[c1.1] Moved.", {"turn_id": "turn-1", "for_user": True}, 3),
    ):
        db.execute("UPDATE threads SET next_message_seq=? WHERE id=?", (seq, THREAD))
        insert_message(THREAD, "assistant", text, extra, msg_id=message_id, db=db, created_at="2026-01-02")
    db.commit()
    assert _flag(db, "answer-only") is False
    assert _turn_step(db, THREAD, "turn-1", "2026-01-03") == "worked"
