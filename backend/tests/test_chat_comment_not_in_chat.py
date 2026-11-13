"""A comment-only input never reaches a chat client as a chat row.

Its input row stays the delivery record, and `place_comments` marks it with an
empty `chat_visible_content` row. `NEVER_DRAWN_FILTER` keeps it out of every page
read, and `broadcast_input_rows` sends chat clients a `chat_comments_changed`
notice in its place. Each test drives a path that can hand a user row to a chat
client. The `ordinary` cases are the control: the same comment sent with text is a
chat message and still shows.
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


def _comment_metadata():
    return {"reply_context": {"comments": [{
        "id": "key-1", "cid": "c1.1", "seq": 1, "from_off": 0, "to_off": 6,
        "quote": "target", "anchor_prefix": "", "anchor_suffix": "", "note": NOTE,
    }]}}


def _seed(db, content):
    from infra.input_admission import accept_thread_input, stamped_human_metadata

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
        THREAD, role="user", content=content, intent="apply_now", message_id=COMMENT, metadata=metadata, db=db,
    )


def _deliver(db):
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
        "VALUES ('turn-c',?,0,'m','completed','2026-01-02','test')", (THREAD,),
    )
    db.execute("UPDATE messages SET delivery_state='delivered', seq=2, active_turn_id='turn-c' WHERE id=?", (COMMENT,))


@pytest.fixture(params=["queued", "delivered"])
def thread(request, isolated_db):
    db = get_db()
    _seed(db, "")
    assert db.execute("SELECT content FROM chat_visible_content WHERE message_id=?", (COMMENT,)).fetchone()[0] == ""
    if request.param == "delivered":
        _deliver(db)
    db.commit()
    yield db, request.param
    db.close()


@pytest.fixture
def ordinary(isolated_db):
    """The control: the same comment sent with chat text is a chat message."""
    db = get_db()
    _seed(db, "also this")
    _deliver(db)
    db.commit()
    assert db.execute("SELECT 1 FROM chat_visible_content WHERE message_id=?", (COMMENT,)).fetchone() is None
    yield db
    db.close()


def _leaks(payload) -> bool:
    return COMMENT in json.dumps(payload, default=str)


NOTICE = {"type": "chat_comments_changed", "thread_id": THREAD, "target_seqs": [1]}


def notices(frames):
    return [frame for frame in frames if frame["type"] == "chat_comments_changed"]


def user_rows(frames):
    """Every user row any frame hands a chat client."""
    rows = [row for frame in frames for row in ([frame["message"]] if "message" in frame else frame.get("messages", []))]
    return [row for row in rows if row["role"] == "user"]


def _page(db):
    from api.thread_paging import read_authorized_page_with_summaries

    return read_authorized_page_with_summaries(db, THREAD, None, None, user_id=OWNER_USER)


def test_page_read_and_turn_summaries(thread):
    db, _state = thread
    messages, _more, _oldest, summaries = _page(db)
    assert {"target", "after"} <= {m["id"] for m in messages}
    assert not _leaks(messages)
    assert "turn-c" not in summaries  # no blank Worked line for a Turn whose only input is hidden


def test_ordinary_input_still_shows_in_the_page(ordinary):
    messages, *_rest = _page(ordinary)
    assert COMMENT in {m["id"] for m in messages}


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


def test_comment_record_reaches_its_thread_with_its_delivery(thread):
    from api.chat_comment_views import _threads

    db, state = thread
    result = _threads(OWNER_USER, THREAD, ["target"])
    assert [m["id"] for m in result["messages"]] == ["target"]
    comment = result["messages"][0]["chat_comments"][0]
    assert (comment["cid"], comment["delivery_state"], comment["delivery_error"]) == ("c1.1", state, None)


def test_sidebar_preview(thread):
    from infra.sidebar_preview import sidebar_update

    assert sidebar_update(THREAD)["last_message"] == "after text"


def _broadcast(ids):
    from infra.send_broadcast import broadcast_message_rows

    frames = []
    asyncio.run(broadcast_message_rows(THREAD, ids, broadcast=lambda tid, payload: frames.append(payload)))
    return frames


def test_rebroadcast_sends_a_notice_not_the_row(thread):
    assert _broadcast((COMMENT,)) == [NOTICE]


def test_a_batch_keeps_its_chat_rows_in_one_frame(thread):
    db, _state = thread
    for message_id in ("before", "later"):
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at) VALUES (?,?,'user','hi','2026-01-04')",
                   (message_id, THREAD))
    db.commit()
    frames = _broadcast(("before", COMMENT, "later"))
    assert [m["id"] for m in frames[0]["messages"]] == ["before", "later"]
    assert frames[1:] == [NOTICE]


def test_ordinary_input_broadcasts_its_row_unchanged(ordinary):
    from infra.send_broadcast import input_rows_payload, read_input_broadcast_row

    expected = input_rows_payload(THREAD, (read_input_broadcast_row(COMMENT),))
    assert _broadcast((COMMENT,)) == [expected]
    assert expected["message"]["id"] == COMMENT and expected["message"]["content"] == "also this"


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


@pytest.fixture
def frames(monkeypatch):
    import infra.state as state

    captured = []

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(state, "broadcast_to_thread", lambda tid, payload, **_k: captured.append(payload))
    monkeypatch.setattr("infra.send_broadcast.broadcast_newest_error", nothing)
    monkeypatch.setattr("infra.send_service.broadcast_newest_error", nothing)
    monkeypatch.setattr("infra.thread_execution.drive_thread", nothing)
    return captured


def test_cancel_route_broadcast(thread, frames):
    from api.thread_inbox_actions import run_inbox_message_action

    _db, phase = thread
    if phase != "queued":
        pytest.skip("only an unclaimed input can be cancelled")
    asyncio.run(run_inbox_message_action(THREAD, COMMENT, OWNER_USER, action="cancel"))
    assert not _leaks(frames)
    assert user_rows(frames) == [] and notices(frames) == [NOTICE]


@pytest.fixture
def publish_only(monkeypatch):
    """Admit for real, publish the accepted input, and start no Turn."""
    import infra.send_service as send_service
    from infra.send_post_admission import publish_accepted_admission

    async def deliver(admission, _row, *, text, metadata, role, broadcast_input, broadcast_exclude_ws, **_k):
        return await publish_accepted_admission(
            admission, text, metadata, role=role, broadcast_input=broadcast_input,
            broadcast_exclude_ws=broadcast_exclude_ws, delivery_after_failed_prefix=None, on_admitted=None,
        )

    async def no_images(*_a):
        return []

    monkeypatch.setattr(send_service, "_deliver_admission", deliver)
    monkeypatch.setattr(send_service, "_bind_image_identity", no_images)
    monkeypatch.setattr(send_service, "_boot_epoch", lambda: "test")
    monkeypatch.setattr("jobs.corrections_recording.notify_if_owner", lambda *_a, **_k: None)


def test_retry_route_broadcast(thread, frames, publish_only):
    from api.thread_inbox_actions import retry_thread_delivery_now

    db, phase = thread
    if phase != "delivered":
        pytest.skip("Retry re-sends an ended attempt")
    db.execute("UPDATE messages SET delivery_state='failed', delivery_error='boom' WHERE id=?", (COMMENT,))
    db.commit()
    result = asyncio.run(retry_thread_delivery_now(THREAD, COMMENT, OWNER_USER))
    copy = result["revived_message_id"]
    assert db.execute("SELECT content FROM chat_visible_content WHERE message_id=?", (copy,)).fetchone()[0] == ""
    assert not [f for f in frames if copy in json.dumps(f)]
    assert user_rows(frames) == [] and notices(frames) == [NOTICE]
