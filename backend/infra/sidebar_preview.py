"""Shared sidebar message selection and batched answer projection."""

import json
import sqlite3

from infra.chat_comment_answers import _TAGGED, SHOWN_ANSWERS_FROM, strip_answer_tags, visible_answer_content

# Both listed and live previews select the same latest user or assistant message.
LATEST_MESSAGE_JOIN = """LEFT JOIN messages m ON m.id =
    (SELECT lm.id FROM messages lm WHERE lm.thread_id=s.id
     AND lm.role IN ('user','assistant') ORDER BY lm.created_at DESC LIMIT 1)"""
PREVIEW_CONTENT = "CASE WHEN instr(m.content, '[c') > 0 THEN m.content ELSE substr(m.content, 1, 100) END"


def project_previews(db: sqlite3.Connection, rows: list[tuple[str, str | None, str | None]],
                     *, limit: int | None = 100) -> list[str | None]:
    """Project saved message text in one answer lookup; optionally cut the result."""
    pairs = list(dict.fromkeys((thread, message) for thread, message, text in rows
                               if message and text and '[c' in text))
    answers: dict[tuple[str, str], list[str]] = {}
    if pairs:
        source = SHOWN_ANSWERS_FROM.replace(
            'FROM chat_comment_answers a',
            "FROM json_each(?) requested CROSS JOIN chat_comment_answers a "
            "ON a.assistant_message_id=json_extract(requested.value, '$[1]') "
            "AND a.thread_id=json_extract(requested.value, '$[0]')",
        )
        query = f"SELECT a.thread_id, a.assistant_message_id, a.cid {source}"
        for thread, message, cid in db.execute(query, (json.dumps(pairs),)):
            answers.setdefault((thread, message), []).append(cid)
    result = []
    for thread, message, text in rows:
        if message and text and '[c' in text:
            text = visible_answer_content(text, answers.get((thread, message), []))
        if text and limit is not None:
            # A preview needs only the first visible characters, but a complete
            # tag must be recognized before any of its text is removed.
            chunks: list[str] = []
            pos = length = 0
            while length < limit and text.find('[c', pos) != -1:
                match = _TAGGED.search(text, pos)
                if match is None:
                    break
                prefix = text[pos:match.start(1)]
                chunks.append(prefix)
                length += len(prefix)
                pos = match.end()
            chunks.append(text[pos:pos + max(0, limit - length)])
            text = ''.join(chunks)[:limit]
        elif limit is None:
            text = strip_answer_tags(text)
        result.append(text)
    return result
