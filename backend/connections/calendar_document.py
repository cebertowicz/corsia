"""Provider-neutral calendar invite draft formatting and validation."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from connections.mail.email_document import is_plain_address


OFFSET_REQUIRED = "Give times with a UTC offset, e.g. 2026-10-15T09:00:00-07:00."


def parse_time(value: Any) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise ValueError(OFFSET_REQUIRED)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(OFFSET_REQUIRED) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(OFFSET_REQUIRED)
    return value, parsed


def attendee_emails(value: Any) -> list[str]:
    invalid = (not isinstance(value, list) or any(
        not isinstance(item, str)
        or item.count("@") != 1
        or not all(item.split("@"))
        or not is_plain_address(item)
        for item in value
    ))
    if invalid:
        raise ValueError("Attendees must be email addresses.")
    return value


def one_line_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"An invite needs a one-line {label}.")
    return value.strip()


def compose_document(account: str, title: str, start: str, end: str, attendees: list[str], description: str) -> str:
    return (
        f"From: {account}\nTitle: {title}\nWhen: {start} to {end}\n"
        f"Attendees: {', '.join(attendees)}\n"
        f"Sending this emails an invitation to every attendee.\n\n{description}\n"
    )


@dataclass(frozen=True)
class Invite:
    """The invite fields every provider needs, validated from tool input or a draft's target."""

    title: str
    start: str
    end: str
    attendees: list[str]
    description: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class Draft:
    """What a provider reads back out of an approved invite draft, plus its raw recorded fields."""

    invite: Invite
    address: str
    account_id: Any
    target: Mapping[str, Any]


def read_invite(source: Mapping[str, Any]) -> Invite:
    """Validate the invite fields common to every provider, read from tool input or a draft's target.

    Raises :class:`ValueError` naming the field at fault, so the calling tool answers with its
    own fixed text instead of this module's.
    """
    title = one_line_text(source.get("title"), "title")
    start, start_at = parse_time(source.get("start"))
    end, end_at = parse_time(source.get("end"))
    if end_at <= start_at:
        raise ValueError("The invite end must be after its start.")
    attendees = attendee_emails(source.get("attendees"))
    description = source.get("description", "")
    if not isinstance(description, str):
        raise ValueError("The invite description must be text.")
    return Invite(title, start, end, attendees, description, start_at, end_at)


def read_draft(data: bytes, target_json: str | None, *, require_account_id: bool = False) -> Draft:
    """Validate an approved invite draft the way every provider does before it can be sent.

    ``require_account_id`` covers the providers that send from the account recorded in the draft
    and so refuse a draft naming none; a provider that takes the account on the call leaves it off.
    Raises :class:`ValueError` when the draft is not in the draft format, again so the calling tool
    keeps its own fixed text.
    """
    if not data.decode("utf-8").startswith("From: "):
        raise ValueError("The invite draft has no From line.")
    target = json.loads(target_json or "{}")
    if not isinstance(target, dict):
        raise ValueError("The invite draft's recorded fields are not a record.")
    invite = read_invite(target)
    address, account_id = target.get("account"), target.get("account_id")
    if not isinstance(address, str):
        raise ValueError("The invite draft has no From address.")
    if require_account_id and not account_id:
        raise ValueError("The invite draft names no account to send from.")
    return Draft(invite=invite, address=address, account_id=account_id, target=target)
