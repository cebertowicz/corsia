"""Drafts, sending and invitations on a mail host: the same reviewed-draft gate as Gmail, over IMAP APPEND and SMTP.

Servers are the local TLS fakes in ``mailhost_fakes``; every send goes through ``email_send`` / ``calendar_send_invite``.
"""

from __future__ import annotations

import json
import logging
from email import message_from_bytes, policy
from types import SimpleNamespace

import pytest
from mailhost_fakes import HOST_REPLY_MARKER
from mailhost_helpers import (  # noqa: F401  (fixtures)
    ADDRESS, PASSWORD, add_account, authority, conductor, env, row, run_check, servers,
)

import infra.config as config
from connections.mailhost import check, read as mailhost_read, store
from events.connectors import mail_poller as poller
from infra.database import get_db
from infra.platform_paths import WorkspaceSelection
from tools import calendar_list as google_calendar
from tools import google_email
from tools.action_gate import Approved
from tools.calendar_list import CalendarDraftInviteTool, CalendarSendInviteTool
from tools.google_email import EmailDraftTool, EmailSendTool

pytestmark = pytest.mark.asyncio


@pytest.fixture
def make_host(conductor, servers, tmp_path, monkeypatch):
    """Build a connected mail-host account on fake servers; ``imap`` / ``caldav`` tune what they answer."""
    from registry import service
    from tools import sandbox_config

    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION",
                        WorkspaceSelection(workspace_id="workspace-stable-id", root=workspace, name="Test workspace"))
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **_kwargs: [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])

    async def approved(kind, draft_id, revision):
        found = get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
        return Approved(dict(found), (workspace / found["path"]).read_bytes())

    monkeypatch.setattr(google_email, "check_action", approved)
    monkeypatch.setattr(google_calendar, "check_action", approved)
    async def build(**tuning) -> SimpleNamespace:
        built = servers(**tuning)
        account_id = add_account(built, conductor)["account_id"]
        assert await run_check(account_id) in store.CONNECTED_STATUSES
        return SimpleNamespace(built=built, account_id=account_id, thread=conductor, workspace=workspace)

    return build


@pytest.fixture
async def host(make_host):
    return await make_host()


def _row(draft_id: str) -> dict:
    return dict(get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())


async def _draft(host, **fields) -> dict:
    args = {"to": ["pat@example.com"], "cc": ["lee@example.com"], "subject": "Lunch", "body": "Noon works?", **fields}
    out = await EmailDraftTool().execute(_thread_id=host.thread, account_id=host.account_id, **args)
    assert not out.startswith("Error"), out
    return json.loads(out)


async def _send(host, saved: dict) -> str:
    return await EmailSendTool().execute(_thread_id=host.thread, draft_id=saved["draft_id"], revision=saved["revision"])


def _no_leak(*texts: str) -> None:
    for text in texts:
        assert PASSWORD not in text and HOST_REPLY_MARKER not in text


async def test_a_draft_is_appended_to_the_hosts_drafts_folder_and_nothing_is_sent(host) -> None:
    saved = await _draft(host)
    [(folder, raw)] = host.built.imap.appended
    parsed = message_from_bytes(raw, policy=policy.default)
    assert folder == "Drafts" and host.built.smtp.sent == []
    assert parsed["From"] == ADDRESS and parsed["To"] == "pat@example.com" and parsed["Subject"] == "Lunch"
    assert _row(saved["draft_id"])["state"] == "open"


async def test_send_submits_over_smtp_to_every_recipient_and_keeps_a_sent_copy(host) -> None:
    saved = await _draft(host)
    out = json.loads(await _send(host, saved))
    [(recipients, data)] = host.built.smtp.sent
    assert recipients == ["pat@example.com", "lee@example.com"]
    sent = message_from_bytes(data, policy=policy.default)
    assert sent["Subject"] == "Lunch" and sent["From"] == ADDRESS and out["account_id"] == host.account_id
    assert out["sent_message_id"] == sent["Message-ID"]
    assert [folder for folder, _ in host.built.imap.appended] == ["Drafts", "Sent"]
    assert message_from_bytes(host.built.imap.appended[1][1], policy=policy.default)["Message-ID"] == sent["Message-ID"]
    assert _row(saved["draft_id"])["state"] == "sent"
    assert (await _send(host, saved)).startswith("Error")
    assert len(host.built.smtp.sent) == 1


