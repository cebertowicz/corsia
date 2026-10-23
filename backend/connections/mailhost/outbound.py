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
from connections.connected_accounts import get_account
from connections.mailhost import caldav, folders, imap, invites, read, smtp
from infra.database import get_db
from infra.db_async import run_in_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    not_delivered_to: tuple[str, ...]
    sent_copy_missing: bool


@dataclass(frozen=True)
class InviteSent:
    uid: str
    on_calendar: bool
    not_delivered_to: tuple[str, ...]
    sent_copy_missing: bool


def _from_account(message: EmailMessage, address: str) -> None:
    """The sender is always the connected account, never a header the draft carried."""
    del message["From"]
    message["From"] = address


def _account_address(account_id: str) -> str:
    db = get_db()
    try:
        return str(get_account(account_id, db=db)["address"])
    finally:
        db.close()


async def _keep_copy(account_id: str, kind: str, message: EmailMessage) -> bool:
    """Append a copy to the host's Drafts or Sent folder; False when it has no such folder."""
    endpoints, username, password, check = await run_in_db(read.credentials, account_id)

    def call() -> bool:
        with imap.account_lock(account_id):
            return imap.append(endpoints, username, password, kind, message.as_bytes())

    saved = await read._guarded(account_id, check, call)
    if not saved:
        log.warning("mail host shows no %s folder; the copy was not saved", kind)
    return bool(saved)


async def save_draft(account_id: str, message: EmailMessage) -> bool:
    """Append the exact reviewed message to the host's Drafts folder when that folder exists."""
    _from_account(message, await run_in_db(_account_address, account_id))
    return await _keep_copy(account_id, folders.DRAFTS, message)


async def send(account_id: str, message: EmailMessage) -> SentMessage:
    """Submit one approved message, always attempting a Sent copy after SMTP accepts it."""
    endpoints, _username, password, check = await run_in_db(read.credentials, account_id)
    address = await run_in_db(_account_address, account_id)
    _from_account(message, address)
    if not message.get("Message-ID"):
        message["Message-ID"] = make_msgid()

    def deliver() -> list[str]:
        return smtp.send_message(endpoints, endpoints.username, password, message, from_addr=address)

    refused = await read._guarded(account_id, check, deliver)
    try:
        sent_copy_missing = not await _keep_copy(account_id, folders.SENT, message)
    except Exception:  # noqa: BLE001 - SMTP acceptance makes every later copy failure ancillary
        # SMTP accepted the message: its Sent copy is ancillary, never a failed send.
        log.warning("mail host delivered a message but its Sent copy was not saved")
        sent_copy_missing = True
    return SentMessage(str(message["Message-ID"]), tuple(refused), sent_copy_missing)


async def send_invite(account_id: str, sender: str, invite: Invite) -> InviteSent:
    """Email the invitation, then put the same event on the organizer's calendar."""
    endpoints, _username, password, _check = await run_in_db(read.credentials, account_id)
    uid = str(uuid4())
    sent = await send(account_id, invites.invite_message(sender, invite, uid))
    stored = await caldav.put_event(endpoints, password, uid, invites.stored_event(sender, invite, uid))
    return InviteSent(uid, stored, sent.not_delivered_to, sent.sent_copy_missing)
