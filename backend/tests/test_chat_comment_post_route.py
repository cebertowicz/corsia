"""POST /api/threads/{id}/chat-comments: a comment posts from its own box, never as a chat row."""

import asyncio
import json

import pytest
from fastapi import HTTPException

from auth_fixtures import OWNER_USER
from doc_feedback_helpers import seed_thread
from infra.database import get_db
from test_chat_comment_not_in_chat import (  # noqa: F401
    NOTE, NOTICE, THREAD, _comment_metadata, frames, notices, publish_only, user_rows,
)

USER = {"id": OWNER_USER}


@pytest.fixture
def target(isolated_db):
    db = get_db()
    seed_thread(db, THREAD)
    db.execute("UPDATE threads SET user_id=?, next_message_seq=2 WHERE id=?", (OWNER_USER, THREAD))
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,created_at,seq,delivery_state) "
        "VALUES ('target',?,'assistant','target text','2026-01-01',1,'delivered')", (THREAD,),
    )
    db.commit()
    yield db
    db.close()


def _post(input_id="box-1", thread_id=THREAD, comment=None):
    from api.chat_comment_views import PostRequest, post_chat_comment

    body = PostRequest(input_id=input_id, comment=comment or _comment_metadata()["reply_context"]["comments"][0])
    return asyncio.run(post_chat_comment(thread_id, body, user=USER))


def test_post_stores_the_comment_and_answers_with_its_thread(target, frames, publish_only):
    result = _post()
    assert (result["message_id"], result["delivery_state"], result["delivery_error"]) == ("box-1", "queued", None)
    comment = result["messages"][0]["chat_comments"][0]
    assert (result["messages"][0]["id"], comment["note"], comment["delivery_state"]) == ("target", NOTE, "queued")
    assert target.execute("SELECT content FROM chat_visible_content WHERE message_id='box-1'").fetchone()[0] == ""
    # Every client hears which commented row changed; none gets the input row.
    assert user_rows(frames) == [] and notices(frames) == [NOTICE]


def test_a_second_press_answers_with_the_stored_comment(target, frames, publish_only):
    first = _post()
    assert _post() == first
    assert target.execute("SELECT COUNT(*) FROM messages WHERE id='box-1'").fetchone()[0] == 1


def test_a_refused_comment_is_not_stored(target, frames, publish_only):
    with pytest.raises(HTTPException) as refused:
        _post(comment={"cid": "c1.1", "seq": 99, "note": "on nothing"})
    assert refused.value.status_code == 400
    assert target.execute("SELECT 1 FROM messages WHERE id='box-1'").fetchone() is None
    assert user_rows(frames) == notices(frames) == []


def test_a_comment_on_a_person_row_is_refused_not_hidden(target, frames, publish_only):
    # It would have no thread to show in, so it is refused rather than stored hidden from chat.
    target.execute(
        "INSERT INTO messages(id,thread_id,role,content,created_at,seq,delivery_state) "
        "VALUES ('mine',?,'user','my text','2026-01-01',2,'delivered')", (THREAD,),
    )
    target.execute("UPDATE threads SET next_message_seq=3 WHERE id=?", (THREAD,))
    target.commit()
    with pytest.raises(HTTPException) as refused:
        _post(comment={**_comment_metadata()["reply_context"]["comments"][0], "seq": 2})
    assert refused.value.status_code == 400
    assert target.execute("SELECT 1 FROM messages WHERE id='box-1'").fetchone() is None
    assert target.execute("SELECT 1 FROM chat_visible_content WHERE message_id='box-1'").fetchone() is None


def test_stored_then_undelivered_answers_with_the_failed_comment(target, frames, publish_only, monkeypatch):
    import infra.send_service as send_service
    from infra.send_types import SendRejected

    real_send = send_service.send_thread_input

    async def send_then_fail(*args, **kwargs):
        await real_send(*args, **kwargs)
        target.execute("UPDATE messages SET delivery_state='failed', delivery_error='Not delivered' WHERE id='box-1'")
        target.commit()
        raise SendRejected(409, "Not delivered", input_undeliverable=True)

    monkeypatch.setattr(send_service, "send_thread_input", send_then_fail)
    result = _post()
    # The box closes on it: the comment's own thread holds the failure and its Retry.
    assert (result["delivery_state"], result["messages"][0]["chat_comments"][0]["delivery_state"]) == ("failed", "failed")


