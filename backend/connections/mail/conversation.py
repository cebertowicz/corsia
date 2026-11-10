"""The conversation an email draft replies into, as each mail backend reads it in one go for the draft view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import getaddresses


@dataclass(frozen=True)
class ConversationEmail:
    """One email of the conversation, with its text and every address it went between."""

    message_id: str
    sender: str
    date: datetime
    body: str
    #: From, To and Cc, lowercase: the view marks a recipient the answered email didn't have.
    addresses: tuple[str, ...]


def header_addresses(*headers: str) -> tuple[str, ...]:
    return tuple(address.lower() for _, address in getaddresses([h for h in headers if h]) if address)
