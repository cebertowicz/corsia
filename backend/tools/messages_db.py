"""Read the Mac's Messages database (``chat.db``) read-only: chats, texts, search, recipient lookup.

Everything here is blocking; callers run it in a thread. A refusal is a ``MessagesError`` whose text
is one of the fixed sentences in ``tools.messages_texts``.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import sys
from pathlib import Path

from tools import messages_texts as texts

#: Seconds between 1970-01-01 and 2001-01-01, the epoch of Messages dates (stored in nanoseconds).
APPLE_EPOCH = 978307200
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
NEEDED = {
    "message": {"ROWID", "guid", "text", "attributedBody", "handle_id", "date", "is_from_me", "error", "associated_message_type"},
    "chat": {"ROWID", "guid", "chat_identifier", "display_name", "service_name"},
    "handle": {"ROWID", "id"},
    "chat_message_join": {"chat_id", "message_id"},
    "chat_handle_join": {"chat_id", "handle_id"},
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MessagesError(Exception):
    """A state the person or agent must act on; ``str(error)`` is its fixed sentence."""


def is_mac() -> bool:
    return sys.platform == "darwin"


def open_db() -> sqlite3.Connection:
    """The database, read-only, after the schema check; raises the fixed state sentence otherwise."""
    if not is_mac():
        raise MessagesError(texts.NOT_A_MAC)
    try:
        os.stat(CHAT_DB)
    except FileNotFoundError:
        raise MessagesError(texts.NO_HISTORY) from None
    except PermissionError:
        raise MessagesError(texts.NO_FDA) from None
    try:
        db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        raise MessagesError(texts.NO_FDA) from None
    try:
        for table, columns in NEEDED.items():
            missing = columns - {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
            if missing:
                raise MessagesError(texts.SCHEMA.format(table=table, columns=", ".join(sorted(missing))))
    except sqlite3.Error:
        db.close()
        raise MessagesError(texts.NO_FDA) from None
    except MessagesError:
        db.close()
        raise
    return db


def decode(text: str | None, blob: bytes | None) -> str:
    """A text's words: its ``text`` column, else the string inside its ``attributedBody`` typedstream."""
    if text:
        return text
    if not blob:
        return ""
    start = blob.find(b"NSString")
    marker = blob.find(b"+", start) if start >= 0 else -1
    if marker < 0 or marker + 1 >= len(blob):
        return ""
    at, length = marker + 2, blob[marker + 1]
    if length == 0x81:
        length, at = int.from_bytes(blob[at:at + 2], "little"), at + 2
    elif length == 0x82:
        length, at = int.from_bytes(blob[at:at + 4], "little"), at + 4
    return blob[at:at + length].decode("utf-8", "replace")


def when(nanoseconds: int) -> str:
    return dt.datetime.fromtimestamp(nanoseconds / 1e9 + APPLE_EPOCH).strftime("%Y-%m-%d %H:%M")


def now_ns() -> int:
    return int((dt.datetime.now().timestamp() - APPLE_EPOCH) * 1e9)


def _cut(body: str, limit: int) -> str:
    return body if len(body) <= limit else body[:limit] + texts.MORE_CHARS.format(count=len(body) - limit)


def chat_label(name: str | None, identifier: str, people: int) -> str:
    return f"{name} (group chat, {people} people)" if people > 1 else (name or identifier)


def chats(db: sqlite3.Connection, limit: int) -> str:
    rows = db.execute(
        "SELECT c.ROWID, COALESCE(NULLIF(c.display_name,''), c.chat_identifier), "
        "(SELECT COUNT(*) FROM chat_handle_join h WHERE h.chat_id=c.ROWID), MAX(m.date) "
        "FROM chat c JOIN chat_message_join j ON j.chat_id=c.ROWID JOIN message m ON m.ROWID=j.message_id AND m.associated_message_type=0 "
        "GROUP BY c.ROWID ORDER BY MAX(m.date) DESC LIMIT ?", (limit,),
    ).fetchall()
    if not rows:
        return texts.NO_CHATS
    lines = (f"{r[0]} | {r[1]} | {r[2]} {'person' if r[2] == 1 else 'people'} | {when(r[3])}" for r in rows)
    return "\n".join((texts.DATA_RULE, *lines))


