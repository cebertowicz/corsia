"""Calendar invite draft formatting and validation.

The calendar tools write a person-readable invite document, then recover the
structured event details from the action draft the gate approved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from connections.google.email_document import is_plain_address


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
