"""Send now through the person-facing route with a fake Gmail provider."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from infra.database import get_db

import pytest
from fastapi import HTTPException

from api.email_drafts import dont_send
from doceditor.document_scope import revision as document_revision

from api.email_drafts import SendNow, draft_context, send_now
from tests.test_email_drafts_api import _artifact, _owner, _shown
from tests.test_google_email import (  # noqa: F401 - pytest fixtures
    ACCESS, EMAIL, _draft, _mime, _row, client_file, conductor, google, mail as mail,
)


@pytest.mark.asyncio
async def test_send_now_sends_the_screen_and_notifies_without_body(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    text = (mail.workspace / saved["path"]).read_text()
    result = await send_now(_artifact(saved), _shown(saved, text), _owner(mail.thread))
    assert result == {"state": "sent"}
    assert _row(saved["draft_id"])["state"] == "sent"
    assert len(mail.sends) == 1
    sent = _mime(mail.sends[0]).get_body(preferencelist=("plain",)).get_content()
    assert sent.replace("\r\n", "\n").rstrip("\n") == text.partition("\n\n")[2].rstrip("\n")
    notice.assert_awaited_once()
    assert notice.await_args.kwargs["delivery_id"] == f"draft-send:{saved['draft_id']}:sent:{saved['revision']}"
    assert "Noon works?" not in str(notice.await_args)


@pytest.mark.asyncio
async def test_failed_delivery_can_be_retried(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    body = _shown(saved, (mail.workspace / saved["path"]).read_text())
    mail.send_mode = "400"
    result = await send_now(artifact, body, owner)
    assert result["state"] == "failed" and result["reason"]
    assert _row(saved["draft_id"])["state"] == "open"
    assert EMAIL in str(notice.await_args)
    mail.send_mode = "ok"
    assert await send_now(artifact, body, owner) == {"state": "sent"}
    assert len(mail.sends) == 2


@pytest.mark.asyncio
async def test_unconfirmed_is_not_sent_again(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    body = _shown(saved, (mail.workspace / saved["path"]).read_text())
    mail.send_mode = "timeout"
    assert await send_now(artifact, body, owner) == {"state": "unconfirmed"}
    assert _row(saved["draft_id"])["state"] == "sending"
    assert await send_now(artifact, body, owner) == {"state": "sending"}
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_person_edit_is_recorded_and_sent(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    path.write_text(path.read_text().replace("Noon works?", "One works."))
    text = path.read_text()
    assert await send_now(_artifact(saved), _shown(saved, text), _owner(mail.thread)) == {"state": "sent"}
    assert _row(saved["draft_id"])["recorded_revision"] == document_revision(text.encode())
    assert "One works." in _mime(mail.sends[0]).get_body(preferencelist=("plain",)).get_content()


@pytest.mark.asyncio
async def test_changed_since_screen_is_rejected(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    shown = path.read_text()
    path.write_text(shown.replace("Noon works?", "A changed draft."))
    with pytest.raises(HTTPException) as caught:
        await send_now(_artifact(saved), _shown(saved, shown), _owner(mail.thread))
    assert caught.value.status_code == 409
    assert caught.value.detail == {"state": "changed", "message": "The draft changed; review it again."}
    assert not mail.sends and _row(saved["draft_id"])["state"] == "open"


@pytest.mark.asyncio
async def test_two_clicks_send_once(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    body = _shown(saved, (mail.workspace / saved["path"]).read_text())
    results = await asyncio.gather(send_now(artifact, body, owner), send_now(artifact, body, owner))
    assert all(result["state"] in ("sent", "sending") for result in results)
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_an_agent_save_during_a_send_waits_and_the_file_stays_what_was_sent(mail, monkeypatch):
    from tools.google_email import EmailDraftTool
    import api.email_drafts as route

    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    path = mail.workspace / saved["path"]
    shown = path.read_text()
    sending, release, real = asyncio.Event(), asyncio.Event(), route.send_approved

    async def held(*args, **kwargs):
        sending.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(route, "send_approved", held)
    send = asyncio.create_task(send_now(_artifact(saved), _shown(saved, shown), _owner(mail.thread)))
    await sending.wait()
    save = asyncio.create_task(EmailDraftTool().execute(
        _thread_id=mail.thread, account_id=mail.account, draft_id=saved["draft_id"],
        to=["sam@example.com"], subject="Lunch", body="A late change."))
    await asyncio.sleep(0.05)
    # The save is held at the lock while the send is under way; it has not touched the file.
    assert not save.done() and path.read_text() == shown
    release.set()
    assert await send == {"state": "sent"}
    refused = await save
    assert refused.startswith("Error") and "A late change." not in path.read_text()
    assert path.read_text() == shown and len(mail.sends) == 1
    assert "Noon works?" in _mime(mail.sends[0]).get_body(preferencelist=("plain",)).get_content()


@pytest.mark.asyncio
async def test_another_person_cannot_send(mail):
    saved = await _draft(mail)
    with pytest.raises(HTTPException) as caught:
        await send_now(_artifact(saved), _shown(saved, (mail.workspace / saved["path"]).read_text()), {"id": "other"})
    assert caught.value.status_code == 404 and not mail.sends


@pytest.mark.asyncio
async def test_discard_notifies_and_prevents_send(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    assert await dont_send(artifact, owner) == {"state": "discarded"}
    assert _row(saved["draft_id"])["state"] == "discarded"
    assert notice.await_args.args[1] == "email_draft.discarded"
    assert await send_now(artifact, _shown(saved, (mail.workspace / saved["path"]).read_text()), owner) == {"state": "discarded"}
    assert not mail.sends


@pytest.mark.asyncio
async def test_removed_account_fails_without_claim_and_notifies(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    target = json.loads(_row(saved["draft_id"])["target_json"])
    target["account_id"] = "removed-account"
    db = get_db()
    db.execute("UPDATE action_drafts SET target_json=? WHERE id=?", (json.dumps(target), saved["draft_id"]))
    db.commit()
    result = await send_now(_artifact(saved), _shown(saved, (mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
    assert result["state"] == "failed" and result["reason"]
    assert _row(saved["draft_id"])["state"] == "open" and not mail.sends
    notice.assert_awaited_once()


@pytest.mark.asyncio
async def test_newer_reply_requires_review_then_read_at_allows_send(mail, monkeypatch):
    from tests.test_guarded_mail_journeys import _arrival, _backdate, newer, provider_state
    from tests.test_google_email import THREADED

    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail, to=[], reply_to_message_id="m-100")
    now = datetime.now(timezone.utc)
    _backdate(saved, now - timedelta(seconds=10))
    provider_state(mail, [THREADED, _arrival(newer(), now - timedelta(seconds=5))])
    artifact, owner = _artifact(saved), _owner(mail.thread)
    text = (mail.workspace / saved["path"]).read_text()
    with pytest.raises(HTTPException) as caught:
        await send_now(artifact, _shown(saved, text), owner)
    assert caught.value.status_code == 409 and caught.value.detail["state"] == "newer"
    assert not mail.sends and _row(saved["draft_id"])["state"] == "open"
    assert await send_now(artifact, _shown(saved, text, read_at=now.isoformat()), owner) == {"state": "sent"}
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_request_account_does_not_change_bound_sender(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    shown = _shown(saved, (mail.workspace / saved["path"]).read_text())
    body = SendNow.model_validate({**shown.model_dump(), "account_id": "another"})
    assert not hasattr(body, "account_id")
    assert await send_now(_artifact(saved), body, _owner(mail.thread)) == {"state": "sent"}
    assert len(mail.sent_requests()) == 1
    assert mail.sent_requests()[0].headers["authorization"] == f"Bearer {ACCESS}"


def _review_ask(thread_id: str, artifact_id: str) -> str:
    """The draft's agent asks the person to review the draft (block_node with the draft as its ask)."""
    import uuid

    from plans import service
    from plans.boundary import PlanActor, mutate

    db = get_db()
    try:
        plan = service.ensure_plan("thread", thread_id, db=db)
        node = service.add_node(plan.id, kind="task", title="Send the reply?", acceptance_criteria="Answered", db=db)
        db.commit()
        token = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()["user_id"]
        mutate(plan.id, "block_node", {
            "node_id": node.id, "reason_key": "review", "reason": "Read the reply before it goes",
            "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node.id)["revision"],
        }, actor=PlanActor(thread_id, token, None), provenance="test.send_now", mutation_id=uuid.uuid4().hex, db=db)
        db.commit()
        return node.id
    finally:
        db.close()


