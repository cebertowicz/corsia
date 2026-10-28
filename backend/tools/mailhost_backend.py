"""The mail-host backend: IMAP for reading and the Drafts/Sent copies, SMTP for sending, CalDAV for the calendar.

The host's Drafts folder mirrors the reviewed draft as the person's own mail app would: each save
appends the current copy under a Message-ID recorded on the draft (``host_copy_id``) and removes the
copy it replaced, and a confirmed send removes the last one. A copy that can't be saved or removed
is a note on the result; the reviewed draft and the send never depend on it.
"""

from __future__ import annotations

import json
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from connections.account_errors import AccountError
from connections.calendar_document import Invite
from connections.mail.email_document import build_message, parse_document
from connections.mailhost import agenda, guard_read, outbound, read as mailhost_read, reply, smtp
from tools.action_delivery import deliver_claimed
from tools.reviewed_drafts import draft_target

if TYPE_CHECKING:
    from tools.mail_backends import Gate

COPY_ID = "host_copy_id"
NOT_COPIED = "The draft was saved, but it wasn't copied to the account's Drafts folder."
EARLIER_LEFT = "The draft was saved, but its earlier copy is still in the account's Drafts folder."
ALL_CALENDARS = "This account lists all its calendars together; leave calendar_id out."
EMAIL_UNCONFIRMED = (
    "The mail host didn't confirm the send, so the email may have gone out. Don't send this draft "
    "again: check the person's Sent mail first."
)
INVITE_UNCONFIRMED = (
    "The mail host didn't confirm the invite send, so the invitation may have gone out, but the "
    "event isn't on the person's calendar. Don't send this invite again: check the person's Sent mail first."
)
NOT_ON_CALENDAR = "The invitation was emailed, but the event isn't on the person's calendar."


def _sign_in_error(exc: Exception) -> str:
    return str(exc) if isinstance(exc, AccountError) else "couldn't read this account's settings. Try again."


def _email_sent(sent: outbound.SentMessage, message_id: str, account_id: str) -> str:
    result: dict[str, Any] = {"sent_message_id": message_id, "account_id": account_id}
    if sent.not_delivered_to:
        result["not_delivered_to"] = list(sent.not_delivered_to)
        result["note"] = "Tell the person that these addresses did not get the email."
    result["sent_copy_note"] = ("The email was sent, but a copy was not saved in the account's Sent folder."
                                if sent.sent_copy_missing else "Sent copy saved.")
    if sent.draft_copy_left:
        result["draft_copy_note"] = "The email was sent, but its draft is still in the account's Drafts folder."
    return json.dumps(result)


def _invite_sent(sent: outbound.InviteSent, event_id: str) -> str:
    result: dict[str, Any] = {"event_id": event_id}
    if not sent.on_calendar:
        result["note"] = NOT_ON_CALENDAR
    if sent.not_delivered_to:
        result["not_delivered_to"] = list(sent.not_delivered_to)
        result["delivery_note"] = "Tell the person that these addresses did not get the invitation email."
    result["sent_copy_note"] = ("The invitation was emailed, but a copy was not saved in the account's Sent folder."
                                if sent.sent_copy_missing else "Sent copy saved.")
    return json.dumps(result)


class MailHostBackend:
    read = mailhost_read
    guard_data = staticmethod(guard_read.guard_data)
    sent_html = staticmethod(guard_read.sent_html)
    fetch_body = staticmethod(guard_read.fetch_body)

    async def attachment_limit(self, account_id: str) -> tuple[int | None, bool]:
        sender = await outbound.sender(account_id)

        def read_limit() -> int | None:
            client = smtp._logged_in_client(sender.endpoints, sender.username, sender.password)
            try:
                value = client.esmtp_features.get("size", "").strip()
                return int(value) if value.isdecimal() and int(value) > 0 else None
            finally:
                smtp._close(client)

        return await mailhost_read._guarded(account_id, sender.check, read_limit), True

    async def draft_metadata(self, account: dict, existing: dict | None, reply_id: object) -> tuple[dict, str | None, str]:
        """The reply target named now or kept from the draft it replaces, and a new Message-ID for this copy."""
        account_id = str(account["id"])
        if reply_id:
            target, label = await reply.reply_target(account_id, str(reply_id))
        else:
            target, label = draft_target(existing), await reply.kept_reply_label(account_id, existing)
        return {**target, COPY_ID: outbound.new_message_id(str(account["address"]))}, label, ""

    async def keep_draft_copy(self, account: dict, text: str, target: dict, previous: dict) -> str | None:
        """Append this revision to the host's Drafts folder and remove the copy it replaces."""
        parsed = parse_document(text)
        if parsed is None:
            return NOT_COPIED
        try:
            copy = await outbound.save_draft(
                str(account["id"]), build_message(parsed, target), copy_id=str(target[COPY_ID]),
                replaces=previous.get(COPY_ID),
            )
        except AccountError as exc:
            return f"{NOT_COPIED} {exc}"
        if not copy.saved:
            return NOT_COPIED
        return EARLIER_LEFT if copy.earlier_left else None

    async def send_email(self, draft_id: str, account: dict, message: EmailMessage, target: dict, gate: Gate) -> str:
        """Submit over SMTP; once accepted, a Sent copy is kept and the draft's Drafts copy removed."""
        account_id = str(account["id"])
        return await deliver_claimed(
            draft_id, acquire=lambda: outbound.sender(account_id), provider_errors=(AccountError,),
            send=lambda sender: outbound.send(sender, message, draft_copy=target.get(COPY_ID)),
            result_id=lambda sent: sent.message_id,
            success=lambda sent, message_id: _email_sent(sent, message_id, account_id),
            sign_in_error=_sign_in_error, request_error=str, never_sent=outbound.refused_before_send,
            still_sending=gate.still_sending, claim_error=gate.claim_error, unconfirmed=EMAIL_UNCONFIRMED, label=gate.label,
        )

    async def send_invite(self, draft_id: str, account: dict, invite: Invite, gate: Gate) -> str:
        """Email an iCalendar request, then store the event on the organizer's CalDAV calendar."""
        account_id = str(account["id"])
        return await deliver_claimed(
            draft_id, acquire=lambda: outbound.sender(account_id), provider_errors=(AccountError,),
            send=lambda sender: outbound.send_invite(sender, invite), result_id=lambda sent: sent.uid,
            success=_invite_sent, sign_in_error=_sign_in_error, request_error=str, never_sent=outbound.refused_before_send,
            still_sending=gate.still_sending, claim_error=gate.claim_error, unconfirmed=INVITE_UNCONFIRMED, label=gate.label,
        )

    async def list_events(
        self, account_id: str, calendar_id: str, time_min: str, time_max: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """Every event of every calendar the host offers in the window; there is no per-calendar id."""
        if calendar_id != "primary":
            raise ValueError(ALL_CALENDARS)
        return {"events": await agenda.list_events(account_id, start, end)}


MAIL_HOST = MailHostBackend()