def read(db: sqlite3.Connection, chat_id: int, limit: int) -> str:
    if db.execute("SELECT 1 FROM chat WHERE ROWID=?", (chat_id,)).fetchone() is None:
        raise MessagesError(texts.NO_CHAT)
    rows = db.execute(
        "SELECT m.date, m.is_from_me, COALESCE(h.id,''), m.text, m.attributedBody FROM message m "
        "JOIN chat_message_join j ON j.message_id=m.ROWID LEFT JOIN handle h ON h.ROWID=m.handle_id "
        "WHERE j.chat_id=? AND m.associated_message_type=0 ORDER BY m.date DESC LIMIT ?", (chat_id, limit),
    ).fetchall()[::-1]
    if not rows:
        return texts.NO_TEXTS
    lines = (f"{when(d)} {'me' if mine else who}: {_cut(decode(t, ab), 500)}" for d, mine, who, t, ab in rows)
    return "\n".join((texts.DATA_RULE, *lines))


def search(db: sqlite3.Connection, query: str, limit: int) -> str:
    needle = query.lower()
    out: list[str] = []
    cursor = db.execute(
        "SELECT j.chat_id, m.date, m.is_from_me, COALESCE(h.id,''), m.text, m.attributedBody FROM message m "
        "JOIN chat_message_join j ON j.message_id=m.ROWID LEFT JOIN handle h ON h.ROWID=m.handle_id "
        "WHERE m.associated_message_type=0 ORDER BY m.date DESC",
    )
    for chat_id, date, mine, who, text, blob in cursor:
        body = decode(text, blob)
        if needle in body.lower():
            out.append(f"chat {chat_id} | {when(date)} {'me' if mine else who}: {_cut(body, 200)}")
            if len(out) >= limit:
                break
    return "\n".join((texts.DATA_RULE, *out)) if out else texts.NO_MATCH


def normalize_handle(value: str) -> str | None:
    """A phone number as ``+`` and digits, or an email lowercased; None for anything else."""
    raw = value.strip()
    if _EMAIL.match(raw):
        return raw.lower()
    digits = re.sub(r"[\s().-]", "", raw)
    return digits if re.fullmatch(r"\+\d{8,15}", digits) else None


def chat_by_id(db: sqlite3.Connection, chat_id: object) -> dict | None:
    """One chat by its ``chat_id``: guid, label and service, or None."""
    row = db.execute(
        "SELECT c.ROWID, c.guid, COALESCE(NULLIF(c.display_name,''), c.chat_identifier), c.service_name, "
        "(SELECT COUNT(*) FROM chat_handle_join h WHERE h.chat_id=c.ROWID) FROM chat c WHERE c.ROWID=?",
        (int(chat_id) if str(chat_id).isdigit() else -1,),
    ).fetchone()
    if row is None:
        return None
    return {"chat_id": str(row[0]), "guid": row[1], "label": chat_label(row[2], row[2], row[4]),
            "service": row[3] or "iMessage"}


def chats_for_handle(db: sqlite3.Connection, handle: str) -> list[dict]:
    """The one-to-one chats whose identifier is exactly this number or email."""
    rows = db.execute(
        "SELECT ROWID FROM chat WHERE lower(chat_identifier)=? ORDER BY ROWID", (handle.lower(),),
    ).fetchall()
    return [found for row in rows if (found := chat_by_id(db, row[0])) is not None]


def sent_row(db: sqlite3.Connection, chat_guid: str | None, handle: str | None, body: str, since: int) -> str | None:
    """The guid of a text this Mac sent to that chat after ``since`` with exactly this body and no error."""
    rows = db.execute(
        "SELECT m.guid, m.text, m.attributedBody FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID "
        "JOIN chat c ON c.ROWID=j.chat_id WHERE (c.guid=? OR lower(c.chat_identifier)=?) AND m.is_from_me=1 "
        "AND m.date>? AND m.error=0 AND m.associated_message_type=0 ORDER BY m.date DESC",
        (chat_guid or "", (handle or "").lower(), since),
    ).fetchall()
    return next((str(guid) for guid, text, blob in rows if decode(text, blob) == body), None)
