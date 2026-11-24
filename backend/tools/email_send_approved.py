"""Send one email draft the person approved with Send now: the exact bytes they saw, from the draft's own account.

The person's click is the approval (``api/email_drafts.py`` checks who they are and that the bytes are the ones on
their screen); nothing here asks a model. The send itself is the one the agent tool made before: the account bound to
the draft, the conversation re-checked for a newer email, the sent-mail font, the quoted original and the pinned
attachments, then one claimed delivery (``tools.action_delivery.deliver_claimed``). The draft row's state afterwards
is the one answer to what happened: ``sent``, back to ``open`` (provably not sent), or still ``sending`` (unconfirmed).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from connections.account_errors import AccountError
from connections.mail import attachments, mail_compose
from connections.mail.email_document import DraftRefused, build_message, parse_document
from connections.mail_accounts import resolve_for_thread
from connections.microsoft.provider import MICROSOFT
from infra.database import get_db
from infra.db_async import run_in_db
from tools.action_delivery import deliver_claimed
from tools.mail_backends import Gate, backend_for
from tools.mail_guard_runtime import new_arrival
from tools.reviewed_drafts import draft_target

log = logging.getLogger(__name__)

SENT, FAILED, UNCONFIRMED, NEWER, IN_FLIGHT = "sent", "failed", "unconfirmed", "newer", "sending"

NO_ACCOUNT = "this draft isn't tied to an email account"
BAD_DOCUMENT = "the draft isn't in the email format, so it can't be read as an email"
FROM_MISMATCH = "the From line doesn't match the account the draft was written from ({account})"
UNGUARDED_REPLY = "this reply has no record of when its conversation was last read; save the draft again"
UNEXPECTED = "something went wrong preparing the email; nothing left"
STILL_SENDING = "an earlier send of this draft didn't finish, so it may already have gone out"
GATE = Gate("send_now", STILL_SENDING, "couldn't check the draft")
MS_UNCONFIRMED = "Microsoft did not confirm the send, so the email may have gone out."


@dataclass(frozen=True)
class SendResult:
    """What the click did: ``state`` is the draft's outcome; ``reason`` is the cause in plain words for the agent."""

    state: str
    account: str | None = None
    reason: str | None = None
    newer: str | None = None
    #: On a send that went out: addresses the mail host refused, and the provider's note for the agent (for example
    #: Gmail filing the email under another conversation than the reply target).
    not_delivered_to: tuple[str, ...] = ()
    note: str | None = None


def _provider(account_id: str) -> str | None:
    db = get_db()
    try:
        row = db.execute("SELECT provider FROM connected_accounts WHERE id=?", (account_id,)).fetchone()
        return str(row["provider"]) if row else None
    finally:
        db.close()


def _draft_state(draft_id: str) -> str | None:
    db = get_db()
    try:
        row = db.execute("SELECT state FROM action_drafts WHERE id=?", (draft_id,)).fetchone()
        return str(row["state"]) if row else None
    finally:
        db.close()


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _error_text(result: str) -> str | None:
    """The cause a delivery's agent-facing error names, in the agent's notice."""
    if not result.startswith("Error: "):
        return None
    return result.removeprefix("Error: ").removeprefix("Not sent: ")


def _sent(account: str, result: str) -> SendResult:
    """A send that went out, with what the delivery said about it beyond 'sent'."""
    try:
        body = json.loads(result)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return SendResult(SENT, account)
    refused = body.get("not_delivered_to")
    note = body.get("note")
    return SendResult(
        SENT, account,
        not_delivered_to=tuple(str(address) for address in refused) if isinstance(refused, list) else (),
        note=note if isinstance(note, str) else None,
    )


async def _outcome(draft_id: str, account: str, result: str) -> SendResult:
    if result == f"Error: {STILL_SENDING}":
        # Another click claimed this draft first; its own request reports what happened.
        return SendResult(IN_FLIGHT, account)
    state = await run_in_db(_draft_state, draft_id)
    if state == "sent":
        return _sent(account, result)
    if state == "sending":
        return SendResult(UNCONFIRMED, account, _error_text(result))
    return SendResult(FAILED, account, _error_text(result) or UNEXPECTED)


