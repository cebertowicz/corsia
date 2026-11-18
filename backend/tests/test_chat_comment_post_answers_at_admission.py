"""POST /api/threads/{id}/chat-comments answers once the comment is stored, not once its delivery has started."""

import asyncio

import pytest
from fastapi import HTTPException

from auth_fixtures import OWNER_USER
from test_chat_comment_not_in_chat import THREAD, _comment_metadata, frames, notices  # noqa: F401
from test_chat_comment_post_route import target  # noqa: F401

USER = {"id": OWNER_USER}


class Delivery:
    """Stands in for the provider start: starts when the batch is claimed, ends when the test says."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = None

    async def execute(self, _batch, **_kwargs):
        self.started.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        return None


@pytest.fixture
def delivery(monkeypatch):
    """Admit and publish for real; the claimed batch's execution is the one gated step."""
    import infra.send_service as send_service

    gate = {}

    async def resolved(_admission, _target_for):
        return object()

    async def no_images(*_a):
        return []

    async def execute(batch, **kwargs):
        return await gate["delivery"].execute(batch, **kwargs)

    monkeypatch.setattr(send_service, "resolve_claimed_target", resolved)
    monkeypatch.setattr(send_service, "execute_claimed_batch", execute)
    monkeypatch.setattr(send_service, "_bind_image_identity", no_images)
    monkeypatch.setattr(send_service, "_boot_epoch", lambda: "test")
    monkeypatch.setattr("jobs.corrections_recording.notify_if_owner", lambda *_a, **_k: None)
    return gate


def _comment(**changes):
    return {**_comment_metadata()["reply_context"]["comments"][0], **changes}


async def _post(comment=None, thread_id=THREAD):
    from api.chat_comment_views import PostRequest, post_chat_comment

    return await asyncio.wait_for(
        post_chat_comment(thread_id, PostRequest(input_id="box-1", comment=comment or _comment()), user=USER),
        timeout=10,
    )


async def _settled():
    from ws.ingress import background_tasks

    await asyncio.wait_for(asyncio.gather(*list(background_tasks)), timeout=10)


def test_the_post_answers_while_delivery_is_still_starting(target, frames, delivery):
    from infra import drain

    async def scenario():
        delivery["delivery"] = gated = Delivery()
        result = await _post()
        # Stored, announced once, and answered while the gated start has not finished
        # (awaiting it would hang here until the post's timeout).
        assert not gated.release.is_set()
        assert (result["message_id"], result["delivery_state"]) == ("box-1", "queued")
        assert result["messages"][0]["chat_comments"][0]["note"]
        assert len(notices(frames)) == 1
        await asyncio.wait_for(gated.started.wait(), timeout=10)
        # Delivery is still in flight, so a restart drain still waits for it.
        assert drain._turn_admissions == 1
        gated.release.set()
        await _settled()
        assert drain._turn_admissions == 0

    asyncio.run(scenario())


def test_a_failure_after_the_comment_is_stored_does_not_fail_the_post(target, frames, delivery):
    from infra.send_types import SendRejected

    async def scenario():
        delivery["delivery"] = gated = Delivery()
        gated.fail = SendRejected(409, "provider refused", input_undeliverable=True)
        gated.release.set()
        result = await _post()
        assert result["message_id"] == "box-1"
        await _settled()

    asyncio.run(scenario())


@pytest.mark.parametrize("comment, thread_id, status, detail", [
    ({"seq": 99}, THREAD, 400, "comment_quote_gone"),
    ({}, "no-such-thread", 404, "chat_not_found"),
], ids=["quote_gone", "no_thread"])
def test_a_refusal_before_the_row_is_stored_still_answers_with_its_code(
    target, frames, delivery, comment, thread_id, status, detail,
):
    async def scenario():
        delivery["delivery"] = gated = Delivery()
        with pytest.raises(HTTPException) as refused:
            await _post(_comment(**comment), thread_id)
        assert (refused.value.status_code, refused.value.detail) == (status, detail)
        assert not gated.started.is_set()
        await _settled()

    asyncio.run(scenario())
    assert target.execute("SELECT 1 FROM messages WHERE id='box-1'").fetchone() is None


def test_a_refusal_before_the_row_is_stored_logs_no_background_failure(target, frames, delivery):
    from loguru import logger

    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(message.record["message"]), level="ERROR")

    async def scenario():
        delivery["delivery"] = Delivery()
        with pytest.raises(HTTPException):
            await _post(_comment(seq=99))
        await _settled()

    try:
        asyncio.run(scenario())
    finally:
        logger.remove(sink)
    # The person is told; the task observer must not also log it as failed work.
    assert errors == []


def test_a_comment_check_that_fails_still_answers_with_its_reference(target, frames, delivery, monkeypatch):
    import infra.reply_context as reply_context
    from infra.failure_reference import ReferencedHTTPException

    def broken(*_args, **_kwargs):
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(reply_context, "_comments", broken)

    async def scenario():
        delivery["delivery"] = Delivery()
        with pytest.raises(ReferencedHTTPException) as refused:
            await _post()
        assert (refused.value.status_code, refused.value.detail) == (500, "comment_check_failed")
        await _settled()

    asyncio.run(scenario())
