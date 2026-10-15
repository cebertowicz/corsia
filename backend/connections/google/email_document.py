"""The email draft document: its header lines, body and signature, and the MIME built from it.

A draft is plain text the person reads in the workspace: ``To:``, optional ``Cc:`` and
``Subject:`` lines, a blank line, the body, then the signature after a ``-- `` delimiter.
``email_send`` parses the bytes the gate approved and builds the message from them.
"""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any


SIGNATURE_DELIMITER = "-- "

# A plain address: no display name, quotes, brackets, separators or whitespace.
_ADDRESS = re.compile(r"""^[^\s,;<>"()\[\]\\@]+@[^\s,;<>"()\[\]\\@]+$""")
_HEADERS = ("from", "to", "cc", "subject", "in reply to")
UNREAD_REPLY_TARGET = "an earlier message"
_SHOWN_LIMIT = 200

NEEDS_RECIPIENT = "A draft needs at least one recipient."
BAD_ADDRESS = "Recipients must be plain email addresses such as name@example.com."
NEEDS_SUBJECT = "A draft needs a one-line subject."
NEEDS_BODY = "A draft needs a body."


class DraftRefused(Exception):
    """A request the draft tools refuse; the text is fixed and safe to show."""


def addresses(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DraftRefused(BAD_ADDRESS)
    cleaned = [item.strip() for item in value if item.strip()]
    if not all(_ADDRESS.match(item) for item in cleaned):
        raise DraftRefused(BAD_ADDRESS)
    return cleaned


def text_arg(value: Any, refusal: str, *, one_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftRefused(refusal)
    if one_line and re.search(r"[\r\n]", value):
        raise DraftRefused(refusal)
    return value.strip() if one_line else value


class _PlainText(HTMLParser):
    """The text of a Gmail HTML signature, one line per block."""

    _BREAKS = {"br", "p", "div", "tr", "li"}
    _SKIPPED = {"script", "style"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIPPED:
            self._skipping += 1
        elif tag in self._BREAKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BREAKS - {"br"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self._parts).replace("\xa0", " ").split("\n")]
        collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        # Gmail signatures often open with their own "--" line; the draft adds the delimiter.
        parts = collapsed.split("\n", 1)
        if parts[0].strip() == "--":
            collapsed = parts[1].strip() if len(parts) > 1 else ""
        return collapsed


def signature_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    parser.close()
    return parser.text()


def reply_target_label(sender: str | None, subject: str | None) -> str:
    """The original's sender and subject as one safe line, or a fixed phrase when neither read.

    Both come from someone else's email: whitespace and control characters collapse so a header
    value can never start a line of its own in the document.
    """

    def clean(value: str | None) -> str:
        return re.sub(r"[\s\x00-\x1f\x7f]+", " ", value or "").strip()[:_SHOWN_LIMIT]

    sender, subject = clean(sender), clean(subject)
    if sender and subject:
        return f'"{subject}" from {sender}'
    if subject:
        return f'"{subject}"'
    if sender:
        return f"a message from {sender}"
    return UNREAD_REPLY_TARGET


def compose_document(
    sender: str, to: list[str], cc: list[str], subject: str, body: str, signature: str,
    reply_label: str | None = None,
) -> str:
    """The draft document: header lines, a blank line, the body, then the signature.

    ``From:`` shows the person which account sends and ``In reply to:`` which message it
    answers; the send reads neither, so neither can choose a sender or a thread.
    """
    head = [f"From: {sender}", f"To: {', '.join(to)}"]
    if cc:
        head.append(f"Cc: {', '.join(cc)}")
    head.append(f"Subject: {subject}")
    if reply_label:
        head.append(f"In reply to: {reply_label}")
    text = "\n".join(head) + "\n\n" + body.strip("\n")
    if signature:
        text += f"\n\n{SIGNATURE_DELIMITER}\n{signature}"
    return text + "\n"


def parse_document(text: str) -> dict | None:
    """The headers and exact body of a draft document, or None when it isn't one.

    ``from`` is None for a document saved before the line existed.
    """
    head, separator, body = text.replace("\r\n", "\n").partition("\n\n")
    if not separator:
        return None
    found: dict[str, str] = {}
    for line in head.split("\n"):
        key, colon, value = line.partition(":")
        key = key.strip().lower()
        if not colon or key not in _HEADERS or key in found:
            return None
        found[key] = value.strip()
    if not found.get("to") or not found.get("subject") or not body.strip():
        return None
    try:
        to = addresses(found["to"].split(","))
        cc = addresses(found.get("cc", "").split(","))
    except DraftRefused:
        return None
    if not to:
        return None
    return {"from": found.get("from"), "to": to, "cc": cc, "subject": found["subject"], "body": body}


def build_message(parsed: dict, target: dict) -> EmailMessage:
    """The MIME message for a parsed draft; a header value with a line break raises ValueError."""
    message = EmailMessage(policy=policy.SMTP)
    message["To"] = ", ".join(parsed["to"])
    if parsed["cc"]:
        message["Cc"] = ", ".join(parsed["cc"])
    message["Subject"] = parsed["subject"]
    if target.get("in_reply_to"):
        message["In-Reply-To"] = target["in_reply_to"]
        message["References"] = target.get("references") or target["in_reply_to"]
    message.set_content(parsed["body"])
    return message
