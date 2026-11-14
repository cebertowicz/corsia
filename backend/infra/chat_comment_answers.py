"""Place tagged answers when a message is written."""

from __future__ import annotations

import json
import re
import sqlite3

from infra.chat_comment_ids import CID_PATTERN, could_start_cid, parse_cid

_TAGGED = re.compile(rf"(?:\A|\r?\n[ \t]*\r?\n)([ \t]*(?:\[{CID_PATTERN}\][ \t]*)+)")
_TAG = re.compile(rf"\[({CID_PATTERN})\]")
_BLANK = " \t\r\n"


def _held_from(text: str) -> int:
    """Where trailing text that could still open an answer section begins."""
    line_start = max(text.rfind("\n"), text.rfind("\r")) + 1
    line = text[line_start:].lstrip(" \t")
    if line.startswith("[") and could_start_cid(line[1:]):
        return len(text[:line_start].rstrip(_BLANK))
    return len(text.rstrip(_BLANK))


class VisibleAnswerStream:
    """Stream a step's text until an answer section opens; the saved step settles the rest."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pending = ""
        self.started = False
        self.held = False

    def feed(self, chunk: str) -> str:
        if self.held:
            return ""
        text = self.pending + chunk
        # A sentinel keeps the start-of-text anchor from matching once text has been sent.
        tag = _TAGGED.search("x" + text, 1) if self.started else _TAGGED.search(text)
        self.held = tag is not None
        end = tag.start() - self.started if tag else _held_from(text)
        self.pending = "" if self.held else text[end:]
        self.started = self.started or end > 0
        return text[:end]


def parse_answers(text: str, message_id: str) -> dict[str, list[dict[str, str]]]:
    """Return each tagged section for every comment it names."""
    matches = list(_TAGGED.finditer(text))
    answers: dict[str, list[dict[str, str]]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        answer = text[match.end():end].strip()
        for cid in dict.fromkeys(_TAG.findall(match.group(1))):
            answers.setdefault(cid, []).append({"text": answer, "assistant_message_id": message_id})
    return answers


def visible_answer_content(text: str, placed_cids: list[str]) -> str:
    """Keep the introduction and sections whose tags were not placed."""
    if not placed_cids:
        return text
    matches = list(_TAGGED.finditer(text))
    result = text[:matches[0].start()] if matches else text
    known = set(placed_cids)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if not known.intersection(_TAG.findall(match.group(1))):
            result += text[match.start():end]
    return result.strip()


def strip_answer_tags(text: str | None) -> str | None:
    """Drop the comment tags that open answer sections, keeping their text."""
    if not text:
        return text
    return _TAGGED.sub(lambda match: match.group(0)[:match.start(1) - match.start(0)], text)


# A message's chat text: what place_answers saved for it, else its content as written.
# Readers alias the message row ``m``.
VISIBLE_CONTENT_JOIN = "LEFT JOIN chat_visible_content cv ON cv.message_id=m.id"
VISIBLE_CONTENT = "COALESCE(cv.content, m.content)"


# The rule behind ``metadata.chat_drawn = false``, which ``place_answers`` (the one stamper) stamps once as
# it saves a step: the step placed answers, no chat text is left, and it has no reasoning or tool calls. A
# tool call appended to the saved step later (subagents/assistant_step_tool_append, the appender) clears it. Every
# reader reads the flag; the 191 -> 192 data step reads these same fields in SQL, pinned to
# ``draws_nothing`` by tests/test_migration_191_to_192_chat_drawn.py.
def _has_items(value: object) -> bool:
    return isinstance(value, list) and bool(value)


def draws_nothing(visible_text: str, metadata: dict | None) -> bool:
    """Whether a step that placed answers draws nothing in chat."""
    meta = metadata if isinstance(metadata, dict) else {}
    return not visible_text and not (
        meta.get("reasoning") not in (None, "", False, 0) or _has_items(meta.get("llm_tool_calls"))
        or _has_items(meta.get("tool_calls")) or _has_items(meta.get("tool_hints"))
    )


# SQL for "this message row is stamped as drawing nothing", for a table aliased as given.
def chat_not_drawn_sql(column: str = "metadata") -> str:
    return f"COALESCE((CASE WHEN json_valid({column}) THEN json_type({column}, '$.chat_drawn') END) = 'false', 0)"


def saved_answer_fields(db: sqlite3.Connection, thread_id: str, message_ids: list[str]) -> dict[str, dict]:
    """The chat text saved for each answering message, and the cids its comment threads show."""
    fields = {
        message_id: {
            "chat_answer_cids": [], "chat_visible_content": content, **({"chat_drawn": False} if not drawn else {}),
        }
        for message_id, content, drawn in db.execute(
            f"SELECT v.message_id,v.content,NOT {chat_not_drawn_sql('m.metadata')} "
            "FROM chat_visible_content v LEFT JOIN messages m ON m.id=v.message_id "
            "WHERE v.message_id IN (SELECT value FROM json_each(?)) AND v.thread_id=?",
            (json.dumps(message_ids), thread_id),
        )
    }
    if fields:
        for message_id, cid in db.execute(
            f"SELECT a.assistant_message_id,a.cid {SHOWN_ANSWERS_FROM} "
            "WHERE a.thread_id=? AND a.assistant_message_id IN (SELECT value FROM json_each(?))",
            (thread_id, json.dumps(list(fields))),
        ):
            fields[message_id]["chat_answer_cids"].append(cid)
    return fields


def message_answer_fields(thread_id: str, message_id: str, db: sqlite3.Connection | None = None) -> dict:
    """One message's saved answer fields; empty when it answered no comment."""
    from infra.database import get_db

    connection = db or get_db()
    try:
        return saved_answer_fields(connection, thread_id, [message_id]).get(message_id, {})
    finally:
        if db is None:
            connection.close()