async def test_a_host_known_to_save_its_own_sent_copy_gets_no_second_one(host) -> None:
    endpoints, _ = store.read(host.account_id, db=get_db())
    store.write(host.account_id, type(endpoints)(**{**endpoints.__dict__, "smtp_saves_sent": True}), status=store.OK)
    saved = await _draft(host)
    await _send(host, saved)
    assert len(host.built.smtp.sent) == 1
    assert [folder for folder, _ in host.built.imap.appended] == ["Drafts"]


async def test_a_missing_sent_copy_does_not_turn_a_delivered_send_into_a_failure(host, caplog) -> None:
    saved = await _draft(host)
    host.built.imap.password = "changed-after-the-draft"
    with caplog.at_level(logging.WARNING):
        out = await _send(host, saved)
    assert not out.startswith("Error") and len(host.built.smtp.sent) == 1
    assert _row(saved["draft_id"])["state"] == "sent"
    _no_leak(out, caplog.text)


async def test_a_refused_password_sends_nothing_reopens_the_draft_and_turns_the_row_to_refused(host, monkeypatch) -> None:
    notified = []
    monkeypatch.setattr(mailhost_read, "notify_connected_apps_updated", lambda: notified.append(1))
    saved = await _draft(host)
    host.built.smtp.password = "changed-after-the-draft"
    out = await _send(host, saved)
    assert out.startswith("Error: Not sent: ") and "didn’t accept this app password" in out
    assert host.built.smtp.sent == [] and _row(saved["draft_id"])["state"] == "open"
    found = row(host.account_id)
    assert found["check"]["status"] == store.REVOKED and found["state"] == "needs_sign_in" and notified
    _no_leak(out, found["raw"])


async def test_a_send_with_an_unknown_outcome_stays_claimed_and_is_not_repeated(host) -> None:
    saved = await _draft(host)
    host.built.smtp.close()
    out = await _send(host, saved)
    assert out.startswith("Error: The mail host didn't confirm the send")
    assert _row(saved["draft_id"])["state"] == "sending"
    assert (await _send(host, saved)).startswith("Error")


async def test_an_invite_is_sent_as_an_icalendar_request_by_smtp(host) -> None:
    out = await CalendarDraftInviteTool().execute(
        _thread_id=host.thread, account_id=host.account_id, title="Planning", start="2026-10-27T10:00:00-07:00",
        end="2026-10-27T11:00:00-07:00", attendees=["pat@example.com"], description="Quarterly plan",
    )
    assert not out.startswith("Error"), out
    saved = json.loads(out)
    sent = await CalendarSendInviteTool().execute(_thread_id=host.thread, draft_id=saved["draft_id"], revision=saved["revision"])
    assert not sent.startswith("Error"), sent
    [(recipients, data)] = host.built.smtp.sent
    parsed = message_from_bytes(data, policy=policy.default)
    calendar = parsed.get_body(preferencelist=("calendar",))
    assert calendar is not None
    assert recipients == ["pat@example.com"] and calendar.get_content_type() == "text/calendar"
    text = calendar.get_content()
    assert "METHOD:REQUEST" in text and "SUMMARY:Planning" in text and "ATTENDEE:mailto:pat@example.com" in text
    assert _row(saved["draft_id"])["state"] == "sent"


async def test_the_password_is_in_no_draft_send_or_log_text(host, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        saved = await _draft(host)
        out = await _send(host, saved)
    _no_leak(json.dumps(saved), out, caplog.text, row(host.account_id)["raw"])
    for _, raw in host.built.imap.appended:
        assert PASSWORD.encode() not in raw
    for _, data in host.built.smtp.sent:
        assert PASSWORD.encode() not in data


async def test_a_password_the_poller_finds_refused_flips_the_row_and_polling_for_it_stops(host, monkeypatch) -> None:
    for name in ("_accounts", "_inflight", "_due"):
        monkeypatch.setattr(poller, name, {})
    monkeypatch.setattr(poller, "_status", poller._fresh_status())
    monkeypatch.setenv(poller.TEST_ENABLE_ENV, "1")
    notified = []
    monkeypatch.setattr(mailhost_read, "notify_connected_apps_updated", lambda: notified.append(1))
    await poller.run_cycle()
    assert row(host.account_id)["state"] == "connected"
    host.built.imap.password = "revoked-by-the-host"
    poller._due[host.account_id] = 0
    await poller.run_cycle()
    found = row(host.account_id)
    assert found["check"]["status"] == store.REVOKED and found["state"] == "needs_sign_in" and notified
    logins = [verb for _, verb in host.built.imap.commands].count("LOGIN")
    await poller.run_cycle()
    assert [verb for _, verb in host.built.imap.commands].count("LOGIN") == logins
    assert host.account_id not in poller._accounts
