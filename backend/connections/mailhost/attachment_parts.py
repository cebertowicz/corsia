"""Which parts of a mail-host message are attachments, from the message itself or from IMAP BODYSTRUCTURE.

A part with a filename is an attachment, as is one marked ``attachment``; an inline image has neither.
Both views walk parts depth first and treat an attached ``message/rfc822`` as one part, so the index of an
attachment is the same in each: the listing of a message too big to fetch whole still names the part to
save from the full message.
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.policy
import re
from collections.abc import Iterator
from urllib.parse import unquote

from connections.mail.received_attachments import Attachment

_TOKEN = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')
_ESCAPE = re.compile(rb"\\(.)")
# Base64 is wrapped at 76 characters plus CRLF: 78 encoded bytes per 57 decoded.
_BASE64_RATIO = (57, 78)


def _leaves(msg: email.message.MIMEPart) -> Iterator[email.message.MIMEPart]:
    if msg.is_multipart() and msg.get_content_type() != "message/rfc822":
        for part in msg.iter_parts():
            yield from _leaves(part)
    else:
        yield msg


def _attachment_leaves(msg: email.message.MIMEPart) -> list[email.message.MIMEPart]:
    return [p for p in _leaves(msg) if p.get_filename() or p.get_content_disposition() == "attachment"]


def _payload(part: email.message.MIMEPart) -> bytes:
    if part.get_content_type() == "message/rfc822":
        return part.get_payload(0).as_bytes()  # type: ignore[union-attr]
    return part.get_payload(decode=True) or b""  # type: ignore[return-value]


def listed(msg: email.message.MIMEPart) -> tuple[Attachment, ...]:
    """The attachments of a fully fetched message."""
    return tuple(Attachment(p.get_filename() or "", p.get_content_type(), len(_payload(p)), str(index))
                 for index, p in enumerate(_attachment_leaves(msg)))


def contents(raw: bytes) -> list[bytes]:
    """The bytes of every attachment of a full message, in listing order."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return [_payload(p) for p in _attachment_leaves(msg)]


def _parse(text: bytes) -> list:
    """The first parenthesised list in an IMAP reply, as nested lists of strings (NIL is None)."""
    root: list = []
    stack = [root]
    for found in _TOKEN.finditer(text):
        opened, closed, quoted, atom = found.groups()
        if opened:
            stack.append([])
        elif closed:
            if len(stack) == 1:
                break
            done = stack.pop()
            stack[-1].append(done)
        elif quoted is not None:
            stack[-1].append(_ESCAPE.sub(rb"\1", quoted).decode("utf-8", "replace"))
        else:
            stack[-1].append(None if atom.upper() == b"NIL" else atom.decode("utf-8", "replace"))
    return root[0] if root and isinstance(root[0], list) else []


def _params(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    return {str(k).upper(): str(v) for k, v in zip(value[::2], value[1::2]) if v is not None}


def _decoded(key: str, value: str) -> str:
    try:
        if key.endswith("*"):
            return unquote(value.split("'", 2)[-1])
        return str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, ValueError, UnicodeError):
        return value


def _leaf(part: list, index: int) -> Attachment | None:
    mime = f"{part[0]}/{part[1]}".lower()
    extension = 8 if part[0] and str(part[0]).lower() == "text" else 10 if mime == "message/rfc822" else 7
    disposition = part[extension + 1] if len(part) > extension + 1 else None
    if not isinstance(disposition, list):
        disposition = []
    kind = str(disposition[0]).lower() if disposition else ""
    sources = (_params(disposition[1] if len(disposition) > 1 else None), _params(part[2]))
    name = next((_decoded(key, params[key]) for params in sources
                 for key in ("FILENAME*", "FILENAME", "NAME*", "NAME") if params.get(key)), "")
    if not name and kind != "attachment":
        return None
    size = int(part[6]) if str(part[6]).isdigit() else 0
    if str(part[5]).lower() == "base64":
        size = size * _BASE64_RATIO[0] // _BASE64_RATIO[1]
    return Attachment(name, mime, size, str(index))


def _walk(part: object, found: list[Attachment]) -> None:
    if not isinstance(part, list) or not part:
        return
    if isinstance(part[0], list):
        for child in part:
            if not isinstance(child, list):
                break
            _walk(child, found)
    elif len(part) > 6:
        item = _leaf(part, len(found))
        if item is not None:
            found.append(item)


def from_structure(reply: bytes) -> tuple[Attachment, ...]:
    """The attachments named by a ``BODYSTRUCTURE`` reply, sizes as encoded on the wire (so approximate)."""
    found: list[Attachment] = []
    _walk(_parse(reply.partition(b"BODYSTRUCTURE")[2]), found)
    return tuple(found)
