"""Schema 186 -> 187: save the chat text of messages that answered comments before 187.

From 187 on, ``place_answers`` saves it when the message is written. This step does
the same once for every message already in ``chat_comment_answers``.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import sqlite3

from infra.chat_comment_answers import save_visible_content, visible_answer_content
from infra.chat_comment_schema import CHAT_VISIBLE_CONTENT_SQL


def migrate_186_to_187(conn: sqlite3.Connection) -> None:
    for statement in CHAT_VISIBLE_CONTENT_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat_comment_answers", "messages"} <= tables:
        return
    rows = conn.execute(
        "SELECT m.id,m.thread_id,m.content,json_group_array(a.cid) FROM chat_comment_answers a "
        "JOIN messages m ON m.id=a.assistant_message_id AND m.thread_id=a.thread_id "
        "GROUP BY m.id"
    ).fetchall()
    for message_id, thread_id, content, cids in rows:
        save_visible_content(conn, message_id, thread_id, visible_answer_content(content, json.loads(cids)))