def test_an_undeliverable_input_that_was_never_stored_is_refused(target, frames, publish_only, monkeypatch):
    import infra.send_service as send_service
    from infra.send_types import SendRejected

    async def refuse(*_args, **_kwargs):
        raise SendRejected(502, "Provider refused the input", input_undeliverable=True)

    monkeypatch.setattr(send_service, "send_thread_input", refuse)
    with pytest.raises(HTTPException) as refused:
        _post()
    assert (refused.value.status_code, refused.value.detail) == (502, "Provider refused the input")


def test_another_persons_thread_is_not_found(target, frames, publish_only):
    target.execute("UPDATE threads SET user_id='someone-else' WHERE id=?", (THREAD,))
    target.commit()
    with pytest.raises(HTTPException) as missing:
        _post()
    assert (missing.value.status_code, missing.value.detail) == (404, "chat_not_found")


def test_a_post_without_its_comment_is_refused_as_malformed(target, frames, publish_only):
    from api.chat_comment_views import PostRequest, post_chat_comment

    with pytest.raises(HTTPException) as refused:
        asyncio.run(post_chat_comment(THREAD, PostRequest(input_id="box-1"), user=USER))
    assert (refused.value.status_code, refused.value.detail) == (400, "comment_missing")


def _retry(message_id="box-1"):
    from api.chat_comment_views import PostRequest, post_chat_comment

    return asyncio.run(post_chat_comment(THREAD, PostRequest(retry_of=message_id), user=USER))


def _end(state, message_id="box-1"):
    target_db = get_db()
    target_db.execute("UPDATE messages SET delivery_state=?, delivery_error='provider_error' WHERE id=?", (state, message_id))
    target_db.commit()
    target_db.close()


@pytest.mark.parametrize("state, warned", [("failed", False), ("ambiguous", True)])
def test_retry_posts_the_ended_comment_again_in_its_place(target, frames, publish_only, state, warned):
    from core.message_history import render_agent_envelope
    from core.reply_context import DELIVERY_WARNING

    _post()
    _end(state)
    frames.clear()
    result = _retry()
    copy = result["message_id"]
    assert copy != "box-1" and result["delivery_state"] == "queued"
    # The thread now shows the Retry, not the ended comment beside it.
    assert [(c["user_message_id"], c["note"]) for c in result["messages"][0]["chat_comments"]] == [(copy, NOTE)]
    content, metadata = target.execute("SELECT content,metadata FROM messages WHERE id=?", (copy,)).fetchone()
    metadata = json.loads(metadata)
    assert (content, metadata["retried_from_message_id"]) == ("", "box-1")
    assert target.execute("SELECT content FROM chat_visible_content WHERE message_id=?", (copy,)).fetchone()[0] == ""
    assert user_rows(frames) == [] and notices(frames) == [NOTICE]
    rendered = render_agent_envelope(content, metadata).content
    assert (DELIVERY_WARNING in rendered) is warned
    assert NOTE in rendered


