"""Mail-host Drafts/Sent copies and SMTP delivery.

The shared tools own the reviewed-draft and confirmation lifecycle.  This module
owns only protocol work, so every mail host follows that one gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4

from connections.calendar_document import Invite
from connections.mailhost import caldav, folders, imap, invites, read, smtp
from connections.mailhost.errors import MailHostError
from infra.db_async import run_in_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InviteSent:
    uid: str
    on_calendar: bool


def _from_account(message: EmailMessage, address: str) -> None:
    """The sender is always the connected account, never a header the draft carried."""
    del message["From"]
    message["From"] = address


async def _keep_copy(account_id: str, kind: str, message: EmailMessage) -> bool:
    """Append a copy to the host's Drafts or Sent folder; False (logged) when the host shows no such folder."""
    endpoints, username, password = await run_in_db(read.credentials, account_id)

    def call() -> bool:
        with imap.account_lock(account_id):
            return imap.append(endpoints, username, password, kind, message.as_bytes())

    saved = await read._guarded(account_id, endpoints, call)
    if not saved:
        log.warning("mail host shows no %s folder; the copy was not saved", kind)
    return bool(saved)


async def save_draft(account_id: str, message: EmailMessage) -> None:
    """Append the exact reviewed message to the host's Drafts folder."""
    endpoints = (await run_in_db(read.credentials, account_id))[0]
    _from_account(message, endpoints.username)
    await _keep_copy(account_id, folders.DRAFTS, message)


async def send(account_id: str, message: EmailMessage) -> str:
    """Send one approved message and append a Sent copy when SMTP does not do so itself."""
    endpoints, _username, password = await run_in_db(read.credentials, account_id)
    _from_account(message, endpoints.username)
    if not message.get("Message-ID"):
        message["Message-ID"] = make_msgid()

    def deliver() -> None:
        smtp.send_message(endpoints, endpoints.username, password, message)

    await read._guarded(account_id, endpoints, deliver)
    if not endpoints.smtp_saves_sent:
        try:
            await _keep_copy(account_id, folders.SENT, message)
        except MailHostError:
            # The host accepted the message: a missing Sent copy must not read as a failed send.
            log.warning("mail host delivered a message but its Sent copy was not saved")
    return str(message["Message-ID"])


async def send_invite(account_id: str, sender: str, invite: Invite) -> InviteSent:
    """Email the request, then put the same event (same UID) on the organizer's calendar.

    Hosts with no verified RFC 6638 scheduling get the invitation by SMTP.  The calendar copy comes only after
    the host accepted the mail, so a failed send leaves nothing behind to duplicate on a retry; a calendar that
    will not take the event never undoes the send, and the result says so.
    """
    endpoints, _username, password = await run_in_db(read.credentials, account_id)
    uid = str(uuid4())
    await send(account_id, invites.invite_message(sender, invite, uid))
    stored = await caldav.put_event(endpoints, password, uid, invites.stored_event(sender, invite, uid))
    return InviteSent(uid, stored)
