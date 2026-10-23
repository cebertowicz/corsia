"""Mail-host replies, Drafts/Sent folder discovery, and the organizer's copy of an invite.

Same fixtures as ``test_mailhost_send``: local TLS fakes, the real draft and send tools, the approved-draft gate stubbed.
"""

from __future__ import annotations

import json
import logging
from email import message_from_bytes, policy

import pytest
from mailhost_helpers import authority, conductor, env, servers  # noqa: F401  (fixtures)
from test_mailhost_send import _draft, _send, host, make_host  # noqa: F401  (fixtures and helpers)

from connections.mail.email_document import reply_subject
from connections.mailhost import read
from tools.google_calendar import CalendarDraftInviteTool, CalendarSendInviteTool
from tools.google_email import EmailDraftTool


ORIGINAL = (
    b"From: Sam <sam@example.com>\r\nReply-To: Team <team@example.com>\r\nTo: you@example.com\r\n"
    b"Date: Wed, 21 Oct 2026 09:00:00 -0700\r\nSubject: Lunch plans\r\nMessage-ID: <orig@example.com>\r\n"
    b"References: <root@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nNoon on Friday?\r\n"
)
PLAIN_FROM = ORIGINAL.replace(b"Reply-To: Team <team@example.com>\r\n", b"")


def _id(host, uid: int = 5, validity: int = 7) -> str:
    return read.mint_message_id(host.account_id, validity, uid)


def _parsed(raw: bytes):
    return message_from_bytes(raw, policy=policy.default)


async def _reply_host(make_host, original: bytes = ORIGINAL):
    host = await make_host()
    host.built.imap.messages[5] = original
    return host


def test_a_reply_subject_gets_re_once() -> None:
    assert reply_subject("Lunch") == "Re: Lunch"
    assert reply_subject("re: Lunch") == "re: Lunch" and reply_subject("  RE : Lunch") == "  RE : Lunch"


@pytest.mark.asyncio
async def test_a_reply_threads_under_the_message_defaults_to_reply_to_and_prefixes_the_subject(make_host) -> None:
    host = await _reply_host(make_host)
    saved = await _draft(host, to=[], cc=[], subject="Lunch plans", reply_to_message_id=_id(host))
    document = (host.workspace / saved["path"]).read_text()
    assert "To: team@example.com" in document and "Subject: Re: Lunch plans" in document
    assert not (await _send(host, saved)).startswith("Error")
    [(recipients, data)] = host.built.smtp.sent
    assert recipients == ["team@example.com"]
    copies = [_parsed(raw) for folder, raw in host.built.imap.appended if folder == "Sent"]
    for message in (_parsed(data), *copies):
        assert message["In-Reply-To"] == "<orig@example.com>"
        assert message["References"] == "<root@example.com> <orig@example.com>"
        assert message["Subject"] == "Re: Lunch plans"
    assert len(copies) == 1
    [(_, draft_copy)] = [item for item in host.built.imap.appended if item[0] == "Drafts"]
    assert _parsed(draft_copy)["In-Reply-To"] == "<orig@example.com>"


@pytest.mark.asyncio
async def test_a_reply_without_reply_to_goes_to_the_sender_and_an_explicit_recipient_wins(make_host) -> None:
    host = await _reply_host(make_host, PLAIN_FROM)
    saved = await _draft(host, to=[], cc=[], subject="Re: Lunch plans", reply_to_message_id=_id(host))
    assert "To: sam@example.com" in (host.workspace / saved["path"]).read_text()
    explicit = await _draft(host, to=["pat@example.com"], cc=[], subject="Lunch plans", reply_to_message_id=_id(host))
    assert "To: pat@example.com" in (host.workspace / explicit["path"]).read_text()


@pytest.mark.parametrize("kind", ["rebuilt", "gone", "gmail", "injection", "other_account"])
@pytest.mark.asyncio
async def test_a_message_that_is_not_in_this_accounts_inbox_is_refused_and_nothing_is_saved(make_host, kind) -> None:
    host = await _reply_host(make_host)
    message_id = {
        "rebuilt": _id(host, validity=99), "gone": _id(host, uid=77), "gmail": "9f3a1c",
        "injection": _id(host) + "; DELETE", "other_account": read.mint_message_id("another-account", 7, 5),
    }[kind]
    out = await EmailDraftTool().execute(
        _thread_id=host.thread, account_id=host.account_id, subject="Re: x", body="Yes", reply_to_message_id=message_id,
    )
    assert out == "Error: That message can't be found."
    assert host.built.imap.appended == []


@pytest.mark.asyncio
async def test_a_reply_with_no_recipient_anywhere_is_refused(make_host) -> None:
    host = await _reply_host(make_host, PLAIN_FROM.replace(b"From: Sam <sam@example.com>\r\n", b""))
    out = await EmailDraftTool().execute(
        _thread_id=host.thread, account_id=host.account_id, subject="Re: x", body="Yes", reply_to_message_id=_id(host),
    )
    assert out == "Error: A draft needs at least one recipient."