async def send_approved(draft: dict, data: bytes, *, read_at: datetime | None) -> SendResult:
    """Send ``data``, the open draft's live bytes the person approved; never raises for a send outcome."""
    target = draft_target(draft)
    account_id = target.get("account_id")
    thread_id = str(draft["thread_id"])
    if not account_id:
        return SendResult(FAILED, None, NO_ACCOUNT)
    try:
        if await run_in_db(_provider, str(account_id)) == MICROSOFT.name:
            return await _send_microsoft(draft, data, str(account_id), thread_id)
        return await _send_mail(draft, data, str(account_id), thread_id, target, read_at)
    except Exception:
        log.exception("send_now: draft %s could not be prepared", draft["id"])
        return SendResult(FAILED, target.get("account"), UNEXPECTED)


async def _send_mail(
    draft: dict, data: bytes, account_id: str, thread_id: str, target: dict, read_at: datetime | None,
) -> SendResult:
    """Gmail and mail hosts: the send ``email_send`` made, from the bound account."""
    from tools.google_email import _font

    label = target.get("account")
    try:
        account = await run_in_db(resolve_for_thread, account_id, thread_id)
        backend = backend_for(account)
    except AccountError as exc:
        return SendResult(FAILED, label, str(exc))
    label = str(account["address"])
    parsed = parse_document(data.decode("utf-8", errors="strict")) if data else None
    if parsed is None:
        return SendResult(FAILED, label, BAD_DOCUMENT)
    # Display only: the sender is always the bound account, never this line.
    if parsed["from"] is not None and parsed["from"].lower() != label.lower():
        return SendResult(FAILED, label, FROM_MISMATCH.format(account=label))
    message = build_message(parsed, target)
    original = None
    try:
        if target.get("reply_to"):
            guard_read_at = _utc(target.get("guard_read_at"))
            if guard_read_at is None:
                return SendResult(FAILED, label, UNGUARDED_REPLY)
            # The view shows every email in the conversation as of its read, so what it showed is not newer.
            since = max(guard_read_at, read_at) if read_at else guard_read_at
            original, newer = await new_arrival(backend, account_id, str(target["reply_to"]), since)
            if newer:
                return SendResult(NEWER, label, newer=newer)
        quote_body = await backend.fetch_body(account_id, original.message_id) if original else None
        face, size, _ = await _font(backend, account_id)
        mail_compose.add_html(message, str(message.get_content()), face, size, quote_body)
        await asyncio.to_thread(attachments.add, thread_id, message, target.get("attachments") or [])
    except (DraftRefused, AccountError) as exc:
        return SendResult(FAILED, label, str(exc))
    result = await backend.send_email(str(draft["id"]), account, message, target, GATE)
    return await _outcome(str(draft["id"]), label, result)


async def _send_microsoft(draft: dict, data: bytes, account_id: str, thread_id: str) -> SendResult:
    """Microsoft: the send ``microsoft_mail_send`` made, under the same designated-thread and reach checks."""
    from connections.microsoft import client, mail
    from connections.microsoft.errors import MicrosoftError
    from connections.connected_accounts import ConnectedAccountError
    from tools.microsoft_account import _designated
    from tools.microsoft_actions import _visible_account

    refusal = await _designated({"_thread_id": thread_id})
    if refusal:
        return SendResult(FAILED, None, refusal.removeprefix("Error: "))
    try:
        row = await run_in_db(_visible_account, account_id, thread_id)
    except ConnectedAccountError as exc:
        return SendResult(FAILED, None, str(exc))
    label = str(row["address"])
    parsed = parse_document(data.decode("utf-8", errors="strict")) if data else None
    if parsed is None:
        return SendResult(FAILED, label, BAD_DOCUMENT)
    if parsed["from"] is not None and parsed["from"].lower() != label.lower():
        return SendResult(FAILED, label, FROM_MISMATCH.format(account=label))
    result = await deliver_claimed(
        str(draft["id"]),
        acquire=lambda: client.access_token(account_id),
        provider_errors=(MicrosoftError,),
        send=lambda token: mail.send(account_id, parsed["to"], parsed["subject"], parsed["body"], cc=parsed["cc"], token=token),
        result_id=lambda sent: sent,
        success=lambda _sent, _external_id: json.dumps({"sent": True}),
        sign_in_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft sign-in is unavailable",
        request_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft could not send this request",
        never_sent=lambda exc: isinstance(exc, MicrosoftError) and exc.never_sent,
        still_sending=STILL_SENDING, claim_error=GATE.claim_error, unconfirmed=MS_UNCONFIRMED, label=GATE.label,
    )
    return await _outcome(str(draft["id"]), label, result)
