"""Calendar tool contract, list semantics, and invite-specific recovery."""

from __future__ import annotations

import json
import os

import httpx
import pytest

import infra.config as config
from delegation_fixtures import deleg_db  # noqa: F401 (fixture)
from infra.database import get_db
from tests.test_action_gate import _admit, _bind, _draft, workspace  # noqa: F401 (fixture)
from tests.test_google_email import mail  # noqa: F401 (fixture)
from tests.test_google_sign_in import client_file, conductor, google  # noqa: F401 (mail dependencies)
from connections.google.calendar_document import attendee_emails, compose_document
from tools import action_gate, google_calendar
from tools.action_drafts import record_draft
from tools.action_gate import Approved, Refusal
from tools.google_calendar import CalendarDraftInviteTool, CalendarListTool, CalendarSendInviteTool


async def _allowed(_kwargs):
    return None


@pytest.mark.asyncio
async def test_list_requires_offset_and_refuses_ranges_over_31_days(monkeypatch):
    monkeypatch.setattr(google_calendar, "_designated", _allowed)
    tool = CalendarListTool()
    assert await tool.execute(
        time_min="2026-10-15T09:00:00", time_max="2026-10-16T09:00:00-07:00",
    ) == f"Error: {google_calendar.OFFSET_REQUIRED}"
    assert await tool.execute(
        time_min="2026-10-01T00:00:00-07:00", time_max="2026-11-01T00:00:01-07:00",
    ) == f"Error: {google_calendar.RANGE_TOO_LARGE}"


@pytest.mark.asyncio
async def test_list_defaults_to_primary_expands_recurring_and_preserves_google_times(monkeypatch):
    monkeypatch.setattr(google_calendar, "_designated", _allowed)
    calls = []

    async def get_json(url, *, params):
        calls.append((url, params))
        return {"items": [
            {"id": "timed", "start": {"dateTime": "2026-10-15T09:00:00-07:00"},
             "end": {"dateTime": "2026-10-15T10:00:00-07:00"}, "summary": "Standup",
             "attendees": [{"email": "sam@example.com"}], "transparency": "opaque"},
            {"id": "all-day", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"},
             "summary": "Holiday", "transparency": "transparent"},
        ], "nextPageToken": "more"}

    monkeypatch.setattr(google_calendar.google_client, "get_json", get_json)
    result = json.loads(await CalendarListTool().execute(
        time_min="2026-10-15T00:00:00-07:00", time_max="2026-10-16T00:00:00-07:00",
    ))
    assert calls == [(google_calendar._event_url("primary"), {
        "timeMin": "2026-10-15T00:00:00-07:00", "timeMax": "2026-10-16T00:00:00-07:00",
        "maxResults": 250, "singleEvents": "true", "orderBy": "startTime",
    })]
    assert result == {"events": [
        {"id": "timed", "start": "2026-10-15T09:00:00-07:00", "end": "2026-10-15T10:00:00-07:00",
         "title": "Standup", "attendees": ["sam@example.com"], "free/busy": "busy"},
        {"id": "all-day", "start": "2026-10-16", "end": "2026-10-17", "title": "Holiday",
         "attendees": [], "free/busy": "free"},
    ], "note": google_calendar.CUT_NOTE}


def test_first_description_lines_and_email_refusal_bytes_are_pinned():
    assert CalendarListTool().description.splitlines()[0] == (
        "List the person's Google Calendar events in a time range of up to 31 days"
    )
    assert CalendarDraftInviteTool().description.splitlines()[0] == (
        "Save a calendar invite as a draft document for the person to review; nothing is sent"
    )
    assert CalendarSendInviteTool().description.splitlines()[0] == (
        "Send a saved invite draft after the person says to send it; attendees get emails"
    )
    assert action_gate.CHANGED_OUTSIDE == (
        "Not sent: the draft was changed outside the draft tools. Save it with email_draft, "
        "show the person, and wait for their reply."
    )
    assert action_gate._changed_outside("invite") == (
        "Not sent: the draft was changed outside the draft tools. Save it with calendar_draft_invite, "
        "show the person, and wait for their reply."
    )