def answered_comment_seqs(db: sqlite3.Connection, thread_id: str, message_ids: list[str]) -> list[int]:
    """The seqs of the commented rows that the placed answers of these messages sit on."""
    seqs: set[int] = set()
    for cid, metadata in db.execute(
        "SELECT a.cid, u.metadata FROM chat_comment_answers a JOIN messages u ON u.id=a.user_message_id "
        "WHERE a.thread_id=? AND a.assistant_message_id IN (SELECT value FROM json_each(?))",
        (thread_id, json.dumps(message_ids)),
    ):
        try:
            comments = json.loads(metadata or "{}").get("reply_context", {}).get("comments", [])
        except (ValueError, TypeError, AttributeError):
            continue
        for item in comments if isinstance(comments, list) else []:
            if isinstance(item, dict) and item.get("cid") == cid and isinstance(item.get("seq"), int):
                seqs.add(item["seq"])
    return sorted(seqs)


def has_comment_cid(metadata: str | None, cid: str) -> bool:
    try:
        value = json.loads(metadata or "{}")
        comments = value.get("reply_context", {}).get("comments", [])
        return isinstance(comments, list) and any(
            isinstance(item, dict) and item.get("cid") == cid for item in comments
        )
    except (ValueError, TypeError, AttributeError):
        return False


def focus_mode_on(db: sqlite3.Connection) -> bool:
    """As the settings API reads it: Focus is on unless saved off."""
    from infra.settings import get_setting

    return (get_setting("features", db=db) or {}).get("focus_mode", True) is not False


def _reaches_the_person(db: sqlite3.Connection, metadata: dict | None) -> bool:
    """Whether the person is shown this assistant message's words: sent for them, or Focus is off."""
    return bool(isinstance(metadata, dict) and metadata.get("for_user")) or not focus_mode_on(db)


def placeable_answers(
    db: sqlite3.Connection, message_id: str, thread_id: str, content: str, seq: int,
) -> list[tuple[str, str, str]]:
    """(cid, comment's message id, answer text) for each tagged section that answers a known comment."""
    found = []
    for cid, sections in parse_answers(content, message_id).items():
        parsed = parse_cid(cid)
        if parsed is None:
            continue
        row = db.execute(
            "SELECT id, metadata, seq FROM messages WHERE thread_id=? AND input_order=? AND role='user'",
            (thread_id, parsed[0]),
        ).fetchone()
        if row is None or row[2] is None or row[2] >= seq:
            continue
        if has_comment_cid(row[1], cid):
            found.append((cid, row[0], sections[0]["text"]))
    return found