def test_a_second_retry_press_is_the_same_input(target, frames, publish_only):
    _post()
    _end("failed")
    first = _retry()
    assert _retry() == first
    assert target.execute(
        "SELECT COUNT(*) FROM messages WHERE json_extract(metadata,'$.retried_from_message_id')='box-1'"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("state", ["delivered", "cancelled"])
def test_retry_of_a_comment_already_delivered_or_cancelled_answers_with_it_as_it_stands(target, frames, publish_only, state):
    # Say another device settled it between the render and the press: no error, and no copy sent.
    _post()
    _end(state)
    result = _retry()
    assert (result["message_id"], result["delivery_state"]) == ("box-1", state)
    assert result["messages"][0]["chat_comments"][0]["delivery_state"] == state
    assert target.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 1


def _reply_under(root="box-1"):
    db = get_db()
    root_cid = db.execute("SELECT cid FROM chat_comments WHERE user_message_id=?", (root,)).fetchone()[0]
    db.close()
    reply = {**_comment_metadata()["reply_context"]["comments"][0], "id": "key-2",
             "note": "my reply", "root_user_message_id": root, "root_cid": root_cid}
    _post("reply-1", comment=reply)


def _thread_states():
    from api.chat_comment_views import _threads

    comments = _threads(OWNER_USER, THREAD, ["target"])["messages"][0]["chat_comments"]
    return {c["user_message_id"]: c["delivery_state"] for c in comments}


def test_a_retried_comment_with_a_reply_shows_its_copys_delivery(target, frames, publish_only):
    # The reply keeps the original's thread beside the copy's, so the original shows the copy's state:
    # its failure clears once the copy is delivered, and comes back with Retry if the copy ends too.
    _post()
    _end("failed")
    _reply_under()
    copy = _retry()["message_id"]
    assert _thread_states() == {"box-1": "queued", copy: "queued"}
    _end("delivered", copy)
    assert _thread_states() == {"box-1": "delivered", copy: "delivered"}
    _end("failed", copy)
    assert _thread_states() == {"box-1": "failed", copy: "failed"}
    # Its Retry then sends the copy again, as chat's Retry does.
    again = _retry()["message_id"]
    assert again not in ("box-1", copy)
    assert json.loads(target.execute("SELECT metadata FROM messages WHERE id=?", (again,)).fetchone()[0])[
        "retried_from_message_id"] == copy
    assert _thread_states()["box-1"] == "queued"


def test_retry_of_a_queued_comment_drives_it_in_place_as_chat_does(target, frames, publish_only):
    _post()
    result = _retry()
    assert (result["message_id"], result["delivery_state"]) == ("box-1", "queued")
    assert target.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 1


def test_an_uncertain_chat_message_retry_still_leads_with_the_warning(target, frames, publish_only):
    # Only a comment-only input carries the warning in its reply context; a typed message keeps chat's prefix.
    from api.thread_inbox_actions import retry_thread_delivery_now
    from core.reply_context import DELIVERY_WARNING

    target.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state) "
        "VALUES ('typed',?,'user','hello','{}','t','ambiguous')", (THREAD,),
    )
    target.commit()
    copy = asyncio.run(retry_thread_delivery_now(THREAD, "typed", OWNER_USER))["revived_message_id"]
    content, metadata = target.execute("SELECT content,metadata FROM messages WHERE id=?", (copy,)).fetchone()
    assert content == f"{DELIVERY_WARNING}\n\nhello"
    assert json.loads(metadata)["possible_duplicate"] is True and "reply_context" not in json.loads(metadata)


def test_retry_of_a_chat_message_is_refused(target, frames, publish_only):
    target.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state) "
        "VALUES ('typed',?,'user','hello','{}','t','failed')", (THREAD,),
    )
    target.commit()
    with pytest.raises(HTTPException) as refused:
        _retry("typed")
    assert (refused.value.status_code, refused.value.detail) == (409, "comment_not_retryable")


@pytest.mark.parametrize("spawned_by, mode", [(None, "event"), ("parent", "delegation")])
def test_a_subagent_pane_posts_to_its_child_thread_the_same_way(target, monkeypatch, spawned_by, mode):
    import infra.send_service as send_service

    target.execute("UPDATE threads SET spawned_by=? WHERE id=?", (spawned_by, THREAD))
    target.commit()
    seen = {}

    async def send(thread_id, thread_row, content, **kwargs):
        seen.update(kwargs, thread_id=thread_id, content=content)
        target.execute(
            "INSERT INTO messages(id,thread_id,role,content,metadata,created_at,delivery_state) "
            "VALUES (?,?,'user','',?,'t','queued')", (kwargs["input_id"], thread_id, json.dumps(kwargs["metadata"])),
        )
        target.commit()

    monkeypatch.setattr(send_service, "send_thread_input", send)
    _post()
    assert (seen["thread_id"], seen["content"], seen["native_mode"], seen["human_origin"]) == (THREAD, "", mode, True)
    assert seen.get("broadcast_input", True) is True
