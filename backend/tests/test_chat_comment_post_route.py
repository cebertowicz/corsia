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


def _referenced(error) -> bool:
    """A failure the person can only retry carries the reference it is logged under, so the box can show it."""
    from infra.failure_reference import REFERENCE

    return bool(REFERENCE.match(getattr(error, "reference", "") or ""))


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


def _base(**changes):
    return {**_comment_metadata()["reply_context"]["comments"][0], **changes}


@pytest.mark.parametrize("changes", [{"seq": 99}, {"from_off": 3, "to_off": 3}], ids=["seq_gone", "offsets"])
def test_a_comment_whose_quote_is_gone_is_refused_by_that_cause(target, frames, publish_only, changes):
    # Its own code, never chat composer's sentence about numbered comments and a message, nor the generic
    # "try again": no press can store it, so the box offers no Retry.
    with pytest.raises(HTTPException) as refused:
        _post(comment=_base(**changes))
    assert (refused.value.status_code, refused.value.detail) == (400, "comment_quote_gone")
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
        _post(comment=_base(seq=2))
    assert (refused.value.status_code, refused.value.detail) == (400, "comment_quote_gone")
    assert target.execute("SELECT 1 FROM messages WHERE id='box-1'").fetchone() is None
    assert target.execute("SELECT 1 FROM chat_visible_content WHERE message_id='box-1'").fetchone() is None


def test_a_malformed_comment_gets_the_generic_refusal(target, frames, publish_only):
    with pytest.raises(HTTPException) as refused:
        _post(comment=_base(id=""))
    assert (refused.value.status_code, refused.value.detail) == (400, "comment_not_sent")
    # A client bug is a system failure: it gets a reference too.
    assert _referenced(refused.value)


def test_a_comment_admission_could_not_check_carries_its_reference(target, frames, publish_only, monkeypatch):
    import infra.reply_context as reply_context
    from infra.failure_reference import ReferencedHTTPException

    def broken(*_args, **_kwargs):
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(reply_context, "_comments", broken)
    with pytest.raises(ReferencedHTTPException) as refused:
        _post()
    # A system failure: the person gets the reference it was logged under, and Retry.
    assert (refused.value.status_code, refused.value.detail) == (500, "comment_check_failed")
    assert _referenced(refused.value)
    assert target.execute("SELECT 1 FROM messages WHERE id='box-1'").fetchone() is None


def test_a_model_cap_refusal_gets_its_own_code(target, frames, publish_only, monkeypatch):
    import infra.send_service as send_service
    from infra.input_admission import CHECKPOINT_CONFIGURATION_IMPOSSIBLE
    from infra.send_types import SendRejected

    async def refuse(*_args, **_kwargs):
        raise SendRejected(409, "The context cap for m is too small for a checkpoint.",
                           metadata={"cause": CHECKPOINT_CONFIGURATION_IMPOSSIBLE})

    monkeypatch.setattr(send_service, "send_thread_input", refuse)
    with pytest.raises(HTTPException) as refused:
        _post()
    assert (refused.value.status_code, refused.value.detail) == (409, "comment_model_cap")


def test_a_chat_deleted_while_posting_is_not_found(target, frames, publish_only, monkeypatch):
    import infra.send_service as send_service
    from infra.send_types import SendRejected

    async def refuse(*_args, **_kwargs):
        raise SendRejected(404, "thread does not exist")

    monkeypatch.setattr(send_service, "send_thread_input", refuse)
    with pytest.raises(HTTPException) as refused:
        _post()
    assert (refused.value.status_code, refused.value.detail) == (404, "chat_not_found")


def test_an_admitted_comment_that_cannot_be_read_back_carries_a_reference(target, frames, publish_only, monkeypatch):
    import api.chat_comment_views as views
    from infra.failure_reference import ReferencedHTTPException

    monkeypatch.setattr(views, "_posted", lambda *_args: None)
    with pytest.raises(ReferencedHTTPException) as failed:
        _post()
    assert (failed.value.status_code, failed.value.detail) == (500, "comment_not_stored")
    assert _referenced(failed.value)


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
    assert (refused.value.status_code, refused.value.detail) == (502, "comment_not_sent")
    assert _referenced(refused.value)


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
    assert _referenced(refused.value)


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


@pytest.mark.parametrize("rejection, expected", [
    ((409, "Thread can't be continued as a Claude Code session.", {}), (409, "comment_not_sent")),
    ((400, "Comment 1 couldn’t be matched … remove that comment and send again.", {"cause": "comment_quote_gone"}),
     (400, "comment_quote_gone")),
    ((404, "thread does not exist", {}), (404, "chat_not_found")),
], ids=["generic", "quote_gone", "chat_deleted"])
def test_a_retry_the_chat_refuses_says_why_rather_than_standing(target, frames, publish_only, monkeypatch, rejection, expected):
    # The comment is still failed, so a refusal is the Retry's own failure, never a silent 200. It is told
    # by the post's codes, never by chat's prose for a message, which reads wrong on a comment thread.
    import infra.send_service as send_service
    from infra.send_types import SendRejected

    _post()
    _end("failed")
    status, detail, metadata = rejection

    async def refuse(*_args, **_kwargs):
        raise SendRejected(status, detail, metadata=metadata)

    monkeypatch.setattr(send_service, "send_thread_input", refuse)
    with pytest.raises(HTTPException) as refused:
        _retry()
    assert (refused.value.status_code, refused.value.detail) == expected
    # Only the generic failure, which the person can just retry, carries a reference.
    assert _referenced(refused.value) is (expected[1] == "comment_not_sent")


def test_a_retry_chat_finds_nothing_to_retry_for_gets_the_generic_refusal(target, frames, publish_only, monkeypatch):
    import api.thread_inbox_actions as inbox

    _post()
    _end("failed")

    async def missing(*_args, **_kwargs):
        raise HTTPException(404, "Task or message not found")

    monkeypatch.setattr(inbox, "retry_thread_delivery_now", missing)
    with pytest.raises(HTTPException) as refused:
        _retry()
    assert (refused.value.status_code, refused.value.detail) == (404, "comment_not_sent")
    assert _referenced(refused.value)


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
    assert _referenced(refused.value)


def test_the_api_answers_a_referenced_refusal_with_its_reference(isolated_db, monkeypatch):
    # The box reads the reference from the body, whatever the status; an unreferenced 4xx stays as it was.
    import main
    from infra.failure_reference import ReferencedHTTPException
    from starlette.requests import Request

    monkeypatch.setattr(main, "emit_product_event", lambda *args, **kwargs: None)
    request = Request({"type": "http", "method": "POST", "path": "/api/threads/t/chat-comments",
                       "headers": [], "query_string": b""})
    referenced = asyncio.run(main._emit_http_error_api(request, ReferencedHTTPException(400, "comment_not_sent", "ABC123")))
    plain = asyncio.run(main._emit_http_error_api(request, HTTPException(400, "comment_quote_gone")))
    assert (referenced.status_code, json.loads(referenced.body)) == (400, {"detail": "comment_not_sent", "reference": "ABC123"})
    assert (plain.status_code, json.loads(plain.body)) == (400, {"detail": "comment_quote_gone"})


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
