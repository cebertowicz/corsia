"""Reply threading for a mail-host draft: one message's headers, read from the same account over IMAP.

Only headers are requested, never a body.  The result feeds the draft's target the way Gmail's does:
``in_reply_to`` and ``references`` thread the send, the label is the line the person sees, and the
reply addresses and subject prefix are defaults the draft tool applies.
"""

from __future__ import annotations

import email
import email.policy
import json
from email.utils import getaddresses

from connections.google.client import GoogleError
from connections.mail.email_document import DraftRefused, is_plain_address, reply_target_label
from connections.mailhost import imap, read
from infra.db_async import run_in_db


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


async def reply_target(account_id: str, message_id: str) -> tuple[dict, str]:
    """Threading facts and the line the person sees; the message must be in this account's inbox."""
    try:
        validity, uid = read.parse_message_id(message_id)
    except read.MessageNotFound as exc:
        raise DraftRefused(str(exc)) from None
    endpoints, username, password = await run_in_db(read.credentials, account_id)

    def call() -> bytes | None:
        with imap.account_lock(account_id):
            return imap.reply_headers(endpoints, username, password, validity, uid)

    raw = await read._guarded(account_id, endpoints, call)
    if raw is None:
        raise DraftRefused(read.NOT_FOUND)
    headers = email.message_from_bytes(raw, policy=email.policy.default)
    message_header = _one_line(headers.get("Message-ID"))
    references = " ".join(part for part in (_one_line(headers.get("References")), message_header) if part)
    sender = _one_line(headers.get("From"))
    reply_to = _one_line(headers.get("Reply-To")) or sender
    target = {
        "reply_to": f"{validity}-{uid}",
        "in_reply_to": message_header or None,
        "references": references or None,
        "reply_addresses": [address for _, address in getaddresses([reply_to]) if is_plain_address(address)],
        "prefix_reply_subject": True,
    }
    return target, reply_target_label(sender, _one_line(headers.get("Subject")))


async def kept_reply_label(account_id: str, existing: dict | None) -> str | None:
    """The line for an update that names no new reply target but still threads under the old.

    A message that can no longer be read must not block the person's edit.
    """
    try:
        kept = json.loads(existing["target_json"] or "{}").get("reply_to") if existing else None
    except (TypeError, ValueError, AttributeError):
        kept = None
    if not kept:
        return None
    try:
        return (await reply_target(account_id, str(kept)))[1]
    except (DraftRefused, GoogleError):
        return reply_target_label(None, None)
