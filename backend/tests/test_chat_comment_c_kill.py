"""Design C kill test: a comment-only input never reaches a chat client.

Design C keeps the comment's input row as the delivery record and marks it with an
empty `chat_visible_content` row. Two seams then keep it out of chat: one clause in
`NEVER_DRAWN_FILTER`, and `read_input_broadcast_row` returning None for it. This
test applies both seams inside the test only. It then drives every path that can
hand a user row to a chat client. A path marked xfail is a leak those two seams don't
cover.
"""

import asyncio
import json

import pytest

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.database import get_db

THREAD = "thread"
COMMENT = "comment-input"
NOTE = "kill-test-note"
C_CLAUSE = (
    "AND NOT (role = 'user' AND id IN "
    "(SELECT message_id FROM chat_visible_content WHERE content = '')) "
)
# infra/sidebar_preview.py's preview as dev has it, without this branch's comment-only predicate.
DEV_PREVIEW = """(SELECT substr({visible}, 1, 100) FROM messages m {join}
    WHERE m.thread_id=s.id AND m.role IN ('user','assistant') AND (cv.content IS NULL OR cv.content != '')
    ORDER BY m.created_at DESC LIMIT 1)"""


@pytest.fixture
def c_seams(monkeypatch):
    import infra.focus_rows as focus_rows
    import infra.input_admission as input_admission
    import infra.send_broadcast as send_broadcast
    import registry.removal_events as removal_events

    original_place = input_admission.place_comments

    def place_with_fact(db, thread_id, user_message_id, metadata, created_at):
        # Design C's one added write: a comment-only input draws nothing in chat.
        original_place(db, thread_id, user_message_id, metadata, created_at)
        row = db.execute("SELECT content FROM messages WHERE id=?", (user_message_id,)).fetchone()
        if (metadata or {}).get("reply_context", {}).get("comments") and not (metadata or {}).get("attachments") \
                and not (row["content"] or "").strip():
            db.execute("INSERT OR REPLACE INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,'')",
                       (user_message_id, thread_id))

    monkeypatch.setattr(input_admission, "place_comments", place_with_fact)

    import api.thread_focus_page as focus_page
    import api.thread_paging as paging

    original_filter = focus_rows.NEVER_DRAWN_FILTER
    original_read = send_broadcast.read_input_broadcast_row

    def refusing(message_id, *, db=None):
        connection = db if db is not None else get_db()
        try:
            hidden = connection.execute(
                "SELECT 1 FROM messages m JOIN chat_visible_content cv ON cv.message_id=m.id "
                "WHERE m.id=? AND m.role='user' AND cv.content=''", (message_id,),
            ).fetchone()
        finally:
            if db is None:
                connection.close()
        return None if hidden else original_read(message_id, db=db)

    # The real change edits the function itself, so every binding of it changes.
    for module in (send_broadcast, removal_events):
        monkeypatch.setattr(module, "read_input_broadcast_row", refusing)
    widened = original_filter + C_CLAUSE
    for module in (focus_rows, paging, focus_page):
        monkeypatch.setattr(module, "NEVER_DRAWN_FILTER", widened)
    # SQL the Focus reader composed from the filter at import.
    for name in ("_VISIBLE_SQL", "_RUNS_SQL", "_OLDER_EXISTS_SQL"):
        built = getattr(focus_page, name)
        assert original_filter in built
        monkeypatch.setattr(focus_page, name, built.replace(original_filter, widened))


def _comment_metadata():
    return {"reply_context": {"comments": [{
        "id": "key-1", "cid": "c1.1", "seq": 1, "from_off": 0, "to_off": 6,
        "quote": "target", "anchor_prefix": "", "anchor_suffix": "", "note": NOTE,
    }]}}


@pytest.fixture(params=["queued", "delivered"])
def thread(request, isolated_db, c_seams):
    from infra.input_admission import accept_thread_input, stamped_human_metadata

    db = get_db()
    seed_thread(db, THREAD)
    db.execute("UPDATE threads SET user_id=?, next_message_seq=4 WHERE id=?", (OWNER_USER, THREAD))
    for message_id, seq, text in (("target", 1, "target text"), ("after", 3, "after text")):
        db.execute(
            "INSERT INTO messages(id,thread_id,role,content,created_at,seq,delivery_state) "
            "VALUES (?,?,'assistant',?,'2026-01-01',?,'delivered')", (message_id, THREAD, text, seq),
        )
    db.commit()
    # The same human stamping every send path applies (infra/thread_driver.py:68).
    metadata = stamped_human_metadata(db, "user", COMMENT, _comment_metadata(), viewing_document=None, thread_id=THREAD)
    accept_thread_input(
        THREAD, role="user", content="", intent="apply_now", message_id=COMMENT, metadata=metadata, db=db,
    )
    assert db.execute("SELECT content FROM chat_visible_content WHERE message_id=?", (COMMENT,)).fetchone()[0] == ""
    if request.param == "delivered":
        db.execute(
            "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('turn-c',?,0,'m','completed','2026-01-02','test')", (THREAD,),
        )
        db.execute(
            "UPDATE messages SET delivery_state='delivered', seq=2, active_turn_id='turn-c' WHERE id=?",
            (COMMENT,),
        )
    db.commit()
    yield db, request.param
    db.close()


def _leaks(payload) -> bool:
    return COMMENT in json.dumps(payload, default=str)


def test_page_read_and_turn_summaries(thread):
    from api.thread_paging import read_authorized_page_with_summaries

    db, _state = thread
    messages, _more, _oldest, summaries = read_authorized_page_with_summaries(db, THREAD, None, None, user_id=OWNER_USER)
    assert {"target", "after"} <= {m["id"] for m in messages}
    assert not _leaks(messages)
    assert "turn-c" not in summaries  # no blank Worked line for a Turn whose only input is hidden


