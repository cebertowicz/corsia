"""Schema 189 -> 190: chat draws nothing for the comment-only inputs stored before 190.

From 190 on, ``place_comments`` records it when the input is stored: an empty
``chat_visible_content`` row for a user input with comments and no text or
attachments (``chat_comment_index.comment_only``). This step writes the same row
once for every such input already stored. It only inserts; no row is rewritten.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_189_to_190(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat_visible_content", "messages"} <= tables:
        return
    conn.execute(
        "INSERT OR IGNORE INTO chat_visible_content(message_id,thread_id,content) "
        "SELECT id,thread_id,'' FROM messages "
        # str.strip()'s blanks, as `comment_only` reads them; trim() alone drops only spaces.
        "WHERE role='user' AND trim(COALESCE(content,''),char(32,9,10,13,11,12))='' AND json_valid(metadata) "
        "AND COALESCE(json_array_length(metadata,'$.reply_context.comments'),0) > 0 "
        # Absent, null or an empty list; anything else counts as attachments, as in `comment_only`.
        "AND (COALESCE(json_type(metadata,'$.attachments'),'null')='null' "
        "OR (json_type(metadata,'$.attachments')='array' AND json_array_length(metadata,'$.attachments')=0))"
    )
