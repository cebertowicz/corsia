"""Provider-independent checks for replying to a message.

Provider adapters supply complete thread and sender-search results.  This module does
no I/O: a failed provider read must be handled as a refusal by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parseaddr

from connections.mail.email_document import reply_addresses

_NO_REPLY = re.compile(r"(^|[.@_-])(no[-_]?reply|do[-_]?not[-_]?reply|donotreply|noreply)([.@_-]|$)", re.I)
_PREFIX = re.compile(r"^\s*(re|fwd|fw|message replied|aw|sv|antw|wg)\s*:\s*", re.I)


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: datetime
    body: str
    reply_to: str = ""
    unread: bool = False
    visible_evidence: str = ""


@dataclass
class Verdict:
    recipients: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)


def normalize_subject(subject: str) -> str:
    """Strip reply prefixes and normalize whitespace and case for conversation matching."""
    previous = None
    while subject != previous:
        previous, subject = subject, _PREFIX.sub("", subject, count=1)
    return " ".join(subject.casefold().split())


def _identity(sender: str) -> tuple[str, str]:
    name, address = parseaddr(sender)
    return " ".join(name.casefold().split()), address.casefold()


def same_sender(left: str, right: str) -> bool:
    """Display names distinguish people sharing a relay address when both are present."""
    ln, la = _identity(left)
    rn, ra = _identity(right)
    if ln and rn and ln == rn:
        return True
    return bool(la and la == ra and (not ln or not rn or ln == rn))


def evidence(message: Message) -> str:
    state = "unread" if message.unread else "read"
    return (f"{message.sender} | {message.date.isoformat()} | {state} | "
            f"thread {message.thread_id} | message {message.message_id}\n"
            + (message.visible_evidence or "Text withheld; use email_read or ask the person to look in their mail app."))


def check_reply(
    target: Message | None, thread: list[Message], sender_messages: list[Message],
    own_address: str, body: str, requested_to: list[str], now: datetime,
) -> Verdict:
    """Return stops, overridable stale stops, warnings and the sender's 30-day thread count."""
    result = Verdict()
    if not body.strip():
        result.stops.append("The message body is empty.")
    if target is None:
        result.stops.append("Couldn't read the message being replied to.")
        return result
    _, sender_address = _identity(target.sender)
    recipients = reply_addresses(target.reply_to, target.sender)
    if not recipients:
        result.stops.append("The original message has no usable reply address.")
    elif not target.reply_to and _NO_REPLY.search(sender_address.split("@", 1)[0]):
        result.stops.append(f"The sender {sender_address} is a no-reply address with no Reply-To.")
    else:
        result.recipients = recipients
        if requested_to and [x.casefold() for x in requested_to] != [x.casefold() for x in recipients]:
            result.warnings.append(f"The requested recipient differs from the original reply address {', '.join(recipients)}; the reply will go to {', '.join(requested_to)}.")
    for message in thread:
        if message.date <= target.date or message.message_id == target.message_id:
            continue
        if _identity(message.sender)[1] == own_address.casefold():
            result.stops.append("You already replied after the target:\n" + evidence(message))
        elif same_sender(message.sender, target.sender):
            result.stale.append("The sender wrote again in this thread:\n" + evidence(message))
        else:
            result.warnings.append("Another person wrote in this thread:\n" + evidence(message))
    subject = normalize_subject(target.subject)
    for message in sender_messages:
        if (message.thread_id == target.thread_id or message.date <= target.date
                or not same_sender(message.sender, target.sender)
                or not subject or normalize_subject(message.subject) != subject):
            continue
        detail = "The sender wrote about the same subject in another thread:\n" + evidence(message)
        if message.unread and message.date >= now - timedelta(days=7):
            result.stale.append(detail)
        else:
            result.warnings.append(detail)
    threads = {message.thread_id for message in sender_messages
               if message.date >= now - timedelta(days=30) and same_sender(message.sender, target.sender)}
    result.info.append(f"{len(threads)} thread(s) from {target.sender} in the last 30 days.")
    return result
