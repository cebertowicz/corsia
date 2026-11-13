"""Schema 190 -> 191: an assistant step that draws nothing in chat is stamped ``chat_drawn: false``.

From 191 on, ``chat_comment_answers.place_answers`` stamps the flag as it saves a step: the step
placed answers (it has a ``chat_visible_content`` row), no chat text is left, and it has no
reasoning or tool calls (``chat_comment_answers.draws_nothing``). This step stamps the same flag
on the steps stored before 191, in SQL pinned to that function by
tests/test_migration_190_to_191_chat_drawn.py. It also leaves a step alone that a comment sits on,
which cannot exist when a step is first saved. It rewrites only ``metadata``; no schema changes.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_190_to_191(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"chat_visible_content", "chat_comments", "messages"} <= tables:
        return
    conn.execute(
        "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),'$.chat_drawn',json('false')) "
        "WHERE role='assistant' AND (metadata IS NULL OR (json_valid(metadata) AND json_type(metadata)='object')) "
        "AND COALESCE(json_type(metadata,'$.chat_drawn'),'null')<>'false' "
        "AND id IN (SELECT message_id FROM chat_visible_content WHERE content='') "
        "AND NOT EXISTS (SELECT 1 FROM chat_comments c WHERE c.target_message_id=messages.id) "
        # `draws_nothing`: a truthy reasoning (not null, false, 0 or ''), any non-empty call or hint list.
        "AND COALESCE(json_extract(metadata,'$.reasoning'),'') IN ('',0) "
        "AND COALESCE(json_array_length(CASE WHEN json_type(metadata,'$.llm_tool_calls')='array' "
        "THEN json_extract(metadata,'$.llm_tool_calls') END),0)=0 "
        "AND COALESCE(json_array_length(CASE WHEN json_type(metadata,'$.tool_calls')='array' "
        "THEN json_extract(metadata,'$.tool_calls') END),0)=0 "
        "AND COALESCE(json_array_length(CASE WHEN json_type(metadata,'$.tool_hints')='array' "
        "THEN json_extract(metadata,'$.tool_hints') END),0)=0"
    )
