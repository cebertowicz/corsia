"""A Gmail-style search into IMAP SEARCH criteria.

Agents search every account with Gmail's syntax, so the common parts translate: words and
phrases, ``from:`` ``to:`` ``cc:`` ``subject:``, ``newer_than:`` ``older_than:`` ``after:``
``before:`` and ``is:unread`` ``is:read`` ``is:starred``, each with a leading ``-`` for not.
Anything else is refused with a fixed line that names what works, and the query's text is never
echoed. Text is ASCII only for now (IMAP search needs a character set the host may not offer),
and every value is quoted here, so a model-written query can't add an IMAP command.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

MAX_TERMS = 12
_TOKEN = re.compile(r'(-?)(?:([A-Za-z_]+):)?(?:"([^"]*)"|(\S+))')
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FIELDS = {"from": "FROM", "to": "TO", "cc": "CC", "subject": "SUBJECT"}
_FLAGS = {"unread": "UNSEEN", "read": "SEEN", "starred": "FLAGGED"}
_AGO = re.compile(r"^(\d{1,4})([dwmy])$")
_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
SUPPORTED = (
    "This account searches words and \"phrases\" plus from:, to:, cc:, subject:, newer_than:, older_than:, "
    "after:, before:, is:unread, is:read and is:starred (a leading - means not). Search again with those."
)
NOT_ASCII = "This account can only search for text in plain English letters and digits for now. Search again without other characters."
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class QueryRefused(ValueError):
    """A query this account can't run; the text is fixed and safe to show."""


def _quoted(text: str) -> str:
    if not text.isascii():
        raise QueryRefused(NOT_ASCII)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _day(value: str, *, today: date) -> date:
    ago = _AGO.match(value)
    if ago:
        return today - timedelta(days=int(ago.group(1)) * _DAYS[ago.group(2)])
    parts = re.split(r"[/-]", value)
    if len(parts) != 3 or len(parts[0]) != 4 or not all(part.isdigit() and part.isascii() for part in parts):
        raise QueryRefused(SUPPORTED)
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        raise QueryRefused(SUPPORTED) from None


def _term(operator: str | None, value: str, *, today: date) -> list[str]:
    if operator is None:
        return ["TEXT", _quoted(value)]
    name = operator.lower()
    if name in _FIELDS:
        return [_FIELDS[name], _quoted(value)]
    if name == "is" and value.lower() in _FLAGS:
        return [_FLAGS[value.lower()]]
    if name == "newer_than":
        return ["SINCE", _imap_date(_day(value.lower(), today=today))]
    if name == "older_than":
        return ["BEFORE", _imap_date(_day(value.lower(), today=today))]
    if name in ("after", "before"):
        return ["SINCE" if name == "after" else "BEFORE", _imap_date(_day(value, today=today))]
    raise QueryRefused(SUPPORTED)


def criteria(query: str, *, today: date) -> list[str]:
    """The IMAP SEARCH arguments for ``query``; ``ALL`` when it names nothing."""
    if _CONTROL.search(query):
        raise QueryRefused(SUPPORTED)
    found: list[str] = []
    tokens = list(_TOKEN.finditer(query))
    if len(tokens) > MAX_TERMS:
        raise QueryRefused(SUPPORTED)
    for token in tokens:
        negate, operator, phrase, word = token.groups()
        value = phrase if phrase is not None else word
        if word is not None and operator is None and word.upper() == "OR":
            raise QueryRefused(SUPPORTED)
        term = _term(operator, value, today=today)
        found.extend(["NOT", *term] if negate else term)
    return found or ["ALL"]