def place_answers(
    db: sqlite3.Connection, message_id: str, thread_id: str, role: str, content: str, seq: int, created_at: str,
    metadata: dict | None = None, *, hidden_step: bool = False,
) -> None:
    """Index known comments, save the chat text left without them, and stamp a step that draws nothing.

    A comment is answered only by words the person is shown: a ``post_update`` message, or a message
    the Focus fallback turns into one (``place_flipped_answers``). Other closing text places nothing,
    except a hidden step of the Turn the fallback is showing the last words of (``hidden_step``): its
    answers go to their comment threads and its text stays out of chat, and a comment already
    answered keeps that answer.

    This is the one stamper of ``metadata.chat_drawn``; the appender clears it (``assistant_step_tool_append``).
    A step that placed answers and has nothing left for chat is stamped ``false`` here, in the transaction
    that saves it.
    """
    if role != "assistant" or "[c" not in content or not (hidden_step or _reaches_the_person(db, metadata)):
        return
    placed = []
    for cid, user_message_id, _text in placeable_answers(db, message_id, thread_id, content, seq):
        if hidden_step and db.execute(
            "SELECT 1 FROM chat_comment_answers WHERE thread_id=? AND user_message_id=? AND cid=?",
            (thread_id, user_message_id, cid),
        ).fetchone():
            continue  # a comment gets one answer: the newest words, placed first
        db.execute(
            "INSERT OR IGNORE INTO chat_comment_answers VALUES (?,?,?,?,?)",
            (thread_id, user_message_id, cid, message_id, created_at),
        )
        placed.append(cid)
    if placed:
        visible = visible_answer_content(content, placed)
        save_visible_content(db, message_id, thread_id, visible)
        if draws_nothing(visible, metadata):
            db.execute(
                "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),'$.chat_drawn',json('false')) WHERE id=?",
                (message_id,),
            )


def place_flipped_answers(
    db: sqlite3.Connection, thread_id: str, message_id: str, *, hidden_step: bool = False,
) -> None:
    """Place the answers in a message just made ``for_user``, or in a hidden step of the same Turn
    (``hidden_step``), in the transaction that flipped the Turn's last words."""
    row = db.execute(
        "SELECT role,content,seq,created_at,metadata FROM messages WHERE id=? AND thread_id=?",
        (message_id, thread_id),
    ).fetchone()
    if row is None or row[2] is None:
        return
    try:
        metadata = json.loads(row[4] or "{}")
    except ValueError:
        metadata = {}
    place_answers(db, message_id, thread_id, row[0], row[1] or "", row[2], row[3], metadata, hidden_step=hidden_step)


def save_visible_content(db: sqlite3.Connection, message_id: str, thread_id: str, content: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,?)",
        (message_id, thread_id, content),
    )


def delete_thread_chat_comment_index_rows(db: sqlite3.Connection, thread_id: str) -> None:
    """Remove derived rows before the thread's messages are deleted."""
    db.execute(
        "DELETE FROM chat_comment_views WHERE user_message_id IN "
        "(SELECT id FROM messages WHERE thread_id=?)", (thread_id,),
    )
    db.execute("DELETE FROM chat_comment_answers WHERE thread_id=?", (thread_id,))
    db.execute("DELETE FROM chat_comments WHERE thread_id=?", (thread_id,))
    db.execute("DELETE FROM chat_visible_content WHERE thread_id=?", (thread_id,))


# All projections compose this source and predicate so that missing comments,
# messages, and viewed answers have the same meaning everywhere.
# An answer shows in its comment thread only while its comment, sender and target rows exist.
SHOWN_ANSWERS_FROM = """FROM chat_comment_answers a
JOIN chat_comments c ON c.user_message_id=a.user_message_id AND c.cid=a.cid AND c.thread_id=a.thread_id
JOIN messages sender ON sender.id=a.user_message_id AND sender.thread_id=a.thread_id
JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id"""
UNREAD_FROM = """FROM chat_comment_answers a
JOIN threads t ON t.id=a.thread_id AND t.user_id=?
JOIN chat_comments c ON c.thread_id=a.thread_id AND c.user_message_id=a.user_message_id AND c.cid=a.cid
JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id
JOIN messages u ON u.id=a.user_message_id AND u.thread_id=a.thread_id
JOIN messages m ON m.id=a.assistant_message_id AND m.thread_id=a.thread_id
LEFT JOIN chat_comment_views v ON v.user_id=? AND v.user_message_id=a.user_message_id AND v.cid=a.cid"""
# One agent message is one answer in a comment thread, however many of the thread's cids it tags.
# That answer is read and stamped through its first row: the thread's earliest tagged comment.
UNREAD_WHERE = """(v.user_id IS NULL OR v.seen_at < a.answered_at) AND NOT EXISTS (
SELECT 1 FROM chat_comment_answers o
JOIN chat_comments oc ON oc.thread_id=o.thread_id AND oc.user_message_id=o.user_message_id AND oc.cid=o.cid
WHERE o.assistant_message_id=a.assistant_message_id AND +o.thread_id=a.thread_id
AND COALESCE(oc.root_user_message_id,oc.user_message_id)=COALESCE(c.root_user_message_id,c.user_message_id)
AND COALESCE(oc.root_cid,oc.cid)=COALESCE(c.root_cid,c.cid)
AND (oc.created_at,oc.user_message_id,oc.cid) < (c.created_at,c.user_message_id,c.cid))"""
