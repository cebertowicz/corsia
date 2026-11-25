"""Agents send a saved email draft themselves: no person turn is needed; the draft's own checks still hold.

email_send runs through the real ``check_action``; microsoft_mail_send's case is in test_microsoft_delivery.
"""
from __future__ import annotations

import json

import pytest

from infra import state
from tests.test_google_email import _draft, _mime, _row, mail  # noqa: F401  (fixture)
from tests.test_google_sign_in import EMAIL, client_file, conductor, google  # noqa: F401  (fixtures)
from tools import action_gate, google_email
from tools.google_email import EmailSendTool


def _no_turn(thread: str) -> None:
    """A call with no person in the turn: a scheduled run, an event wake or another agent's ask."""
    state._current_thread_id.set(thread)
    state._current_turn_identity.set(None)
    state._current_provider_call_id.set(None)
    state._current_tool_invocation_id.set(None)


async def _send(mail, saved: dict, revision: str | None = None) -> str:  # noqa: F811
    return await EmailSendTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=revision or saved["revision"],
    )


@pytest.mark.asyncio
async def test_an_agent_sends_a_saved_draft_with_no_person_turn(mail):  # noqa: F811
    saved = await _draft(mail)
    _no_turn(mail.thread)
    out = json.loads(await _send(mail, saved))
    assert out["sent_message_id"] == "sent-1" and out["account_id"] == mail.account
    message = _mime(mail.sends[0])
    assert message["To"] == "sam@example.com" and message["Subject"] == "Lunch"
    assert _row(saved["draft_id"])["state"] == "sent"
    assert await _send(mail, saved) == f"Error: {action_gate.ALREADY_DONE}"
    assert len(mail.sends) == 1


@pytest.mark.asyncio
async def test_a_stale_revision_or_an_outside_edit_still_sends_nothing(mail):  # noqa: F811
    saved = await _draft(mail)
    _no_turn(mail.thread)
    assert await _send(mail, saved, revision="0" * 64) == f"Error: {action_gate.STALE_REVISION}"
    path = mail.workspace / saved["path"]
    path.write_text(path.read_text().replace("Noon works?", "Send money."))
    assert await _send(mail, saved) == f"Error: {action_gate.CHANGED_OUTSIDE}"
    assert mail.sends == [] and _row(saved["draft_id"])["state"] == "open"


@pytest.mark.asyncio
async def test_a_from_line_that_is_not_the_drafts_account_still_sends_nothing(mail, monkeypatch):  # noqa: F811
    saved = await _draft(mail)
    _no_turn(mail.thread)
    approved = action_gate.Approved(
        _row(saved["draft_id"]),
        (mail.workspace / saved["path"]).read_bytes().replace(EMAIL.encode(), b"other@example.com"),
    )

    async def gate(kind, draft_id, revision, **_):
        return approved

    monkeypatch.setattr(google_email, "check_action", gate)
    assert await _send(mail, saved) == f"Error: {google_email.FROM_MISMATCH.format(account=EMAIL)}"
    assert mail.sends == []


def test_the_send_tools_are_registered_for_agents():
    from tools.catalog import TOOL_CATALOG, pack_members

    names = {name for pack in ("email", "microsoft_mail") for name in pack_members()[pack]}
    assert {"email_send", "microsoft_mail_send"} <= names
    assert {"email_send", "microsoft_mail_send"} <= set(TOOL_CATALOG)
    assert "latest message" not in EmailSendTool().description
