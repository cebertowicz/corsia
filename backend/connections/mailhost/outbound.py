"""Mail-host Drafts/Sent copies and SMTP delivery.

The shared tools own the reviewed-draft and confirmation lifecycle.  This module
owns only protocol work, so every mail host follows that one gate.  The host's
Drafts and Sent folders end up the way the person's own mail app leaves them:
one current copy of a draft, none once it is sent, and sent mail already read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4

from connections.account_errors import AccountError
from connections.calendar_document import Invite
from connections.connected_accounts import get_account
from connections.mailhost import caldav, folders, imap, invites, read, smtp
from connections.mailhost.endpoints import Endpoints
from connections.mailhost.errors import MailHostError
from infra.database import get_db
from infra.db_async import run_in_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sender:
    """One connected account's address and sign-in, read once before anything goes to the host."""

    account_id: str
    address: str
    endpoints: Endpoints
    username: str
    password: str = field(repr=False)
    check: dict[str, object] = field(repr=False)


@dataclass(frozen=True)
class DraftCopy:
    saved: bool
    earlier_left: bool


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    not_delivered_to: tuple[str, ...]
    sent_copy_missing: bool
    draft_copy_left: bool


@dataclass(frozen=True)
class InviteSent:
    uid: str
    on_calendar: bool
    not_delivered_to: tuple[str, ...]
    sent_copy_missing: bool


def refused_before_send(exc: Exception) -> bool:
    """Whether a failed :func:`send` or :func:`send_invite` proves nothing went out.

    Every account or mail-host refusal (``MailHostError`` is an ``AccountError``) happens
    before SMTP accepts the message; after acceptance a transport error propagates as itself
    and a Sent-copy, Drafts-copy or calendar failure becomes a flag on the result.
    """
    return isinstance(exc, AccountError)


def new_message_id(address: str) -> str:
    """A Message-ID on the account's own domain: the default would put this computer's name in every copy."""
    return make_msgid(domain=address.rpartition("@")[2] or "localhost")


def _account_address(account_id: str) -> str:
    db = get_db()
    try:
        return str(get_account(account_id, db=db)["address"])
    finally:
        db.close()


async def sender(account_id: str) -> Sender:
    """The account's address and sign-in; an :class:`AccountError` when it has no usable password."""
    endpoints, username, password, check = await run_in_db(read.credentials, account_id)
    return Sender(account_id, await run_in_db(_account_address, account_id), endpoints, username, password, check)


def _from_account(message: EmailMessage, address: str) -> None:
    """The sender is always the connected account, never a header the draft carried."""
    del message["From"]
    message["From"] = address


async def _keep_copy(account: Sender, kind: str, message: EmailMessage) -> bool:
    """Append a copy to the host's Drafts or Sent folder; False when it has no such folder."""

    def call() -> bool:
        with imap.account_lock(account.account_id):
            return imap.append(account.endpoints, account.username, account.password, kind, message.as_bytes())

    saved = await read.guarded(account.account_id, account.check, call)
    if not saved:
        log.warning("mail host shows no %s folder; the copy was not saved", kind)
    return bool(saved)


async def _drop_copy(account: Sender, kind: str, message_id: str) -> bool:
    """Remove CORSIA's own earlier copy; False, logged and never raised, when the host keeps it."""

    def call() -> None:
        with imap.account_lock(account.account_id):
            imap.remove_copy(account.endpoints, account.username, account.password, kind, message_id)

    try:
        await read.guarded(account.account_id, account.check, call)
    except Exception:  # noqa: BLE001 - removing a convenience copy never fails a save or a send
        log.warning("mail host kept a %s copy CORSIA meant to remove", kind)
        return False
    return True


async def save_draft(account_id: str, message: EmailMessage, *, copy_id: str, replaces: str | None) -> DraftCopy:
    """Append the exact reviewed message to the host's Drafts folder as ``copy_id``, then drop the copy it replaces."""
    account = await sender(account_id)
    _from_account(message, account.address)
    del message["Message-ID"]
    message["Message-ID"] = copy_id
    saved = await _keep_copy(account, folders.DRAFTS, message)
    earlier_left = bool(saved and replaces and replaces != copy_id) and not await _drop_copy(
        account, folders.DRAFTS, str(replaces),
    )
    return DraftCopy(saved, earlier_left)


async def send(account: Sender, message: EmailMessage, *, draft_copy: str | None = None) -> SentMessage:
    """Submit one approved message; once SMTP accepts it, keep a Sent copy and drop the draft's Drafts copy."""
    _from_account(message, account.address)
    if not message.get("Message-ID"):
        message["Message-ID"] = new_message_id(account.address)

    def deliver() -> list[str]:
        return smtp.send_message(account.endpoints, account.endpoints.username, account.password, message,
                                 from_addr=account.address)

    refused = await read.guarded(account.account_id, account.check, deliver)

    def already_copied() -> bool:
        with imap.account_lock(account.account_id):
            return imap.has_copy(account.endpoints, account.username, account.password, folders.SENT, str(message["Message-ID"]))

    try:
        copied = await read.guarded(account.account_id, account.check, already_copied)
    except MailHostError:
        log.warning("mail host Sent-copy check failed; attempting to save a copy")
        copied = False
    try:
        sent_copy_missing = not copied and not await _keep_copy(account, folders.SENT, message)
    except MailHostError:
        log.warning("mail host delivered a message but its Sent copy was not saved")
        sent_copy_missing = True
    draft_copy_left = bool(draft_copy) and not await _drop_copy(account, folders.DRAFTS, str(draft_copy))
    return SentMessage(str(message["Message-ID"]), tuple(refused), sent_copy_missing, draft_copy_left)


async def send_invite(account: Sender, invite: Invite) -> InviteSent:
    """Email the invitation, then put the same event on the organizer's calendar."""
    uid = str(uuid4())
    sent = await send(account, invites.invite_message(account.address, invite, uid))
    stored = await caldav.put_event(account.endpoints, account.password, uid, invites.stored_event(account.address, invite, uid))
    return InviteSent(uid, stored, sent.not_delivered_to, sent.sent_copy_missing)