@pytest.mark.asyncio
async def test_a_host_that_advertises_its_sent_folder_gets_the_copy_there(make_host) -> None:
    host = await make_host(imap={"folders": {"INBOX": "", "Drafts": "\\Drafts", "Sent Messages": "\\Sent"}})
    saved = await _draft(host)
    assert not (await _send(host, saved)).startswith("Error")
    assert [folder for folder, _ in host.built.imap.appended] == ["Drafts", "Sent Messages"]


@pytest.mark.asyncio
async def test_a_host_with_no_special_use_flags_is_found_by_its_usual_names(make_host) -> None:
    host = await make_host(imap={"folders": {"INBOX": "", "Draft": "", "Sent Items": ""}})
    saved = await _draft(host)
    await _send(host, saved)
    assert [folder for folder, _ in host.built.imap.appended] == ["Draft", "Sent Items"]


@pytest.mark.asyncio
async def test_a_host_that_shows_no_such_folder_still_sends_and_still_saves_the_draft(make_host, caplog) -> None:
    host = await make_host(imap={"folders": {"INBOX": ""}})
    with caplog.at_level(logging.WARNING):
        saved = await _draft(host)
        out = await _send(host, saved)
    assert not out.startswith("Error") and len(host.built.smtp.sent) == 1 and host.built.imap.appended == []
    assert "shows no sent folder" in caplog.text and "shows no drafts folder" in caplog.text


async def _invite(host, **tuning) -> dict:
    drafted = json.loads(await CalendarDraftInviteTool().execute(
        _thread_id=host.thread, account_id=host.account_id, title="Planning", start="2026-10-27T10:00:00-07:00",
        end="2026-10-27T11:00:00-07:00", attendees=["pat@example.com", "lee@example.com"], description="Quarterly plan",
    ))
    sent = await CalendarSendInviteTool().execute(_thread_id=host.thread, draft_id=drafted["draft_id"], revision=drafted["revision"])
    assert not sent.startswith("Error"), sent
    return json.loads(sent)


def _unfolded(ics: str) -> str:
    return ics.replace("\r\n ", "").replace("\r\n", "\n")


@pytest.mark.asyncio
async def test_an_invite_is_also_stored_on_the_organizers_calendar_with_the_same_uid(host) -> None:
    result = await _invite(host)
    [(path, stored)] = host.built.caldav.puts
    stored = _unfolded(stored)
    uid = result["event_id"]
    assert path.startswith("/calendars/home/work/") and path.endswith(f"{uid}.ics")
    assert f"UID:{uid}" in stored and "METHOD" not in stored and "SUMMARY:Planning" in stored
    attendees = [line for line in stored.split("\n") if line.startswith("ATTENDEE")]
    assert len(attendees) == 2 and all("SCHEDULE-AGENT=CLIENT" in line for line in attendees)
    [(recipients, data)] = host.built.smtp.sent
    mailed = _unfolded(_parsed(data).get_body(preferencelist=("calendar",)).get_content())
    assert recipients == ["pat@example.com", "lee@example.com"] and f"UID:{uid}" in mailed and "METHOD:REQUEST" in mailed
    assert "note" not in result


@pytest.mark.asyncio
async def test_an_invitation_the_host_will_not_send_puts_nothing_on_the_calendar(make_host) -> None:
    host = await make_host()
    host.built.smtp.password = "not-the-saved-password"
    drafted = json.loads(await CalendarDraftInviteTool().execute(
        _thread_id=host.thread, account_id=host.account_id, title="Planning", start="2026-10-27T10:00:00-07:00",
        end="2026-10-27T11:00:00-07:00", attendees=["pat@example.com"],
    ))
    sent = await CalendarSendInviteTool().execute(_thread_id=host.thread, draft_id=drafted["draft_id"], revision=drafted["revision"])
    assert sent.startswith("Error") and host.built.smtp.sent == [] and host.built.caldav.puts == []


@pytest.mark.asyncio
async def test_a_calendar_that_refuses_the_event_does_not_stop_the_invitation_and_the_result_says_so(make_host) -> None:
    host = await make_host()
    host.built.caldav.put_status = 403
    result = await _invite(host)
    assert result["note"] == "The invitation was emailed, but the event isn't on the person's calendar."
    assert len(host.built.smtp.sent) == 1


@pytest.mark.asyncio
async def test_a_mail_only_account_sends_the_invitation_and_says_the_calendar_was_not_updated(make_host) -> None:
    host = await make_host(with_caldav=False)
    result = await _invite(host)
    assert "calendar" in result["note"] and len(host.built.smtp.sent) == 1
