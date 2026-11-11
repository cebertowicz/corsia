"""Identify completed Turns whose only visible outcome is a chat-comment answer."""

from __future__ import annotations

import json
import sqlite3

# ECMAScript String.trim whitespace, matching suppressibleCommentEnvelopes.
_TRIM = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def comment_only_receipt_turns(db: sqlite3.Connection, thread_id: str, turn_ids: list[str]) -> set[str]:
    """Return qualifying Turns, using the admitted inputs and placed answers as the authority."""
    if not turn_ids:
        return set()
    ids = json.dumps(turn_ids)
    # The first read deliberately does not join answer projections for ordinary Turns.
    candidates = db.execute(
        """SELECT t.id FROM json_each(?) j CROSS JOIN active_turns t
        WHERE t.id=j.value AND t.thread_id=? AND t.status='completed'
          AND EXISTS (SELECT 1 FROM messages i WHERE i.active_turn_id=t.id AND i.input_order IS NOT NULL)
          AND NOT EXISTS (
            SELECT 1 FROM messages i WHERE i.active_turn_id=t.id AND i.input_order IS NOT NULL
            AND (i.role != 'user' OR trim(COALESCE(i.content, ''), ?) != ''
              OR NOT json_valid(i.metadata)
              OR CASE WHEN json_valid(i.metadata) THEN
                COALESCE(json_array_length(i.metadata, '$.reply_context.comments'), 0)=0
                OR COALESCE(json_array_length(i.metadata, '$.attachments'), 0)>0
                ELSE 1 END)
          )""",
        (ids, thread_id, _TRIM),
    ).fetchall()
    candidate_ids = [row[0] for row in candidates]
    if not candidate_ids:
        return set()
    return {row[0] for row in db.execute(
        """SELECT j.value FROM json_each(?) j
        WHERE EXISTS (
          SELECT 1 FROM messages m INDEXED BY idx_messages_turn_calls
          -- CROSS JOIN keeps the turn's own messages first, so answers are reached by key.
          CROSS JOIN chat_comment_answers a ON a.assistant_message_id=m.id AND a.thread_id=m.thread_id
          JOIN chat_visible_content cv ON cv.message_id=m.id AND cv.thread_id=m.thread_id
          JOIN messages i ON i.id=a.user_message_id AND i.thread_id=a.thread_id
            AND i.active_turn_id=j.value AND i.input_order IS NOT NULL
          JOIN chat_comments c ON c.user_message_id=a.user_message_id AND c.cid=a.cid AND c.thread_id=a.thread_id
          JOIN messages target ON target.id=c.target_message_id AND target.thread_id=c.thread_id
          WHERE m.thread_id=? AND m.role='assistant' AND json_valid(m.metadata)
            AND json_extract(m.metadata, '$.turn_id')=j.value
        ) AND NOT EXISTS (
          SELECT 1 FROM messages m INDEXED BY idx_messages_turn_calls
          LEFT JOIN chat_visible_content cv ON cv.message_id=m.id
          WHERE m.thread_id=? AND m.role='assistant' AND json_valid(m.metadata)
            AND json_extract(m.metadata, '$.turn_id')=j.value
            AND json_extract(m.metadata, '$.for_user')=1
            AND trim(COALESCE(cv.content, m.content, ''), ?) != ''
        )""",
        (json.dumps(candidate_ids), thread_id, thread_id, _TRIM),
    ).fetchall()}


def _terminal_comment_only_receipt(thread_id: str, turn_id: str, db=None) -> bool:
    from infra.database import get_db

    connection = db or get_db()
    try:
        return turn_id in comment_only_receipt_turns(connection, thread_id, [turn_id])
    finally:
        if db is None:
            connection.close()


async def comment_only_receipt_field(thread_id: str, turn_id: str, *, db=None) -> dict[str, bool]:
    """Return the terminal frame's receipt field for a committed Turn.

    Every harness publishes its terminal frame only after the terminal commit
    applied, so the persisted ``completed`` status read by the classifier is the
    one completion authority; errored, cancelled and limit-downgraded Turns get
    no field. A caller-owned connection is used in place.
    """
    from infra.db_async import run_in_db_unless_owned

    try:
        hidden = await run_in_db_unless_owned(db, _terminal_comment_only_receipt, thread_id, turn_id, db=db)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Comment-only receipt lookup failed for turn %s", turn_id)
        return {}
    return {"comment_only_receipt": True} if hidden else {}
