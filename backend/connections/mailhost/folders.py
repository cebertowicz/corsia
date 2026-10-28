"""Where a mail host keeps Drafts and Sent: RFC 6154 SPECIAL-USE flags first, then the common names.

The answer is cached per account sign-in, so one LIST serves every later draft and send.  Nothing is
cached for a folder the host did not show, so one created later is found.
"""

from __future__ import annotations

import imaplib
import re

DRAFTS = "drafts"
SENT = "sent"
_FLAGS = {DRAFTS: b"\\drafts", SENT: b"\\sent"}
# Names seen on the known hosts' own help pages and clients; the host's LIST decides which exist.
_NAMES = {
    DRAFTS: ("Drafts", "Draft", "Entw&APw-rfe"),
    SENT: ("Sent", "Sent Messages", "Sent Items", "Gesendet"),
}
_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')
_cache: dict[tuple[str, str], dict[str, str]] = {}


def _listing(conn: imaplib.IMAP4, *, strict: bool = False) -> list[tuple[set[bytes], str]]:
    status, data = conn.list()
    if status != "OK":
        if strict:
            raise imaplib.IMAP4.error("LIST failed")
        return []
    found = []
    for line in data:
        parsed = _LINE.match(line.strip()) if isinstance(line, bytes) else None
        if parsed is None:
            continue
        flags = {flag.lower() for flag in parsed["flags"].split()}
        name = parsed["name"].strip()
        if name.startswith(b'"') and name.endswith(b'"'):
            name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        if b"\\noselect" not in flags:
            found.append((flags, name.decode("ascii", "replace")))
    return found


def resolve(conn: imaplib.IMAP4, key: tuple[str, str], kind: str, *, strict: bool = False) -> str | None:
    """The folder name for ``kind`` (``DRAFTS`` or ``SENT``) on this account, or None when the host shows none."""
    if cached := _cache.get(key, {}).get(kind):
        return cached
    listing = _listing(conn, strict=strict)
    names = {name.lower(): name for _, name in listing}
    for each in (DRAFTS, SENT):
        flagged = next((name for flags, name in listing if _FLAGS[each] in flags), None)
        named = next((names[name.lower()] for name in _NAMES[each] if name.lower() in names), None)
        if found := flagged or named:
            _cache.setdefault(key, {})[each] = found
    return _cache.get(key, {}).get(kind)


def forget(key: tuple[str, str]) -> None:
    _cache.pop(key, None)
