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

from api.email_drafts import SendNow, send_now
from tests.test_email_drafts_api import _artifact, _owner
from tests.test_google_email import (  # noqa: F401 - pytest fixtures
    ACCESS, EMAIL, _draft, _mime, _row, client_file, conductor, google, mail as mail,
)


@pytest.mark.asyncio
async def test_send_now_sends_the_screen_and_notifies_without_body(mail, monkeypatch):
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_to_thread", notice)
    saved = await _draft(mail)
    text = (mail.workspace / saved["path"]).read_text()
    result = await send_now(_artifact(saved), SendNow(text=text), _owner(mail.thread))
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
    body = SendNow(text=(mail.workspace / saved["path"]).read_text())
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
    body = SendNow(text=(mail.workspace / saved["path"]).read_text())
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
    assert await send_now(_artifact(saved), SendNow(text=text), _owner(mail.thread)) == {"state": "sent"}
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
        await send_now(_artifact(saved), SendNow(text=shown), _owner(mail.thread))
    assert caught.value.status_code == 409
    assert caught.value.detail == {"state": "changed", "message": "The draft changed; review it again."}
    assert not mail.sends and _row(saved["draft_id"])["state"] == "open"


@pytest.mark.asyncio
async def test_two_clicks_send_once(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    artifact, owner = _artifact(saved), _owner(mail.thread)
    body = SendNow(text=(mail.workspace / saved["path"]).read_text())
    results = await asyncio.gather(send_now(artifact, body, owner), send_now(artifact, body, owner))
    assert all(result["state"] in ("sent", "sending") for result in results)
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_another_person_cannot_send(mail):
    saved = await _draft(mail)
    with pytest.raises(HTTPException) as caught:
        await send_now(_artifact(saved), SendNow(text=(mail.workspace / saved["path"]).read_text()), {"id": "other"})
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
    assert await send_now(artifact, SendNow(text=(mail.workspace / saved["path"]).read_text()), owner) == {"state": "discarded"}
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
    result = await send_now(_artifact(saved), SendNow(text=(mail.workspace / saved["path"]).read_text()), _owner(mail.thread))
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
        await send_now(artifact, SendNow(text=text), owner)
    assert caught.value.status_code == 409 and caught.value.detail["state"] == "newer"
    assert not mail.sends and _row(saved["draft_id"])["state"] == "open"
    assert await send_now(artifact, SendNow(text=text, read_at=now.isoformat()), owner) == {"state": "sent"}
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_request_account_does_not_change_bound_sender(mail, monkeypatch):
    monkeypatch.setattr("events.delivery.deliver_to_thread", AsyncMock(return_value=True))
    saved = await _draft(mail)
    body = SendNow.model_validate({"text": (mail.workspace / saved["path"]).read_text(), "account_id": "another"})
    assert not hasattr(body, "account_id")
    assert await send_now(_artifact(saved), body, _owner(mail.thread)) == {"state": "sent"}
    assert len(mail.sent_requests()) == 1
    assert mail.sent_requests()[0].headers["authorization"] == f"Bearer {ACCESS}"
