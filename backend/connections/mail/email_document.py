"""The email draft document: its header lines, body and signature, and the MIME built from it.

A draft is plain text the person reads in the workspace: ``To:``, optional ``Cc:`` and
``Subject:`` lines, a blank line, the body, then the signature after a ``-- `` delimiter.
``email_send`` parses the bytes the gate approved and builds the message from them.
"""

from __future__ import annotations

import re
import unicodedata
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any


SIGNATURE_DELIMITER = "-- "

# A plain address: no display name, quotes, brackets, separators or whitespace.
_ADDRESS = re.compile(r"""^[^\s,;<>"()\[\]\\@]+@[^\s,;<>"()\[\]\\@]+$""")
_HEADERS = ("from", "to", "cc", "subject")

#: The workspace folder the draft tools save email drafts into.
EMAIL_DRAFT_FOLDER = "email-drafts"

NEEDS_RECIPIENT = "A draft needs at least one recipient."
BAD_ADDRESS = "Recipients must be plain email addresses such as name@example.com."
NEEDS_SUBJECT = "A draft needs a one-line subject."
NEEDS_BODY = "A draft needs a body."


class DraftRefused(Exception):
    """A request the draft tools refuse; the text is fixed and safe to show."""


def is_plain_address(value: str) -> bool:
    """Whether an address has plain syntax and no Unicode control or format characters."""
    return bool(_ADDRESS.fullmatch(value)) and not any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in value
    )


def reply_addresses(reply_to: str, sender: str) -> list[str]:
    """Resolve reply recipients from Reply-To or From using the shared address rules."""
    return [address for _, address in getaddresses([reply_to or sender]) if is_plain_address(address)]


def addresses(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DraftRefused(BAD_ADDRESS)
    cleaned = [item.strip() for item in value if item.strip()]
    if not all(is_plain_address(item) for item in cleaned):
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


def reply_subject(subject: str) -> str:
    """The subject of a reply: ``Re: `` in front unless it already begins with one."""
    return subject if re.match(r"\s*re\s*:", subject, re.IGNORECASE) else f"Re: {subject}"


def compose_document(
    sender: str, to: list[str], cc: list[str], subject: str, body: str, signature: str,
) -> str:
    """The draft document: header lines, a blank line, the body, then the signature.

    ``From:`` shows the person which account sends; the send doesn't read it, so it can't
    choose a sender. The message a reply answers is in the draft's stored target, not here.
    """
    head = [f"From: {sender}", f"To: {', '.join(to)}"]
    if cc:
        head.append(f"Cc: {', '.join(cc)}")
    head.append(f"Subject: {subject}")
    text = "\n".join(head) + "\n\n" + body.strip("\n")
    if signature:
        text += f"\n\n{SIGNATURE_DELIMITER}\n{signature}"
    return text + "\n"


def _header_lines(head: str) -> list[tuple[str | None, str, int, int]]:
    """Each header line as ``(field, value, start, end)``: the field lowercase, or None when the line has no colon."""
    lines, start = [], 0
    for line in head.split("\n"):
        key, colon, value = line.partition(":")
        lines.append((key.strip().lower() if colon else None, value, start, start + len(line)))
        start += len(line) + 1
    return lines


def _utf16(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def read_header(text: str) -> dict:
    """The header block as the send reads it: each To and Cc entry's range and whether the send takes it as an address.

    ``text`` is the whole document (the header is every line up to the first blank line) and ranges are UTF-16 offsets into it,
    as the editor counts them, so the view can mark them while its header still reads the same.
    """
    head = text.replace("\r\n", "\n").partition("\n\n")[0]
    recipients = []
    for key, value, _, end in _header_lines(head):
        if key not in ("to", "cc"):
            continue
        at = end - len(value)
        for part in value.split(","):
            entry = part.strip()
            if entry:
                first = at + len(part) - len(part.lstrip())
                recipients.append({"field": key, "text": entry, "from": _utf16(head, first),
                                   "to": _utf16(head, first + len(entry)), "full": is_plain_address(entry)})
            at += len(part) + 1
    return {"text": head, "recipients": recipients}


def email_draft_view(draft_id: str, text: str) -> dict:
    """What a document load or live frame tells the view about an email draft: its record and its header."""
    return {"id": draft_id, "header": read_header(text)}


def parse_document(text: str) -> dict | None:
    """The headers and exact body of a draft document, or None when it isn't one.

    ``from`` is None for a document saved before the line existed.
    """
    head, separator, body = text.replace("\r\n", "\n").partition("\n\n")
    if not separator:
        return None
    found: dict[str, str] = {}
    for key, value, _, _ in _header_lines(head):
        if key is None or key not in _HEADERS or key in found:
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