@pytest.mark.asyncio
async def test_send_recovery_is_invite_specific_for_lost_stale_and_sending_drafts(monkeypatch):
    async def run_direct(fn, *args):
        return fn(*args)

    monkeypatch.setattr(google_calendar, "run_in_db", run_direct)
    monkeypatch.setattr(google_calendar, "_open_draft", lambda *_args: None)
    tool = CalendarSendInviteTool()
    assert await tool._prepare("thread", "lost", "revision") == f"Error: {google_calendar.NO_OPEN_DRAFT}"

    monkeypatch.setattr(google_calendar, "_open_draft", lambda *_args: {"state": "sending"})
    assert await tool._prepare("thread", "sending", "revision") == f"Error: {google_calendar.STILL_SENDING}"

    monkeypatch.setattr(google_calendar, "_open_draft", lambda *_args: {"state": "open"})

    async def stale(*_args):
        return Refusal(action_gate.STALE_REVISION)

    monkeypatch.setattr(google_calendar, "check_action", stale)
    assert await tool._prepare("thread", "stale", "revision") == f"Error: {google_calendar.NO_OPEN_DRAFT}"


@pytest.mark.asyncio
async def test_draft_and_send_follow_the_email_lifecycle_with_attendee_notifications(mail, monkeypatch):
    requests = []
    inner = mail.fake.handler

    def calendar_handler(request):
        url = str(request.url).split("?")[0]
        if url == google_calendar._event_url("primary") and request.method == "POST":
            requests.append(request)
            return httpx.Response(200, json={"id": "event-1", "htmlLink": "https://calendar.google/e/1"})
        return inner(request)

    mail.fake.handler = calendar_handler
    saved = json.loads(await CalendarDraftInviteTool().execute(
        _thread_id=mail.thread, title="Planning", start="2026-10-15T09:00:00-07:00",
        end="2026-10-15T10:00:00-07:00", attendees=["sam@example.com"], description="Agenda",
    ))
    assert saved["from"] == "person@example.com"
    document = (mail.workspace / saved["path"]).read_text()
    assert document == (
        "From: person@example.com\nTitle: Planning\nWhen: 2026-10-15T09:00:00-07:00 to "
        "2026-10-15T10:00:00-07:00\nAttendees: sam@example.com\n"
        "Sending this emails an invitation to every attendee.\n\nAgenda\n"
    )

    async def approved(_kind, draft_id, _revision):
        row = get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
        return Approved(dict(row), (config.WORKSPACE / row["path"]).read_bytes())

    monkeypatch.setattr(google_calendar, "check_action", approved)
    result = json.loads(await CalendarSendInviteTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    ))
    assert result == {"event_id": "event-1", "html_link": "https://calendar.google/e/1"}
    assert json.loads(requests[0].content) == {
        "summary": "Planning", "start": {"dateTime": "2026-10-15T09:00:00-07:00"},
        "end": {"dateTime": "2026-10-15T10:00:00-07:00"},
        "attendees": [{"email": "sam@example.com"}], "description": "Agenda",
    }
    assert requests[0].url.params["sendUpdates"] == "all"
    row = get_db().execute("SELECT state, external_id FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()
    assert tuple(row) == ("sent", "event-1")


async def _no_designation(_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_seen", "stale", "changed"])
async def test_send_uses_the_real_gate_for_invite_refusals(deleg_db, workspace, monkeypatch, case):
    monkeypatch.setattr(google_calendar, "_designated", _no_designation)
    data = b"From: person@example.com\nTitle: Planning\nWhen: 2026-10-15T09:00:00-07:00 to 2026-10-15T10:00:00-07:00\nAttendees: sam@example.com\n\nAgenda\n"
    draft = _draft(deleg_db, workspace, data=data, kind="invite")
    _admit(deleg_db, case)
    _bind(deleg_db, case)
    revision = draft["revision"]
    if case == "not_seen":
        draft = record_draft(draft["draft_id"], data, db=deleg_db)
        deleg_db.commit()
        expected = f"Error: {action_gate.NOT_SEEN}"
    elif case == "stale":
        revision = "0" * 64
        expected = f"Error: {google_calendar.NO_OPEN_DRAFT}"
    else:
        path = workspace / "draft.md"
        before = path.stat()
        path.write_bytes(b"edited outside the draft tool")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        expected = (
            "Error: Not sent: the draft was changed outside the draft tools. Save it with "
            "calendar_draft_invite, show the person, and wait for their reply."
        )
    assert await CalendarSendInviteTool().execute(
        _thread_id="sender-self", draft_id=draft["draft_id"], revision=revision,
    ) == expected


async def _approved_calendar_draft(draft_id: str) -> Approved:
    row = get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
    return Approved(dict(row), (config.WORKSPACE / row["path"]).read_bytes())


async def _calendar_draft(mail) -> dict:
    return json.loads(await CalendarDraftInviteTool().execute(
        _thread_id=mail.thread, title="Planning", start="2026-10-15T09:00:00-07:00",
        end="2026-10-15T10:00:00-07:00", attendees=["sam@example.com"], description="Agenda",
    ))


@pytest.mark.asyncio
async def test_provable_calendar_insert_failure_releases_the_claim(mail, monkeypatch):
    inner = mail.fake.handler

    def handler(request):
        if str(request.url).split("?")[0] == google_calendar._event_url("primary"):
            return httpx.Response(400, json={})
        return inner(request)

    async def approved(_kind, draft_id, _revision):
        return await _approved_calendar_draft(draft_id)

    mail.fake.handler = handler
    monkeypatch.setattr(google_calendar, "check_action", approved)
    saved = await _calendar_draft(mail)
    output = await CalendarSendInviteTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert output.startswith("Error: Not sent: Google refused the request (HTTP 400).")
    row = get_db().execute("SELECT state FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()
    assert row["state"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["500", "noid"])
async def test_uncertain_calendar_insert_keeps_the_claim_and_cannot_repeat(mail, monkeypatch, outcome):
    calls = []
    inner = mail.fake.handler

    def handler(request):
        if str(request.url).split("?")[0] == google_calendar._event_url("primary"):
            calls.append(request)
            if outcome == "500":
                return httpx.Response(500, json={})
            return httpx.Response(200, json={})
        return inner(request)

    async def approved(_kind, draft_id, _revision):
        return await _approved_calendar_draft(draft_id)

    mail.fake.handler = handler
    monkeypatch.setattr(google_calendar, "check_action", approved)
    saved = await _calendar_draft(mail)
    first = await CalendarSendInviteTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert first == f"Error: {google_calendar.UNCONFIRMED}"
    row = get_db().execute("SELECT state FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()
    assert row["state"] == "sending"
    again = await CalendarSendInviteTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert again == f"Error: {google_calendar.STILL_SENDING}"
    assert len(calls) == 1


def test_invite_document_discloses_attendee_notification_and_keeps_from_first():
    document = compose_document(
        "person@example.com", "Planning", "2026-10-15T09:00:00-07:00",
        "2026-10-15T10:00:00-07:00", ["sam@example.com"], "Agenda",
    )
    assert document.startswith("From: person@example.com\n")
    assert "Attendees: sam@example.com\nSending this emails an invitation to every attendee.\n" in document


@pytest.mark.parametrize("value", [
    "a@b.example\\nTitle: x", "a@b.example, c@d.example", "Name <a@b.example>",
    "a b@c.example", "ab.example", "a@@b.example", "sam\u202e@example.com", "sam\u200b@example.com",
])
def test_attendee_emails_rejects_non_plain_addresses(value):
    with pytest.raises(ValueError, match="Attendees must be email addresses"):
        attendee_emails([value])
    assert attendee_emails(["a.b+c@d.example"]) == ["a.b+c@d.example"]


@pytest.mark.asyncio
async def test_draft_refuses_to_record_changed_readback(mail, monkeypatch):
    monkeypatch.setattr(google_calendar, "_read_document", lambda _relative: b"changed")
    before = get_db().execute("SELECT COUNT(*) FROM action_drafts").fetchone()[0]
    output = await CalendarDraftInviteTool().execute(
        _thread_id=mail.thread, title="Planning", start="2026-10-15T09:00:00-07:00",
        end="2026-10-15T10:00:00-07:00", attendees=["sam@example.com"], description="Agenda",
    )
    assert output == (
        "Error: The invite draft changed while it was being saved, so it is not recorded. "
        "Save it again with calendar_draft_invite."
    )
    assert get_db().execute("SELECT COUNT(*) FROM action_drafts").fetchone()[0] == before


@pytest.mark.asyncio
async def test_send_refuses_when_the_google_account_changed(mail, monkeypatch):
    saved = await _calendar_draft(mail)

    async def approved(_kind, draft_id, _revision):
        return await _approved_calendar_draft(draft_id)

    calls = []

    async def request_json(*_args, **_kwargs):
        calls.append(True)
        return {"id": "unexpected"}

    monkeypatch.setattr(google_calendar, "check_action", approved)
    monkeypatch.setattr(google_calendar.oauth, "account_status", lambda: {"email": "other@example.com"})
    monkeypatch.setattr(google_calendar.google_client, "request_json", request_json)
    output = await CalendarSendInviteTool().execute(
        _thread_id=mail.thread, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert output == (
        "Error: Not sent: Google is now signed in as other@example.com, but this invite was "
        "drafted from person@example.com. Save it again with calendar_draft_invite and show the person."
    )
    assert calls == []
    row = get_db().execute("SELECT state FROM action_drafts WHERE id=?", (saved["draft_id"],)).fetchone()
    assert row["state"] == "open"
