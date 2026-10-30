"""Place tagged answers when a message is written."""

from __future__ import annotations

import json
import re
import sqlite3

from infra.chat_comment_ids import CID_PATTERN, parse_cid

_TAGGED = re.compile(rf"(?:\A|\r?\n[ \t]*\r?\n)([ \t]*(?:\[{CID_PATTERN}\][ \t]*)+)")
_TAG = re.compile(rf"\[({CID_PATTERN})\]")


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


def has_comment_cid(metadata: str | None, cid: str) -> bool:
    try:
        value = json.loads(metadata or "{}")
        comments = value.get("reply_context", {}).get("comments", [])
        return isinstance(comments, list) and any(
            isinstance(item, dict) and item.get("cid") == cid for item in comments
        )
    except (ValueError, TypeError, AttributeError):
        return False


def place_answers(
    db: sqlite3.Connection, message_id: str, thread_id: str, role: str, content: str, seq: int, created_at: str,
) -> None:
    """Index known comments in the same transaction as the assistant message."""
    if role != "assistant" or "[c" not in content:
        return
    for cid in parse_answers(content, message_id):
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
            db.execute(
                "INSERT OR IGNORE INTO chat_comment_answers VALUES (?,?,?,?,?)",
                (thread_id, row[0], cid, message_id, created_at),
            )


def delete_thread_chat_comment_index_rows(db: sqlite3.Connection, thread_id: str) -> None:
    """Remove derived rows before the thread's messages are deleted."""
    db.execute(
        "DELETE FROM chat_comment_views WHERE user_message_id IN "
        "(SELECT id FROM messages WHERE thread_id=?)", (thread_id,),
    )
    db.execute("DELETE FROM chat_comment_answers WHERE thread_id=?", (thread_id,))
    db.execute("DELETE FROM chat_comments WHERE thread_id=?", (thread_id,))


# All projections compose this source and predicate so that missing comments,
# messages, and viewed answers have the same meaning everywhere.
UNREAD_FROM = """FROM chat_comment_answers a
JOIN threads t ON t.id=a.thread_id AND t.user_id=?
JOIN chat_comments c ON c.thread_id=a.thread_id AND c.user_message_id=a.user_message_id AND c.cid=a.cid
JOIN messages target ON target.id=c.target_message_id AND target.thread_id=a.thread_id
JOIN messages u ON u.id=a.user_message_id AND u.thread_id=a.thread_id
JOIN messages m ON m.id=a.assistant_message_id AND m.thread_id=a.thread_id
LEFT JOIN chat_comment_views v ON v.user_id=? AND v.user_message_id=a.user_message_id AND v.cid=a.cid"""
UNREAD_WHERE = "(v.user_id IS NULL OR v.seen_at < a.answered_at)"