def _open_asks(artifact_id: str) -> list:
    from plans.public import raised_asks
    from registry import artifact_record

    db = get_db()
    try:
        return raised_asks(db, str(artifact_record.identity_row_for(artifact_id, db=db)["id"]))
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "discard"])
async def test_send_or_dont_send_answers_the_review_request(mail, monkeypatch, action):
    """E7: the person's review item for the draft goes with Send now or Don't send, in the same request."""
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    monkeypatch.setattr("plans.public.drive_threads", AsyncMock())
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    await asyncio.to_thread(_review_ask, mail.thread, artifact)
    assert len(_open_asks(artifact)) == 1
    if action == "send":
        assert await send_now(artifact, _shown(saved, (mail.workspace / saved["path"]).read_text()), owner) == {"state": "sent"}
    else:
        assert await dont_send(artifact, owner) == {"state": "discarded"}
    assert _open_asks(artifact) == []
    assert "Your review request on it is answered." in notice.await_args.args[2]


@pytest.mark.asyncio
async def test_failed_send_leaves_the_review_request_open(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    artifact = _artifact(saved)
    await asyncio.to_thread(_review_ask, mail.thread, artifact)
    mail.send_mode = "400"
    result = await send_now(artifact, _shown(saved, (mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
    assert result["state"] == "failed"
    assert len(_open_asks(artifact)) == 1


@pytest.mark.asyncio
async def test_no_review_request_says_nothing_about_one(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    await send_now(_artifact(saved), _shown(saved, (mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
    assert "review request" not in notice.await_args.args[2]


@pytest.mark.asyncio
async def test_a_failed_review_answer_tells_the_agent_to_settle_it(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)

    def broken(*args, **kwargs):
        raise RuntimeError("attention store down")

    monkeypatch.setattr("registry.attention.clear_attention", broken)
    saved = await _draft(mail)
    result = await send_now(_artifact(saved), _shown(saved, (mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
    assert result == {"state": "sent"}
    assert "couldn't be marked answered; settle it yourself" in notice.await_args.args[2]


@pytest.mark.asyncio
async def test_agent_save_of_only_the_target_refuses_the_send_it_was_not_shown(mail, monkeypatch):
    """An agent save that changes only the attachments leaves the text alone; the send still refuses it."""
    from tools.action_drafts import record_draft
    from tools.reviewed_drafts import draft_target

    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    text = (mail.workspace / saved["path"]).read_text()
    read = await draft_context(artifact, owner)
    shown = _shown(saved, text)
    assert read["target_version"] == shown.target_version
    target = {**draft_target(_row(saved["draft_id"])), "attachments": [{"name": "unseen.pdf", "path": "unseen.pdf", "size": 3}]}
    record_draft(saved["draft_id"], text.encode("utf-8"), target)
    with pytest.raises(HTTPException) as refused:
        await send_now(artifact, shown, owner)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"state": "changed", "message": "The draft changed; review it again."}
    assert mail.sends == [] and _row(saved["draft_id"])["state"] == "open"
    assert (await draft_context(artifact, owner))["target_version"] != shown.target_version


@pytest.mark.asyncio
async def test_send_without_the_shown_target_is_refused(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    with pytest.raises(HTTPException) as refused:
        await send_now(_artifact(saved), SendNow(text=(mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
    assert refused.value.status_code == 409 and mail.sends == []
