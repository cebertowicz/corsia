"""Mail-host Drafts/Sent copies and SMTP delivery.

The shared tools own the reviewed-draft and confirmation lifecycle.  This module
owns only protocol work, so every mail host follows that one gate.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import make_msgid

from icalendar import Calendar, Event

from connections.calendar_document import Invite
from connections.mailhost import imap, read, smtp
from connections.mailhost.errors import MailHostError
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

DRAFTS = "Drafts"
SENT = "Sent"


async def save_draft(account_id: str, message: EmailMessage) -> None:
    """Append the exact reviewed message to the host's Drafts folder."""
    endpoints, username, password = await run_in_db(read.credentials, account_id)

    _from_account(message, endpoints.username)

    def call() -> None:
        with imap.account_lock(account_id):
            imap.append(endpoints, username, password, DRAFTS, message.as_bytes())

    await read._guarded(account_id, endpoints, call)


async def send(account_id: str, message: EmailMessage) -> str:
    """Send one approved message and append a Sent copy when SMTP does not do so itself."""
    endpoints, username, password = await run_in_db(read.credentials, account_id)
    _from_account(message, endpoints.username)
    if not message.get("Message-ID"):
        message["Message-ID"] = make_msgid()

    def deliver() -> None:
        smtp.send_message(endpoints, endpoints.username, password, message)

    def keep_copy() -> None:
        with imap.account_lock(account_id):
            imap.append(endpoints, username, password, SENT, message.as_bytes())

    await read._guarded(account_id, endpoints, deliver)
    if not endpoints.smtp_saves_sent:
        try:
            await read._guarded(account_id, endpoints, keep_copy)
        except MailHostError:
            # The host accepted the message: a missing Sent copy must not read as a failed send.
            log.warning("mail host delivered a message but its Sent copy was not saved")
    return str(message["Message-ID"])


def _from_account(message: EmailMessage, address: str) -> None:
    """The sender is always the connected account, never a header the draft carried."""
    del message["From"]
    message["From"] = address


def invite_message(sender: str, invite: Invite) -> EmailMessage:
    """A standards-shaped iCalendar email for hosts without server-side scheduling."""
    calendar = Calendar()
    calendar.add("prodid", "-//CORSIA//mail-host invite//EN")
    calendar.add("version", "2.0")
    calendar.add("method", "REQUEST")
    event = Event()
    event.add("uid", make_msgid().strip("<>"))
    event.add("summary", invite.title)
    event.add("dtstart", invite.start_at)
    event.add("dtend", invite.end_at)
    event.add("description", invite.description)
    event.add("organizer", f"mailto:{sender}")
    for attendee in invite.attendees:
        event.add("attendee", f"mailto:{attendee}")
    calendar.add_component(event)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(invite.attendees)
    message["Subject"] = f"Invitation: {invite.title}"
    message.set_content(invite.description or f"Invitation: {invite.title}")
    message.add_alternative(calendar.to_ical().decode("utf-8"), subtype="calendar", params={"method": "REQUEST"})
    return message


async def send_invite(account_id: str, sender: str, invite: Invite) -> str:
    """Fallback for hosts with no verified RFC 6638 scheduling support: send the invite by SMTP."""
    return await send(account_id, invite_message(sender, invite))
