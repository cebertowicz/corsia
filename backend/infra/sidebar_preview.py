"""Shared sidebar message selection and answer projection."""

from infra.chat_comment_answers import project_previews

# Both listed and live previews select the same latest user or assistant message.
LATEST_MESSAGE_JOIN = """LEFT JOIN messages m ON m.id =
    (SELECT lm.id FROM messages lm WHERE lm.thread_id=s.id
     AND lm.role IN ('user','assistant') ORDER BY lm.created_at DESC LIMIT 1)"""
PREVIEW_CONTENT = """CASE WHEN EXISTS (
    SELECT 1 FROM chat_comment_answers a WHERE a.assistant_message_id = m.id
) THEN m.content ELSE substr(m.content, 1, 100) END"""