def test_focus_page(thread):
    from api.thread_focus_page import read_authorized_focus_page

    db, _state = thread
    page = read_authorized_focus_page(db, THREAD, 50, None, user_id=OWNER_USER)
    assert "target" in json.dumps(page, default=str)
    assert not _leaks(page)


def test_run_range(thread):
    from api.thread_focus_page import read_authorized_run_range

    db, _state = thread
    assert not _leaks(read_authorized_run_range(db, THREAD, 50, "target", "after", None, user_id=OWNER_USER))


def test_comment_record_reaches_its_thread_only(thread):
    from api.chat_comment_views import _threads

    db, _state = thread
    result = _threads(OWNER_USER, THREAD, ["target"])
    assert [m["id"] for m in result["messages"]] == ["target"]
    assert result["messages"][0]["chat_comments"][0]["cid"] == "c1.1"


def test_sidebar_preview_without_branch_predicate(thread, monkeypatch):
    import infra.sidebar_preview as preview
    from infra.chat_comment_answers import VISIBLE_CONTENT, VISIBLE_CONTENT_JOIN

    monkeypatch.setattr(preview, "LAST_MESSAGE_PREVIEW", DEV_PREVIEW.format(visible=VISIBLE_CONTENT, join=VISIBLE_CONTENT_JOIN))
    assert preview.sidebar_update(THREAD)["last_message"] == "after text"


def test_rebroadcasts_through_broadcast_message_rows(thread):
    from infra.send_broadcast import broadcast_message_rows

    frames = []
    asyncio.run(broadcast_message_rows(THREAD, (COMMENT,), broadcast=lambda tid, payload: frames.append(payload)))
    assert frames == []


def test_turn_input_still_renders_the_comment(thread):
    from infra.input_render import render_claimed_batch
    from infra.thread_inputs import start_idle_turn

    db, state = thread
    if state != "queued":
        pytest.skip("a Turn claims a queued input")
    admission = start_idle_turn(THREAD, turn_id="turn-k", boot_epoch="test", started_at="2026-01-03", db=db)
    batch = getattr(admission, "batch", admission)
    assert [item.message_id for item in batch.items] == [COMMENT]
    rendered = render_claimed_batch(batch).content
    assert "c1.1" in rendered and NOTE in rendered


def test_subagent_feed_receipt_carries_no_row():
    from types import SimpleNamespace

    from ws.ingress import _accepted_input_payload

    payload = _accepted_input_payload(THREAD, "input", SimpleNamespace(message_id=COMMENT, created_at="t", turn_id="turn", turn_order=1, seq=2))
    assert not {"content", "metadata", "message"} & set(payload)


@pytest.mark.xfail(strict=True, reason="LEAK: cancel route projects the row itself (api/thread_inbox_actions.py:46-63,92)")
def test_cancel_route_broadcast(thread, monkeypatch):
    import infra.state as state
    from api.thread_inbox_actions import run_inbox_message_action

    _db, phase = thread
    if phase != "queued":
        pytest.skip("only an unclaimed input can be cancelled")
    frames = []

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(state, "broadcast_to_thread", lambda tid, payload, **_k: frames.append(payload))
    monkeypatch.setattr("infra.send_broadcast.broadcast_newest_error", nothing)
    monkeypatch.setattr("infra.thread_execution.drive_thread", nothing)
    asyncio.run(run_inbox_message_action(THREAD, COMMENT, OWNER_USER, action="cancel"))
    assert not _leaks(frames)


@pytest.mark.xfail(strict=True, reason="LEAK: Retry admits with broadcast_input=True; broadcast_accepted_input projects the row itself (infra/send_service.py:110-129)")
def test_retry_route_broadcast(thread, monkeypatch):
    import infra.send_service as send_service
    import infra.state as state
    from api.thread_inbox_actions import retry_thread_delivery_now
    from infra.send_post_admission import publish_accepted_admission

    db, phase = thread
    if phase != "delivered":
        pytest.skip("Retry re-sends an ended attempt")
    db.execute("UPDATE messages SET delivery_state='failed', delivery_error='boom' WHERE id=?", (COMMENT,))
    db.commit()
    frames = []

    async def publish_only(admission, _row, *, text, metadata, role, broadcast_input, broadcast_exclude_ws, **_k):
        return await publish_accepted_admission(
            admission, text, metadata, role=role, broadcast_input=broadcast_input,
            broadcast_exclude_ws=broadcast_exclude_ws, delivery_after_failed_prefix=None, on_admitted=None,
        )

    async def no_images(*_a):
        return []

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(state, "broadcast_to_thread", lambda tid, payload, **_k: frames.append(payload))
    monkeypatch.setattr(send_service, "_deliver_admission", publish_only)
    monkeypatch.setattr(send_service, "_bind_image_identity", no_images)
    monkeypatch.setattr(send_service, "_boot_epoch", lambda: "test")
    monkeypatch.setattr(send_service, "broadcast_newest_error", nothing)
    monkeypatch.setattr("jobs.corrections_recording.notify_if_owner", lambda *_a, **_k: None)
    result = asyncio.run(retry_thread_delivery_now(THREAD, COMMENT, OWNER_USER))
    copy = result["revived_message_id"]
    assert db.execute("SELECT content FROM chat_visible_content WHERE message_id=?", (copy,)).fetchone()[0] == ""
    assert not [f for f in frames if copy in json.dumps(f)]
