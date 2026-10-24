"""The Gmail backend: the Gmail API for mail, Google Calendar for invites and the calendar list.

Every URL is a fixed template with ids URL-encoded, so no model-built URL reaches the HTTP client.
Gmail keeps no host copy of a reviewed draft: the workspace document is the only one.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from connections.calendar_document import Invite
from connections.google import client as google_client, gmail_read
from connections.google.accounts import thread_key
from connections.google.calendar_document import event_summaries
from connections.google.client import GoogleError, GoogleRequestFailed
from connections.google.email_gmail import GMAIL as GMAIL_API, fetch_signature, kept_reply_label, reply_target
from tools.action_delivery import deliver_claimed
from tools.reviewed_drafts import draft_target

if TYPE_CHECKING:
    from tools.mail_backends import Gate

SEND_URL = f"{GMAIL_API}/messages/send"
CALENDAR = "https://www.googleapis.com/calendar/v3"
CUT_NOTE = "The list was cut at 250 events; narrow the range."
EMAIL_UNCONFIRMED = (
    "Google didn't confirm the send, so the email may have gone out. Don't send this draft "
    "again: check the person's Sent mail first."
)
INVITE_UNCONFIRMED = (
    "Google didn't confirm the invite send, so the event may exist. Don't send this invite "
    "again: check the person's calendar first."
)


def event_url(calendar_id: str) -> str:
    return f"{CALENDAR}/calendars/{quote(calendar_id, safe='')}/events"


def _sign_in_error(exc: Exception) -> str:
    return str(exc) if isinstance(exc, GoogleError) else "couldn't reach Google. Try again later."


def _never_sent(exc: Exception) -> bool:
    return isinstance(exc, google_client.GoogleSignInRejected) or isinstance(exc, GoogleRequestFailed) and exc.never_sent


def _sent_id(sent: Any) -> str | None:
    return sent.get("id") if isinstance(sent, dict) else None


class GmailBackend:
    read = gmail_read

    async def draft_metadata(self, account: dict, existing: dict | None, reply_id: object) -> tuple[dict, str | None, str]:
        """The reply target named now or kept from the draft it replaces, and the account's signature."""
        account_id = str(account["id"])
        if reply_id:
            target, label = await reply_target(account_id, str(reply_id))
        else:
            target, label = draft_target(existing), await kept_reply_label(account_id, existing)
        return target, label, await fetch_signature(account_id)

    async def keep_draft_copy(self, account: dict, text: str, target: dict, previous: dict) -> str | None:
        """Nothing to mirror: Gmail sees the message only when it is sent."""
        return None

    async def send_email(self, draft_id: str, account: dict, message: EmailMessage, target: dict, gate: Gate) -> str:
        """Send through the Gmail API, threaded under the reply target's Gmail thread."""
        account_id = str(account["id"])
        body: dict[str, Any] = {"raw": urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        if target.get("thread_id"):
            body["threadId"] = target["thread_id"]
        return await deliver_claimed(
            draft_id, acquire=lambda: google_client.access_token(account_id), provider_errors=(GoogleError,),
            send=lambda bearer: google_client.request_json("POST", SEND_URL, account_id=account_id, body=body, token=bearer),
            result_id=_sent_id,
            success=lambda sent, message_id: json.dumps({"sent_message_id": message_id,
                "thread_id": thread_key(account_id, sent.get("threadId")), "account_id": account_id}),
            sign_in_error=_sign_in_error, request_error=str, never_sent=_never_sent,
            still_sending=gate.still_sending, claim_error=gate.claim_error, unconfirmed=EMAIL_UNCONFIRMED, label=gate.label,
        )

    async def send_invite(self, draft_id: str, account: dict, invite: Invite, gate: Gate) -> str:
        """Create the event on the primary calendar; Google emails every attendee."""
        account_id = str(account["id"])
        body = {"summary": invite.title, "start": {"dateTime": invite.start}, "end": {"dateTime": invite.end},
                "attendees": [{"email": email} for email in invite.attendees], "description": invite.description}
        return await deliver_claimed(
            draft_id, acquire=lambda: google_client.access_token(account_id), provider_errors=(GoogleError,),
            send=lambda bearer: google_client.request_json("POST", event_url("primary"), account_id=account_id,
                body=body, params={"sendUpdates": "all"}, token=bearer),
            result_id=_sent_id,
            success=lambda sent, event_id: json.dumps({"event_id": event_id,
                "html_link": sent.get("htmlLink") if isinstance(sent, dict) else None}),
            sign_in_error=_sign_in_error, request_error=str, never_sent=_never_sent,
            still_sending=gate.still_sending, claim_error=gate.claim_error, unconfirmed=INVITE_UNCONFIRMED, label=gate.label,
        )

    async def list_events(
        self, account_id: str, calendar_id: str, time_min: str, time_max: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """Up to 250 events of one Google calendar, recurring ones expanded."""
        response = await google_client.get_json(account_id, event_url(calendar_id), params={
            "timeMin": time_min, "timeMax": time_max, "maxResults": 250, "singleEvents": "true", "orderBy": "startTime",
        })
        items = response.get("items") if isinstance(response, dict) else None
        result: dict[str, Any] = {"events": event_summaries(items)}
        if isinstance(response, dict) and response.get("nextPageToken"):
            result["note"] = CUT_NOTE
        return result


GMAIL = GmailBackend()
