"""Schema 192 -> 193: drop a closing-text answer when a sent message in its Turn already gave it.

From 193 on, ``chat_comment_answers.place_answers`` places an answer only from words the person is
shown. Before, an agent that answered a comment with ``post_update`` and again in its closing text
had both stored, and the comment thread showed the answer twice. This step deletes the row that
came from words the person was not sent (no ``for_user``) when a ``for_user`` message in the same
Turn answered the same comment. Every other row stays: some are the only answer a comment has.

It deletes rows from ``chat_comment_answers`` only; no schema changes. Runs inside the runner's
transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_SENT = "m.role='assistant' AND json_valid(m.metadata) AND json_extract(m.metadata,'$.for_user')=1"


def migrate_192_to_193(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat_comment_answers", "messages"} <= tables:
        return
    conn.execute(
        "DELETE FROM chat_comment_answers WHERE rowid IN ("
        "SELECT a.rowid FROM chat_comment_answers a "
        "JOIN messages closing ON closing.id=a.assistant_message_id "
        "JOIN chat_comment_answers o ON o.thread_id=a.thread_id AND o.user_message_id=a.user_message_id "
        "AND o.cid=a.cid AND o.assistant_message_id<>a.assistant_message_id "
        "JOIN messages m ON m.id=o.assistant_message_id "
        f"WHERE {_SENT} "
        "AND COALESCE(CASE WHEN json_valid(closing.metadata) THEN json_extract(closing.metadata,'$.for_user') END,0)=0 "
        "AND json_extract(closing.metadata,'$.turn_id') IS NOT NULL "
        "AND json_extract(closing.metadata,'$.turn_id')=json_extract(m.metadata,'$.turn_id'))"
    )
